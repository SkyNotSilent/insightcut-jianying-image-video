<div align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/insightcut-mark-dark.svg" />
    <source media="(prefers-color-scheme: light)" srcset="docs/assets/insightcut-mark.svg" />
    <img src="docs/assets/insightcut-mark.svg" width="112" alt="InsightCut 品牌图标" />
  </picture>
  <h1>InsightCut</h1>
  <p><strong>本地优先、可编辑、可恢复的 AI 图片视频工作台</strong></p>
  <p>从主题或完整文稿出发，完成分镜、画面、配音、字幕、预览与剪映 / CapCut 导出。</p>
  <p>
    简体中文 · <a href="README_EN.md">English</a>
  </p>
  <p>
    <a href="https://github.com/SkyNotSilent/insightcut-jianying-image-video/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/SkyNotSilent/insightcut-jianying-image-video/ci.yml?branch=master&amp;style=flat-square&amp;label=CI" alt="CI 状态" /></a>
    <a href="https://github.com/SkyNotSilent/insightcut-jianying-image-video/stargazers"><img src="https://img.shields.io/github/stars/SkyNotSilent/insightcut-jianying-image-video?style=flat-square&amp;color=315FEA" alt="GitHub Stars" /></a>
    <img src="https://img.shields.io/badge/Python-3.10%2B-315FEA?style=flat-square&amp;logo=python&amp;logoColor=white" alt="Python 3.10+" />
    <img src="https://img.shields.io/badge/React-19-315FEA?style=flat-square&amp;logo=react&amp;logoColor=white" alt="React 19" />
    <img src="https://img.shields.io/badge/Local--first-SQLite-D46F44?style=flat-square" alt="本地优先" />
    <img src="https://img.shields.io/badge/License-not%20declared-68748B?style=flat-square" alt="尚未声明开源许可证" />
  </p>
  <p>
    <a href="https://skynotsilent.github.io/insightcut-jianying-image-video/showcase/"><strong>成片案例</strong></a>
    · <a href="#从零开始使用"><strong>快速开始</strong></a>
    · <a href="#从一句输入开始"><strong>产品界面</strong></a>
    · <a href="docs/"><strong>项目文档</strong></a>
    · <a href="https://github.com/SkyNotSilent/insightcut-jianying-image-video/issues"><strong>问题反馈</strong></a>
  </p>
</div>

---

InsightCut 面向科普、认知、知识解说、观点表达和短视频创作。它不是只交付一个 MP4 的“一键成片”页面，而是一套可以持续工作的本地项目：文稿、分镜、图片提示词、图片、配音、字幕和导出记录都会被保留下来。

<table>
  <tr>
    <td width="33%"><strong>生成以后还能改</strong><br />逐段修改文字、提示词、图片和配音，不必为一个细节重跑整条链路。</td>
    <td width="33%"><strong>失败以后接着做</strong><br />任务中断不会清空已完成内容，只重试缺失或失败的素材。</td>
    <td width="33%"><strong>成片与工程都交付</strong><br />输出 MP4、分镜素材包，以及可继续精修的剪映 / CapCut 草稿。</td>
  </tr>
</table>

> 数据库、媒体文件、配置和声音克隆参考音频默认保存在本机。InsightCut 与剪映、CapCut、字节跳动没有隶属或合作关系。

## 从一句输入开始

在同一页面输入一句主题或完整文稿，并在生成前集中确认项目名称、画面比例、视觉风格与文案风格。主题模式会先扩写完整文稿并交给你确认；脚本模式严格保留原文。

![InsightCut 新版文稿工作台真实界面](docs/screenshots/manuscript.png)

## 真实成片案例

下面的案例全部来自 InsightCut 本地任务，不是为 README 拼接的静态效果稿。任务完成后，对应的文稿、分镜、提示词、图片、配音、字幕和导出记录仍可继续修改。

<p align="center"><a href="https://skynotsilent.github.io/insightcut-jianying-image-video/showcase/"><strong>浏览完整在线成片展厅 →</strong></a></p>

### 我是谁？我在哪？我要去何方？

**主题模式** · 原始输入：`我是谁？我在哪？我要去何方？`

**温暖感人文案** · **毛毡风** · 16:9 · 20 个分镜 · 01:09

