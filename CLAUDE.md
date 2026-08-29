# InsightCut 项目说明

## 项目结构

```
Auto-jianji/
├── ai-kepu-video-server/    # 后端服务（FastAPI）
│   ├── api_server.py         # FastAPI 应用入口
│   ├── main.py               # 命令行工具入口
│   └── src/                  # 源代码
└── ai-kepu-video-web/        # 前端项目
    └── frontend/             # React 前端应用
```

## 启动服务

### 代理文档同步规则

- 本仓库同时维护 `CLAUDE.md` 和 `AGENTS.md` 两份代理说明。
- 两份文件的内容必须保持一致；如果修改端口、启动命令、环境变量、项目约定或开发注意事项，必须同步修改另一份文件。
- 默认端口固定为：前端 `2001`，后端 `2002`。前后端端口不能冲突。

### 一键启动前后端

当用户说"启动"、"打开"、"重新启动端口"时，需要同时启动前后端服务：

**1. 停止旧进程（如果存在）**
```bash
# 停止前端（端口 2001）
lsof -ti:2001 | xargs kill -9 2>/dev/null || true

# 停止后端（端口 2002）
lsof -ti:2002 | xargs kill -9 2>/dev/null || true
```

**2. 启动后端服务**
```bash
cd ai-kepu-video-server && \
source venv/bin/activate && \
python -m uvicorn api_server:app --host 127.0.0.1 --port 2002 --reload
```
- 后端地址：http://localhost:2002
- API 文档：http://localhost:2002/docs
- 健康检查：http://localhost:2002/health

**3. 启动前端服务**
```bash
cd ai-kepu-video-web/frontend && \
npm run dev
```
- 前端地址：http://localhost:2001

### 前后端配置对齐

- 前端配置文件：`ai-kepu-video-web/frontend/.env.development`
  ```
  VITE_API_BASE_URL=http://localhost:2002
  VITE_POLLING_INTERVAL=2000
  ```
- 后端监听端口：`2002`
- 前端开发端口：`2001`

## 重要说明

1. **后端入口文件**：使用 `api_server.py`（不是 `main.py`）
2. **虚拟环境**：后端需要激活 venv 虚拟环境
3. **后台运行**：两个服务都应该在后台运行（`run_in_background: true`）
4. **启动顺序**：先启动后端，再启动前端（避免前端启动时后端未就绪）
5. **本机安全边界**：后端默认只监听 `127.0.0.1`；CORS 只允许 `http://localhost:2001` 和 `http://127.0.0.1:2001`，不得恢复任意跨域或局域网监听。
6. **批量预案**：Web 的“批量预案”支持 2–50 个主题、并发 1–3；只生成文稿、分镜和图片提示词并停在确认阶段，不自动生图或配音。CLI 使用 `python main.py batch --file topics.txt --concurrency 1..3 [--no-wait]`，通过同一后端 API 执行。
7. **字幕与导出**：SRT/VTT、字幕资产和素材包必须复用 `SubtitleWriter`；导出必须经过 `AutoExporter` 的文件存在性与草稿预检，禁止用布尔值伪造成功。

## 开发注意事项

- 前端使用 React 19 + React Router 7 + Vite 8
- 后端使用 FastAPI + Python 3.10+（CI 使用 Python 3.11；安全修复版依赖不再支持 Python 3.9）
- 素材库按 `segment_index` 排序展示（播放顺序）
- 本地维护巡检：在 `ai-kepu-video-server/` 下运行 `python scripts/maintenance_report.py --dry-run` 查看日志、数据库、媒体目录体量和未引用素材；只有显式使用 `--apply` 才会删除未被数据库引用的媒体文件。
- **任务失败不能丢已生成内容**：任何任务被标记为 `failed` 时，已经生成的分镜文本、图片 prompt、图片、音频、草稿文件等资产必须继续入库并在素材库/预览页正常展示；失败状态只表示后续流程停止，不代表清空或隐藏已有资产。
- **超时失败也要先保资产**：自动超时、手动失败、异常失败前，必须尽量保存当前已生成的 `task_segments` 和 `task_assets`，让用户能查看、替换、重新生成或基于已有素材继续处理。
- **后台任务巡检**：`TASK_SWEEPER_INTERVAL_SECONDS` 控制超时任务与孤儿 operation 的后台巡检间隔，默认 `300` 秒；等待确认和等待完成生产阶段不参与超时判定。

## 模型调用架构

### 生文模块（LLM）

使用 **LiteLLM** 统一调用层，支持 100+ 模型提供商：

- **服务商选择**：`llm.provider` 选择 LiteLLM provider 或项目扩展服务商；旧配置没有该字段时，先根据规范模型前缀、再根据 Registry 已识别的 `base_url` 推断，均无法识别则归入 `custom`。`protocol` 不参与 provider 推断，只在 `custom` 兼容路由中保留。
- **模型 ID**：已知服务商的模型保存为 LiteLLM 规范 ID（如 `deepseek/deepseek-chat`、`openai/mimo-v2.5-pro`），不使用 UI 显示名称作为运行参数。
- **自定义兼容接口**：`protocol` / `base_url` / `model` 仍可编辑，用于自定义 OpenAI-compatible 或 Anthropic-compatible 端点及旧配置兼容。
- **Provider Registry**：默认目录来自当前安装的 LiteLLM 本地元数据，并合并项目覆盖与 MiMo 等扩展；账号可用模型的在线同步是可选能力，失败不影响本地目录和已选模型。
- **凭证安全**：不得记录 API Key、Authorization header、provider secret 字段、完整凭证对象或模型同步请求数据。
- **核心文件**：`src/text/generator.py` 的 `_call_api()` 方法