模型从这 14 个字扩写完整文稿，再生成分镜、画面提示词、图片、配音、字幕和成片。没有为案例额外补写脚本。

https://github.com/user-attachments/assets/3c8061af-0100-453c-af08-614a55dd24f0

### 小岛经济学

**脚本模式** · 输入：完整知识文稿

**科普知识文案** · **毛毡风** · 16:9 · 01:02

https://github.com/user-attachments/assets/34833f83-5416-41b9-9a75-b47ddd0e5f05

### 30 年河东，30 年河西，莫欺少年穷

**主题模式** · 原始输入：`30 年河东，30 年河西，莫欺少年穷`

**知识科普文案** · **吉卜力** · 16:9 · 18 个分镜 · 00:44

https://github.com/user-attachments/assets/989b52b5-2f3e-4ed6-9a36-6c26fd02f606

### 成长就是最好的解药

**脚本模式** · 输入：完整励志文稿

**励志向上文案** · **电影质感** · 16:9 · 01:03

https://github.com/user-attachments/assets/de662301-8653-43bf-bf54-31a6d7d2dba1

### 为什么成年人越忙越需要刻意留白？

**脚本模式** · 输入：完整知识文稿

**知识科普文案** · **电影质感** · 9:16 · 00:55

https://github.com/user-attachments/assets/7d7b050e-25c2-4f4f-8cf5-1ab3f7157a95

### 别把忙碌误当成成长

**脚本模式** · 输入：完整口播文稿

**轻松口语文案** · **国风** · 3:4 · 00:52

https://github.com/user-attachments/assets/e4695fbc-2339-4102-b631-bcf61df63264

## 生产与资产界面

### 生产工作台

![InsightCut 新版分镜生产工作台与素材版本界面](docs/screenshots/workspace.png)

白色空间化工作台保留真实的 01–06 生产阶段：左侧浏览分镜，中间完成即时预览，右侧处理当前分镜、素材版本、项目素材和全片设置。

图片与配音不会在重新生成时覆盖旧文件。每次生成、重新生成、上传和跨分镜复用都会形成可追溯版本，可以随时查看、试听和恢复；恢复历史版本不会再次调用模型。

### 项目资产库

![InsightCut 项目资产库：项目筛选、状态与真实画面封面](docs/screenshots/assets.png)

项目资产库集中展示草稿、生成中、可继续、已完成和失败可恢复的项目，并支持按状态、画面风格和时长筛选。项目卡片使用真实生成画面作为封面，可以直接回到对应项目继续预览、修复或导出。

## InsightCut 是什么

一条完整的视频生产流程通常比想象中长：

```text
主题 / 文稿
  → 脚本整理
  → 分镜与画面提示词
  → AI 图片
  → 配音与字幕
  → 预览和逐段修正
  → MP4 / 剪映草稿
```

普通“一键成片”很适合快速看到结果，但当画面不对、某句配音需要重来，或者生成进行到一半发生错误时，往往只能重新开始。InsightCut 把脚本、分镜、图片、音频、字幕和导出文件都保留下来，让一次生成变成一个可以继续工作的项目，而不是一个用完即走的结果文件。

## 为什么不是普通的一键成片

- **不只交付一个成片。** 每个分镜都有自己的旁白、画面提示词、图片、配音、状态和历史记录。
- **默认生成以后还能改。** 可以逐段替换图片、修改提示词、重新生成图片或配音，不需要为了一个细节从头跑完整条链路。
- **失败不会清空已经完成的内容。** 任务中断或后续步骤失败时，已经生成的脚本、图片、音频和草稿仍会进入素材库与预览页。
- **数据默认留在本机。** SQLite 数据库、媒体、配置和声音克隆参考音频都使用本地存储，适合个人创作、实验和排查问题。
- **成片和可编辑草稿都重要。** MP4 方便快速查看和发布；剪映 / CapCut 草稿适合继续调整节奏、字幕和包装。

## 现在可以做什么

1. 用一句主题生成文稿，或粘贴、导入自己的完整文稿。
2. 选择画面比例、视觉风格和文案风格，提交后立即进入持续保存的生产工作台。
3. 按照 01–06 阶段检查完整文稿、短分镜和逐段画面提示词，再确认全片音色与画面。
4. 按需生成 AI 图片与 TTS 配音，并在同一工作台逐段查看真实进度和可操作错误说明。
5. 单个素材失败时只重试对应图片或配音；已经完成的素材不会被恢复流程隐式重做。
6. 查看图片与配音的不可变历史版本，上传替换素材，或从当前项目素材池中回选已有版本。
7. 无需先生成 MP4 即可连续预览；需要时再生成完整视频预览，并在导出时复用同一文件。
8. 导出完整视频、分镜素材包和剪映 / CapCut 草稿，三种交付互不替代。
9. 使用生产模板保存画面、配音、字幕、并发和重试策略；多个项目运行时可通过全局任务胶囊切换查看。
10. 在 API 配置页管理 LiteLLM 生文模型、Agnes 生图、豆包 TTS、小米 MiMo TTS 和本地克隆音色。

## 从零开始使用

### 1. 准备环境

当前后端需要 Python 3.10+（CI 使用 3.11），前端使用 React 19、React Router 7 和 Vite 8。还需要 Node.js 20.19+、npm，以及可用的 FFmpeg；项目安装的 `imageio-ffmpeg` 可以提供内置二进制，系统 PATH 中已有 FFmpeg 时会优先使用系统版本。

```bash
git clone https://github.com/SkyNotSilent/insightcut-jianying-image-video.git
cd insightcut-jianying-image-video
```

准备后端：

```bash
cd ai-kepu-video-server
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

准备前端：

```bash
cd ../ai-kepu-video-web/frontend
npm install
```

### 2. 启动后端

在 `ai-kepu-video-server/` 中运行：

```bash
source venv/bin/activate
python -m uvicorn api_server:app --host 127.0.0.1 --port 2002 --reload
```

- 后端：<http://localhost:2002>
- API 文档：<http://localhost:2002/docs>
- 健康检查：<http://localhost:2002/health>

### 3. 启动前端

另开一个终端，在 `ai-kepu-video-web/frontend/` 中运行：

```bash
npm run dev
```

前端默认打开 <http://localhost:2001>。前端在没有额外环境变量时会连接 `http://localhost:2002`；如需覆盖，可设置 `VITE_API_BASE_URL`。

### 4. 配置模型

打开 <http://localhost:2001/settings>：

1. 在“生文模型”里选择服务商，填写该服务商要求的凭证。
2. 选择内置模型，或点击“验证并同步”读取当前账号可用的生文模型。
3. 填写 Agnes 生图 API Key，并检查图片尺寸。
4. 启用豆包、MiMo 或两者，选择新任务默认使用的配音 Provider。
5. 选择开放音色并试听；需要克隆音色时，进入 MiMo 配置完成授权确认和参考音频上传。
6. 点击“保存配置”。密钥保存在本机配置中，不要把 `data/config.json`、`.env` 或截图中的凭证提交到 Git。

### 5. 完成第一次生成

回到文稿页，输入一个主题，或者切换到文稿模式粘贴完整内容。选择画面比例与视觉风格后生成内容预案；进入生产工作台检查文稿、分镜和提示词，确认音色后再生成图片与配音。素材会在同一页面逐段出现，可以直接连续预览，最后再按需要生成完整视频预览、MP4 或剪映草稿。

### 6. 把结果写入剪映

剪映草稿和 MP4 是两份独立的交付结果。生产任务会先完成文案、分镜、图片和配音，接着构建一份可迁移的剪映草稿并打包成 ZIP，最后再用 FFmpeg 合成 MP4。所以“写入剪映”不是把已经合成的 MP4 重新导入，也不会再调用模型；它只是把现有图片、配音、字幕和时间轴安装到本机剪映的草稿目录。

推荐在任务完成后进入导出页：

1. 在“剪映草稿”区域选择 Mac 或 Windows。
2. 点击“选择”，指定剪映草稿根目录。Mac 通常为 `/Users/你的用户名/Movies/JianyingPro/User Data/Projects/com.lveditor.draft`；Windows 以剪映实际设置的草稿位置为准。
3. 项目会自动验证这个目录，但不会在未经同意的情况下扫描整台电脑。
4. 点击“写入剪映”。后端会创建独立的草稿文件夹，复制素材和草稿 JSON，再按目标系统重写素材路径。
5. 如果剪映没有立即显示新草稿，完全退出并重新打开剪映专业版，然后在“本地草稿”中打开项目。