### 生图模块

使用 **Agnes Image 2.1 Flash**，OpenAI 兼容 images/generations 接口：

- **API URL**：`https://apihub.agnes-ai.com/v1/images/generations`
- **模型**：`agnes-image-2.1-flash`
- **价格**：当前免费
- **配置方式**：`api_url` + `api_key` + `model`
- **核心文件**：`src/media/image_generator.py`
- **请求格式注意**：Agnes 的 `response_format` 必须放在 `extra_body.response_format`，不能放在请求体顶层
- **免费限速注意**：2026-08-18 使用当前免费账号实测，8 路瞬时请求全部成功，但滚动一分钟累计成功 20 次后继续请求会返回 429；`IMAGE_CONCURRENCY` 默认 `8`，项目内按滚动 60 秒最多 20 次请求节流，遇到 429 按 `retry-after` 或 60 秒等待后重试
- **统一重试策略**：`GENERATION_RETRY_COUNT` 控制额外重试次数（0–5，默认 2），`GENERATION_RETRY_INTERVAL_SECONDS` 控制普通失败的基础等待秒数（1–60，默认 5）并按尝试次数递增；HTTP 429 仍优先遵循服务商 `Retry-After`

### TTS 模块

独立配置，`tts.enabled_providers` 控制豆包与 MiMo 是否同时开放，`tts.provider` 只表示新任务的默认 provider。任务创建和重配音接口统一使用带 provider 的 `voice_type`：

- **豆包 TTS**：`provider=doubao`，支持 `auth_method=access_token` 的 `api_url/appid/token/cluster/default_voice` 旧版配置，也支持 `auth_method=api_key` 的 `api_url/api_key/cluster/default_voice` 火山 API Key 配置；音色列表来自本地 SQLite。
- **统一音色 ID**：预置音色使用 `mimo:<voice_id>` 或 `doubao:<voice_id>`，MiMo 本地克隆音色使用 `mimo-clone:<clone_id>`；旧的不带前缀 ID 仍兼容。
- **小米 MiMo TTS**：配置保存在 `tts.mimo.base_url/api_key/model/clone_model/default_voice/format/style_prompt/speed_level`。
- **小米接口注意**：MiMo TTS 不走 `/v1/audio/speech`，而是请求 OpenAI 兼容的 `/v1/chat/completions`；待合成文本放在 assistant message，风格指令放在 user message，音频从 `choices[0].message.audio.data` 读取 base64 后写出 wav。
- **小米预置音色**：`mimo_default/冰糖/茉莉/苏打/白桦/Mia/Chloe/Milo/Dean`；`/ai/native/video/kepu/voices` 可按 `provider/include_disabled` 返回双端预置音色与 MiMo 本地克隆音色。
- **音色列表**：SQLite 内置 MiMo 9 个与豆包 10 个预置音色；设置页可分 provider 全选、半选或关闭，所有已开放音色都可试听。豆包默认开放当前账号已授权的“爽快思思”与“讲解小明”，默认为讲解小明。
- **TTS 参数**：任务和分段会快照 `speed_level`（很慢/偏慢/正常/偏快/很快）；豆包额外保存 `volume_ratio`，MiMo 额外保存 `style_prompt`，设置改动不会悄悄改掉旧任务。
- **MiMo VoiceClone**：克隆模型固定为 `mimo-v2.5-tts-voiceclone`。上传的 MP3/WAV 或浏览器录音会统一转换为 24 kHz 单声道 WAV，保存到 `data/media/_voice_clones/<clone_id>/reference.wav`，试听保存为同目录 `preview.wav`。
- **VoiceClone 安全与限制**：必须确认声音授权；参考音频转为 Base64 后不得超过 10 MB。小米没有远端保存 `voice_id` 机制，每次克隆合成都从本地临时组装 `data:audio/wav;base64,...` 作为 `audio.voice`；DataURL 不得写入数据库、配置或日志。
- **VoiceClone 生命周期**：状态为 `draft/ready/failed/hidden`；只有试听成功的克隆音色才能启用。已被任务引用的克隆音色删除时只做隐藏，未引用的才删除本地文件。克隆失败也必须保留参考音频和已生成试听。

## 存储配置

### 本地存储模式（当前使用）

**只使用本地存储**，所有文件存储在本地：

- **媒体文件目录**：
  - `output/` - 新任务生成的文件（如"开心"项目）
  - `data/media/` - 旧任务的文件（如"3:4测试v3"）

- **媒体服务端点**：`/media/{file_path}`
  - 自动支持两个目录，优先查找 `output/`，回退到 `data/media/`
  - 示例：`http://localhost:2002/media/开心/images/segment_000.png`

- **上传/生成文件**：
  - 统一使用 `LocalUploader` 复制到 `data/media/`
  - 不再支持 OSS/COS 上传、`USE_REMOTE_DB` 切换、MySQL/Redis 远程模式
  - 数据固定使用本地 SQLite：`ai-kepu-video-server/data/local.db`