写入后的项目仍然保留独立的图片、配音、字幕、镜头顺序和时长，可以在剪映里继续修改。如果使用“下载草稿 ZIP”，需要先为它创建一个独立项目文件夹，再将 ZIP 内容完整解压到该文件夹并放入剪映草稿根目录。不要在剪映的“导入媒体”中选择 ZIP，那不会被识别为可编辑草稿。

## 生文模型怎么配置

生文调用由 LiteLLM 统一管理。普通配置不再要求先理解协议、Base URL 和模型 ID：先选择服务商，再选择模型即可。

- 服务商和模型都是可搜索选择器。
- 选择已知服务商后，项目会给出相应的连接字段、默认地址和推荐模型。
- “验证并同步”会把账号实际可用模型合并到同一个模型选择器，不会再出现第二个需要手工抄写的输入框。
- 同步结果只保留生文模型；图片、ASR、TTS、VoiceClone 和 VoiceDesign 等模型不会混进生文列表。
- 已保存但暂时不在目录中的历史模型仍会保留，目录更新不会偷偷替换现有配置。
- OpenAI-compatible 和 Anthropic-compatible 的自定义接口仍然可用，Base URL、协议和模型 ID 放在高级配置中。

API Key 只用于当前本地服务连接。请不要把密钥写进 README、Issue、截图或任何会被推送到远端的文件。

## 配音、试听与声音克隆

豆包和 MiMo 可以同时开放。`tts.provider` 只表示新任务默认选择哪一端，不会关闭另一端；任务与分段会保存当时的 Provider、音色和语速快照，所以以后修改全局设置不会悄悄改变旧项目。

### 豆包 TTS

豆包支持 App ID / Access Token 和 API Key 两种认证方式。项目内置 10 个预置音色，默认开放当前账号常用的“爽快思思”和“讲解小明”，默认音色为“讲解小明”。

豆包任务可以设置统一语速和 `volume_ratio`。只有账号真实有权限的音色才能正常生成；试听失败时，先检查认证方式、Cluster 和音色授权范围。

### 小米 MiMo TTS

MiMo 通过 OpenAI 兼容的 Chat Completions 形式返回音频，不使用常见的 `/v1/audio/speech` 路径。项目内置 9 个音色：`mimo_default`、冰糖、茉莉、苏打、白桦、Mia、Chloe、Milo 和 Dean。

除了统一语速，MiMo 还支持风格提示词，例如更克制、更有活力或更像知识讲解。预置音色可以在配置页按 Provider 全选、部分选择或关闭，所有已开放音色都可以先试听再决定。

### MiMo 声音克隆

第一版完整支持 MiMo VoiceClone。你可以上传 MP3 / WAV，也可以直接使用浏览器录音。后端会把参考音频统一转换成 24 kHz、单声道 WAV，并保存在本地：

1. 新建克隆音色并填写名称。
2. 明确确认已经获得声音本人授权。
3. 上传参考音频或完成录音。
4. 输入试听文本，生成试听音频。
5. 试听成功后再启用该克隆音色，并在生产配置中选择它。

参考音频转成 Base64 后不能超过 10 MB。小米当前没有为这条链路提供远端持久化 `voice_id`，因此每次克隆合成都从本地参考音频临时组装请求；音频 Data URL 不会写入数据库、配置或日志。已经被任务引用的克隆音色删除时只会隐藏，不会破坏旧任务。

## 失败以后，已经生成的内容怎么办

任务状态为 `failed` 只表示后续流程停止，不代表清空已经完成的内容。InsightCut 会尽量保存当前脚本、分镜、图片提示词、图片、音频和草稿文件，并继续在素材库或预览页展示。

你可以在已有内容上继续处理：

- 按错误类型查看服务商返回的可操作说明；内容策略拒绝、限流和普通网络失败不会再混成同一种提示；
- 批量只重试当前失败项，或在分镜卡上单独重试一张图片、一段配音；
- 修改分镜文字和画面提示词；
- 上传替换素材、回选历史版本，或者从项目素材池复用已有图片与配音；
- 在有恢复点时继续生成，或者直接使用已有素材完成后续编辑。

## 输出内容

- 带图片、配音、字幕和基础动态的 MP4。
- 可继续编辑的剪映 / CapCut 草稿 ZIP。
- 分镜文字、图片提示词和项目记录。
- 图片与配音的不可变生成、重试、上传、替换和回选历史。
- TTS 音频和字幕 SRT。
- 可用于恢复、重配音和重新生成的本地素材记录。

## 技术栈

| 层 | 技术 |
| --- | --- |
| 前端 | React 19、React Router 7、Vite 8、Axios、Lucide |
| 后端 | FastAPI、Python 3.10+ |
| 数据库 | 本地 SQLite |
| 生文 | LiteLLM Provider Registry，兼容 OpenAI / Anthropic 协议 |
| 生图 | Agnes Image 2.1 Flash，OpenAI-compatible Images API |
| 配音 | 豆包 TTS、小米 MiMo TTS、MiMo VoiceClone |
| 视频 | FFmpeg、imageio-ffmpeg |
| 剪映草稿 | pyJianYingDraft |
| 存储 | 本地 `data/media/` 与 `output/` |

## 项目结构

```text
insightcut-jianying-image-video/
├── ai-kepu-video-server/          # FastAPI 后端
│   ├── api_server.py              # Web API 入口
│   ├── src/                       # 生文、生图、配音、任务和导出逻辑
│   ├── data/                      # 本地 SQLite、媒体和配置（不提交）
│   └── output/                    # 新任务生成结果（不提交）
├── ai-kepu-video-web/frontend/    # React 19 + Vite 前端
├── docs/
│   ├── assets/                    # README 品牌资源
│   ├── screenshots/               # 当前产品界面截图
│   ├── showcase/                  # 可在线播放的真实成片案例
│   └── prd/                       # 已确认的产品需求文档
└── scripts/                       # 仓库检查和维护工具
```

## 测试与构建

后端：

```bash
cd ai-kepu-video-server
source venv/bin/activate
python -m pytest -q
```

前端：

```bash
cd ai-kepu-video-web/frontend
npm test
npm run build
```

只做本地维护巡检时，可以在后端目录运行：

```bash
python scripts/maintenance_report.py --dry-run
```

只有显式改为 `--apply` 才会删除数据库未引用的媒体文件。

## 本地数据与安全

下面这些内容不应该进入版本库：

- `.env` 和真实 API Key、Access Token、App ID；
- `ai-kepu-video-server/data/local.db`；
- `data/media/`、`output/` 和生成日志；
- MiMo 声音克隆参考音频与试听文件；
- 含有账号信息、密钥或本地绝对路径的截图。

媒体服务统一通过 `/media/{file_path}` 访问，先查找 `output/`，再回退到 `data/media/`。项目当前只使用本地 SQLite 和本地文件存储，不会自动把密钥或克隆音频上传到 InsightCut 自有服务。

## 更多文档

- [按需预览与成片导出 PRD](docs/prd/2026-08-16-on-demand-video-preview-and-export.md)
- [分镜素材包导出 PRD](docs/prd/2026-08-16-storyboard-asset-package-export.md)
- [工程协作流程](docs/engineering-workflow.md)
- [仓库维护约定](docs/repo-hygiene.md)
- [GitHub 设置](docs/github-settings.md)

## 当前阶段

InsightCut 仍是一个持续迭代的、本地优先的单用户产品原型。当前重点是把个人创作者最常用的一条链路做完整：文稿、模型配置、分镜、素材、配音、预览、失败恢复和导出。

多人协作、云端计费、多租户账号、模板市场和托管媒体存储不在当前版本范围内。

## Keywords

`AI video generation` · `AI image video` · `image-to-video` · `AI explainer video` · `AI cognition video` · `local-first video workflow` · `storyboard editor` · `TTS voiceover` · `voice cloning` · `MP4 export` · `Jianying draft` · `CapCut draft` · `FastAPI` · `React` · `Vite` · `FFmpeg`

中文关键词：`AI 视频生成`、`AI 图片视频`、`AI 认知视频`、`AI 解说视频`、`文稿转视频`、`分镜编辑`、`音色试听`、`声音克隆`、`剪映草稿导出`、`本地优先素材管理`。

## License

项目目前尚未声明开源许可证。在新增许可证前，请将本仓库视为 source-available 项目。
