# InsightCut 产品化优化 Roadmap

> 状态：V2.0 已实施并通过验收 · 2026-08-23
> **方向变更：原 B「编辑部／衬线字体」方向自 2026-08-23 起正式废止。** 当前唯一视觉基准为 Spatial Pro 白色专业工作台：现代无衬线字体、克制蓝色强调色、轻量玻璃质感和固定三栏信息架构。
> 范围：前端 `ai-kepu-video-web/frontend` + 后端 `ai-kepu-video-server`
> 本文档是后续逐项开工的执行依据。每个条目均含根因文件定位与验收标准。

## V2.0 覆盖性实施决议（2026-08-23）

以下内容优先级高于本文中所有早期视觉设想；若与后文冲突，以本节为准：

1. 全局壳层固定为左侧功能栏、跨页面任务活动胶囊和项目内 01–06 步骤条；品牌区不得保留顶部空白条。
2. 工作台固定为“分镜导航｜作品预览｜右侧检查器”，右侧包含当前分镜、素材版本、项目素材、全片设置四个真实页签；中栏不得恢复“当前分镜素材／画面与配音”重复卡片。
3. 多项目允许同时运行，但共享供应商限流；全局活动只通过 `GET /activity/tasks` 单次聚合轮询，不产生逐任务请求风暴。
4. 图片和配音采用不可变版本记录，分镜只移动 selected asset 指针；恢复历史版本不得调用模型、覆盖旧文件或刷新旧记录时间。
5. 模板库首版仅提供本地生产预设 CRUD；应用到旧项目只标记真实受影响素材，绝不自动生成。
6. 项目级字幕和生成策略随任务快照保存；即时预览、完整视频和剪映草稿统一读取同一字幕快照。

V2.0 总体验收以“全局创作壳层、并行任务与素材版本系统改版方案”的验收清单为准，并继续继承本文 P0 可靠性不变量。

## 0. 当前基线、已确认约束与非目标

### 当前代码基线（事实）

- 精确素材重试改造已作为独立可回退基线提交 `3cf85e9`：包含 `task_operations`、`POST /retry-assets`、`POST /finalize`、`awaiting_finalization` 以及工作台按钮分流；V2.0 全局壳层与版本系统改造没有混入该基线提交。
- 当前主路由已经把 `/production`、`/process`、`/preview` 重定向至 `/workspace/:taskId`；`PreviewPage`、`ProcessPage`、`ProductionSetupPage` 虽仍在源码中，但不在当前 `App.jsx` 主路由渲染链路中。
- 当前 `WorkspacePage` 仍自行实现工作台轮询和完整视频任务轮询；`usePolling` 只被旧 `ProcessPage` 使用。因此“统一轮询”不是直接复活旧 hook，而是先抽象出能同时覆盖 workspace 与 export job 的公共轮询内核。
- 当前 CSS 中约有 184 个不同的硬编码十六进制颜色值。Phase 3 的目标应以语义 token 覆盖率和视觉回归为准，不能只以机械删除颜色数量为准。

### 已确认、后续不得反向修改的产品约束

1. 保留旧版三栏工作台骨架：左侧分镜导航、中间作品预览与内容流、右侧当前分镜/全片设置；桌面端应用视口锁定，三栏独立滚动。
2. 精确重试、主动替换、待更新素材、预案恢复、草稿构建是五种不同语义，任何入口不得再次统一落回整任务 `/resume`。
3. 默认不生成 MP4；即时预览由浏览器组合素材，完整视频仅在用户点击后生成，正式导出复用有效的完整视频结果。
4. 短分镜、脚本原文策略、提示词逐段独立调用以及现有生文/生图/TTS 模型选择不在本 Roadmap 中调整。
5. 任务失败、超时、刷新、离页或服务重启均不得删除已生成文案、提示词、图片、音频、草稿或历史版本。

### 非目标

- 不迁移到云存储、远程数据库、SSE/WebSocket 或服务端低清代理视频。
- 不在 UI 改版中改变导出三种交付语义，不自动上传、发布或对外分享用户内容。
- 不用减少分镜数量、合并提示词请求或缩短内容来制造“更快”的假象。
- 不在可靠性修复阶段同时大规模换视觉；P0 可靠性与 P1/P1.5 视觉批次必须独立验收。

---

## 0.1 诊断总结：为什么现在像玩具

项目骨架并不差——后端有 checkpoint、`task_operations` 幂等操作表、启动恢复与懒惰超时检测；前端有乐观锁保存、逐段重试按钮。但四类问题叠加后，用户体验呈现为"链路随时会断、断了不知道为什么、也不知道下一步该干嘛"：

### ① 失败/重试链路存在真实的死胡同（最严重）

| 症状 | 根因 |
|---|---|
| 任务永远卡在"素材未齐全"，重试按钮却说"没有需要重试的素材" | `stale` 素材被 `_workspace_health` 算作缺失，却被 `_workspace_recovery_targets()`（`routes.py:1949`）跳过 |
| full 模式的 failed 任务没有任何恢复入口 | `_reconcile_workspace_state()`（`routes.py:1962`）对非 review_first 任务提前 return |
| "正在生成完整视频预览"永远转圈 | 预览轮询的 `catch` 直接杀掉 interval，无提示无状态变更（`WorkspacePage.jsx:317`） |
| 后端挂了，界面看起来"冻结但健康" | 工作台 quiet 轮询吞掉所有错误（`WorkspacePage.jsx:295-299`） |
| 错误只有一句"LLM API 调用失败，请检查模型配置"，用户无法自救 | `generator.py:99` 把真实 provider 错误（401/429/超时）脱敏丢弃 |
| 50 段里 1 段提示词失败，整个任务停摆 | `task_executor.py:1235` 任何提示词失败即抛 `RecoverableTaskError` 整体 halt |
| finalize 失败后可无限原地重复同一失败 | `_run_finalize_task`（`task_executor.py:914-925`）失败后原样放回 `awaiting_finalization` |
| 上传失败但界面显示成功 | Upload 失败被降级为 warning，asset 状态仍是 `completed`（`task_executor.py:1400-1411`） |

### ② 用户引导几乎为零

- 没有阶段说明：用户不知道"预案 → 确认音色 → 确认画面 → 生成素材"每步在干嘛、要多久。
- 创建任务前不检查 API 配置——用户等任务失败才发现要先配 key。
- 最复杂的工作台页面零 tooltip；术语混乱（预案/生产预案/内容预案、项目/任务/草稿混用）。
- "受众人群"下拉框是纯装饰（`ManuscriptPage.jsx:224`，不接状态、不上送 API）。

### ③ UI"AI 味"的来源是没有设计系统

- CSS 中约 184 个不同的硬编码十六进制颜色值，语义变量覆盖很低；间距/字号/阴影大量逐条手写。
- 讽刺：仓库里有一份完整 token 文件 `src/styles/apple-design.css`（466 行）是死代码。
- 工作台单屏塞下预览列 + 分镜流 + 检查器 + 全文折叠 + 分镜表 + 底部操作条，层级扁平、密度过高。
- 素材生成阶段没有总体进度/ETA；唯一会算加权全局进度的组件 `TaskProgress.jsx` 也是死代码。

### ④ 技术债拖慢一切改造

- `WorkspacePage.jsx` 928 行巨石组件（20+ useState、单行 JSX 超 600 字符）。
- 约 1000 行死代码：`PreviewPage`(598)、`ProductionSetupPage`(235)、`ProcessPage`(104)、`TaskProgress`、`usePolling`、`format.js`、`preview-page.css`、`apple-design.css`。
- 三套各自为政的轮询实现，最完善的 `usePolling`（3 次失败退避 + generation token）未被任何页面使用。
- 前端零组件渲染/E2E 测试（现有 14 个 test.mjs 主要覆盖纯函数）。

---

## 1. Phase 1 — 可靠性：修死胡同（P0）

> 目标：任何任务在任何失败点都有"看得懂的原因 + 点得动的出路"。

### R1 · stale 素材死锁（保留语义隔离）

- **问题**：修改设置后素材被标 `stale`，健康检查算未就绪，但 `_workspace_recovery_targets()` 又有意不把 stale 当失败项。当前页面虽另有“更新受影响素材”按钮，底部恢复动作仍可能显示“重试 N 个缺失或失败素材”且 targets 为空，形成互相矛盾的双入口。
- **原则**：`stale` 代表“旧文件仍存在但已与当前内容快照不匹配”，不是生成失败。不得把 stale 混进 `scope=failed`，否则会破坏精确重试与主动更新的语义边界。
- **修法**：
  1. `_workspace_health` 增加 `stale_images`、`stale_audio`、`failed_images`、`failed_audio`，就绪、失败、待更新分别计数。
  2. `_workspace_recovery()` 在只有 stale 或 stale 优先时返回独立 `mode=update_stale_assets`，并在 `recovery.targets` 精确列出受影响项；`retry-assets scope=failed` 继续只解析失败/缺失，待更新仍通过 `scope=selected + targets` 执行。
  3. 前端底部只展示一个与 recovery mode 一致的主恢复动作；分镜卡仍保留单项“更新此图/更新配音”。
  4. stale 未更新前禁止 finalize/完整视频，已存在旧文件仍允许对照查看；任何内容相关变化立即令旧完整视频与 MP4 过期。
- **验收**：
  - 构造 stale-only 任务，workspace 返回 `mode=update_stale_assets` 和准确 targets，界面不出现“失败重试”措辞。
  - 点击批量更新只调用 targets 中对应的生成器；未受影响素材的路径、文件 mtime、数据库 `updated_at` 均不变化。
  - 对同一任务调用 `scope=failed` 不包含 stale；待更新全部成功后进入 `awaiting_finalization`，失败项保留旧文件并仍显示待处理原因。

### R2 · full 模式 failed 任务无恢复入口

- **问题**：非 review_first 任务 failed 后，`recovery.allowed=false` 且前端 `canResume` 为假 → 界面只有警示、没有任何按钮。
- **根因**：`routes.py:1962` reconciliation 对非 review_first 提前 return；`workspacePreview.js:43` 的 `canResume` 依赖后端字段。
- **修法**：让 full 模式也使用统一的 health/capabilities/recovery 推导，但不得把 full 旧任务直接改写成 review_first。按真实检查点返回 `resume_planning` / `retry_assets` / `update_stale_assets` / `finalize`，并保留旧任务的原始执行模式与导出资产。
- **验收**：
  - 分别构造“无分镜、缺提示词、缺一张图片、素材齐全但无草稿”的 full 旧任务，每种状态均只有一个正确的主恢复动作。
  - 缺一张图的 full 任务只产生 1 次生图调用、0 次 TTS/提示词调用；素材齐全场景 finalize 产生 0 次模型调用。
  - 已完成旧任务仍可即时预览、查看历史素材与导出，兼容链接重定向后功能不降级。

### R3 · 结构化错误模型（贯穿前后端）

- **问题**：所有错误是自由中文字符串，LLM 真实失败原因（401/429/超时/模型不存在）在 `generator.py:99` 被整体替换为 `_SAFE_LLM_FAILURE`，用户与前端都无法分类处理。
- **修法**：
  1. 定义稳定错误分类枚举：`auth` / `rate_limit` / `timeout` / `provider_error` / `network` / `disk` / `config_missing` / `conflict` / `cancelled` / `unknown`，并定义 `retryable`、`retry_after_seconds`、`safe_message` 三个安全字段。
  2. `tasks.error`、`task_segments.*_error`、`task_operations.error` 旁增加对应 code 字段；安全结构化元数据放 JSON 字段，SQLite 走幂等列迁移，旧行无 code 时按 `unknown` 兼容。
  3. `generator.py` / `image_generator.py` / TTS 实现层只保留允许暴露的 provider、HTTP 状态码、request id 与分类；API Key、Authorization、完整请求体、克隆音色 DataURL 永不进入数据库、日志或前端。
  4. workspace 与 export job 统一透传安全错误结构；前端由单一 `errorMessages.js` 映射行动文案，禁止直接把未知 backend detail 原样展示。
- **验收**：
  - 模拟 401、429（含/不含 Retry-After）、连接超时、磁盘写失败、409 版本冲突，API 与界面均给出正确分类和下一步动作。
  - 429 文案只在确有等待值时显示秒数；401/config_missing 不自动盲重试。
  - 用包含假 API Key、Authorization、DataURL 的异常做日志/数据库/响应扫描，敏感串匹配结果为 0。
  - 用旧数据库启动时迁移只执行一次，旧错误记录仍能展示为通用错误。

### R4 · 失败聚合横幅

- **问题**：失败信息散落在各分镜卡片 chip 上，用户离开页面回来后无从下手；且每段只显示第一个错误（prompt/image/audio 折叠为一条）。
- **修法**：工作台顶部（stage note 区域）在存在失败时渲染持久横幅："N 段失败（图片 x · 配音 y · 提示词 z）"+ 一键重试全部 + 展开失败清单（可点击定位到分镜）；分镜卡片同时展示多类错误而非只取第一个。数据源用现成的 `health` + `recovery.targets`，基本无需后端改动。
- **验收**：
  - 多类型失败任务的横幅总数与展开后的唯一 targets 数一致，同一分镜的 prompt/image/audio 可分别显示，不重复计数。
  - “重试全部失败项”仅走 `retry-assets scope=failed`；stale 项进入独立“更新受影响素材”动作，不混入失败计数。
  - 点击清单项定位对应分镜，刷新、离开项目资产再返回后横幅与定位能力仍存在。
  - 全部失败项修复后横幅消失且页面不跳动；补后端聚合单测与 Playwright 截图。

### R5 · 部分失败可继续（不再一票否决）

- **问题**：提示词阶段（`task_executor.py:1235`）与素材阶段（`:1555`）只要有一项失败就抛错整体 halt，违背"能保多少保多少"的产品约定精神。
- **修法**：
  1. 去掉“失败率 >50%”这一经验阈值，按错误性质决策：单项可重试错误（限流、超时、单文件 provider_error）允许已派发任务收敛；系统级错误（auth、config_missing、磁盘不可写）停止派发新请求并进入 `interrupted`。
  2. `repairing_assets` 仅表示有素材操作正在执行，不能作为静止的失败汇总态。提示词或素材批次收敛后，只要仍有目标未完成，就进入 `interrupted` 并携带精确 recovery；全部完成则进入下一合法阶段。
  3. 一个分镜失败不取消其他已经开始的请求；取消操作只停止派发新请求，已完成结果继续保存。
- **验收**：
  - 12 段中 mock 2 段图片普通失败：其余 10 段完成并可查看，任务最终为 `interrupted`，recovery 只列 2 个失败目标。
  - 任意段出现 auth/config_missing：调度器停止新增请求，已完成结果保留，界面主动作改为“检查配置”而非“立即重试”。
  - 429 只暂停对应 provider 的新派发并遵循 Retry-After；图片限流不阻塞已可独立运行的 TTS。
  - 服务重启后 processing 目标恢复为可重试，completed 目标不重复调用。

### R6 · 前端静默轮询修复（依赖 T3）

- **问题**：三处手写轮询里两处吞错——预览轮询 `catch` 直接杀 interval（`WorkspacePage.jsx:317`），quiet 轮询无限静默重试（`:295-299`）。
- **修法**：抽出公共 polling engine，workspace 与 export job 分别封装 hook，共用 generation token、单飞请求、退避、可见性暂停与错误上抛。使用一次请求完成后再 `setTimeout` 下一次，禁止 `setInterval` 导致慢请求重叠；安静刷新只禁止全页 loading，不得吞掉连续错误。
- **验收**：
  - 人为让一次请求耗时超过轮询间隔，网络面板中同一资源最大并发请求数始终为 1。
  - 连续 3 次失败后显示“连接中断，点击重试”，清除永久 loading；恢复后手动重试可继续。
  - 从任务 A 切到任务 B 时，A 的迟到响应不能覆盖 B；选中分镜、三栏滚动位置、本地待同步编辑不被轮询重置。
  - 完整视频 job 轮询失败后按钮恢复可点击，重新进入页面可从后端 job 状态继续，而非创建重复任务。

### R7 · finalize 失败循环

- **问题**：`_run_finalize_task` 失败后把任务原样放回 `awaiting_finalization`，错误只有一句字符串，用户可无限重复同一失败。
- **修法**：finalize 开始前再次用统一文件健康检查生成缺失清单。若是素材缺失，原子结束 finalize operation，任务转 `interrupted` 并返回 `retry_assets`；若素材齐全但草稿构建因磁盘/格式失败，保持独立 `finalize_failed` recovery，允许修复原因后重试。是否切换动作由错误分类决定，不以“连续两次”猜测根因。
- **验收**：
  - 删除一个素材文件后 finalize：0 次 LLM/生图/TTS 调用，错误点名分镜与资产类型，下一动作自动切到精确素材重试。
  - mock 草稿构建器失败：现有素材和旧可用草稿不删除，operation 标记失败，界面提供“重新完成生产”。
  - 重复点击 finalize 复用同一活跃 operation；成功后再次请求返回 already_ready，不创建第二份草稿。

### R8 · 后台巡检 sweeper

- **问题**：超时/卡死检测（`fail_stale_task_data`）只在有人轮询时懒惰触发；没人看的任务永远 `processing`。
- **修法**：通过 FastAPI lifespan 管理 sweeper 生命周期（默认 300 秒、测试可注入短间隔），执行 stale task 检查与孤儿 operation 清理。注意：`fail_stale_task_data()` 与 SQLite 清理均为同步阻塞调用，**不得放在裸协程中直接执行**（会阻塞事件循环）——扫描本体须放入线程（lifespan 启停的守护线程，或协程内 `run_in_executor`）。开发 `--reload` 下每个实际服务进程只存在一个 sweeper，shutdown 时显式取消并等待退出。
- **验收**：
  - 构造超时任务且不再请求 API，300 秒内自动转 `interrupted`，已完成资产仍可访问。
  - 同一 task/operation 连续执行两轮 sweeper 结果幂等；不会把 `awaiting_confirmation`、`awaiting_finalization` 当超时任务。
  - 应用关闭后无残留线程/协程；测试直接调用单轮 sweep，不依赖真实等待 5 分钟。

### R9 · 上传失败可见化

- **问题**：`LocalUploader.upload` 失败被记为 `error="Upload failed: …"` 但状态仍 `completed`，UI 显示成功。
- **修法**：统一“可用性”与“规范入库”两个维度。生成源文件仍存在且媒体端点可访问时，素材保持 `completed` 并增加 `storage_warning`；源文件与规范副本都不可用时才是 `failed`。前端以黄色“本地副本未归档”提示，不伪装为完全健康，也不阻塞即时预览。
- **验收**：
  - mock LocalUploader 失败但源文件存在：图片/音频仍可播放，状态 completed + warning，finalize 可使用该真实路径。
  - mock 上传失败且源文件也不存在：状态 failed，进入精确重试 targets。
  - warning 不覆盖真实生成错误；日志仍不包含凭证与用户音频数据。

### R10 · 补齐重试 API

- **问题**：a) 提示词失败后 `retry-assets` 返回 409 `needs_prompt`，但没有任何按钮能"只重生成这段提示词"；b) 换音色重配全片只能逐段调同步接口；c) `regenerate-image/audio`、`rebuild` 是无超时、无 operation 记录的同步接口，失败即 500。
- **修法**：
  1. 新增 `POST /tasks/{id}/segments/{i}/regenerate-prompt`（复用 `ImagePromptAgent` 单段调用）。
  2. 不新增含糊的 `audio_all` 特例；换全片音色后由后端返回准确的 stale audio targets，前端仍通过 `scope=selected + targets` 一次提交，确保单段例外音色不被误改。
  3. 旧同步 `regenerate-image/audio` 内部委托给精确 operation 执行器并逐步废弃，统一返回 202/operation；`rebuild` 委托 finalize/export job，所有长操作均有幂等记录、超时和恢复能力。
- **验收**：
  - 提示词失败时只重生成该段提示词，不调用生图/TTS；成功后该图片变为待生成，不自动消耗生图额度。
  - 换全片音色后一键只重配“跟随全片”的分镜，单段音色例外的路径、mtime、音色快照不变。
  - 旧 regenerate/rebuild 客户端仍能工作，但后端实际执行走 operation 且重复点击不创建并行任务。

### R11 · 创建前就绪检查

- **问题**：LLM/生图/TTS 未配置也能创建任务，用户等任务失败才知道去配置。
- **修法**：新增 `GET /config/readiness`，按当前任务实际所需 provider 返回 `ready | not_ready | unknown`、安全缺失项和设置页锚点。ManuscriptPage 提交前调用：本地配置明确缺失时阻止创建；仅网络探测失败/服务商暂不可达时允许用户确认继续，避免把瞬时网络问题误判为无配置。
- **验收**：
  - 清空 LLM key 后点击“继续制作”：不创建 task，弹窗说明缺失项并可直接打开对应设置。
  - 只关闭未被当前任务选用的 TTS provider 不阻塞；当前选中音色所属 provider 未就绪时准确阻塞。
  - readiness 自身超时返回 unknown，用户可继续；响应与日志不包含任何 secret。

---

## 2. Phase 2 — 用户引导（P1）

### G1 · 阶段导航器 + 全局进度

先提取并测试旧 `TaskProgress.jsx` / `utils/format.js` 中仍适用的纯计算逻辑，不直接复活旧页面组件。工作台顶部常驻步骤条：`写文稿 → 生成预案 → 确认音色 → 确认画面 → 生成素材 → 完成`，当前步高亮、附一句话说明与总体百分比。预计剩余时间必须基于已完成样本动态估算；样本不足时只显示区间或“正在估算”，禁止伪精确倒计时。
**验收**：所有合法 stage/planning_step/operation 组合映射唯一；失败或修复中步骤条不倒退丢失已完成步骤；0 分镜、单分镜和旧任务不出现 NaN/负数；Playwright 截图关键阶段。

### G2 · 统一术语表

先定表后改码：任务→**项目**，预案/生产预案/内容预案→**预案**，segment→**分镜**，素材（image/audio 统称）。全局扫 UI 文案替换，后端载荷字段不动。
**验收**：维护一份可机器检查的 UI 禁用词表；只扫描用户可见字符串，不误改 API 字段、日志、兼容路由或代码标识符；首页、资产页、工作台、设置、导出中心术语一致。

### G3 · 空状态与下一步指引

- 每个 stage 的操作条固定回答两件事："你现在在哪"（G1 步骤条）+"下一步做什么"（主按钮 + 一句话副文案，替换现在的术语式 stage note）。
- ManuscriptPage 空画布：营销轮播降级为次要位，主位改为"三步流程"引导卡。
- 首次进入工作台：一次性引导浮层（localStorage 记忆已读）。

**验收**：首次用户能在不打开文档的情况下说清当前阶段和下一主动作；引导可跳过、可从帮助入口重新打开，localStorage 不可用时不阻断主流程；各空态不提供无效按钮。

### G4 · Tooltip 补齐

给工作台关键控件加 `title`/轻量 tooltip：生成策略、快照/plan_version 冲突提示、重试范围、stale 标记含义、"离开本页不会中断"等。配合 Phase 3 抽象出 `Tooltip` 基础组件（先用原生 `title` 顶上）。

**验收**：鼠标悬停、键盘聚焦均能显示；Esc 可关闭；tooltip 不遮挡主操作且文案只解释概念，不承载必须阅读的错误信息。

### G5 · 清理装饰性控件

删除"受众人群"下拉框（`ManuscriptPage.jsx:224`），或接入真实参数上送 `text_style` 组合——默认删除，避免假承诺。

**验收**：控件与相关无效文案从 UI 删除，创建任务 payload、旧草稿恢复和现有风格参数不发生变化。

### G6 · 危险操作统一确认

`重新拆分`（`WorkspacePage.jsx:650`）与 `删除克隆音色`（`SettingsPage.jsx:538`）从 `window.confirm` 换成现有 `ConfirmDialog`，写明后果（"已生成素材将标记为待更新"）。

**验收**：确认框初始焦点、Tab 焦点陷阱、Esc/取消、提交中防重复点击全部正确；删除已引用音色仍遵守“隐藏而非删除文件”的生命周期规则。

### G7 · 错误文案规范

所有 toast/inline 错误从"裸 backend detail"改为「发生了什么 + 怎么办」两段式，依托 R3 的 error_code 映射表集中在一个 `errorMessages.js` 模块维护。
**验收**：常见失败路径（配置缺失、限流、超时、409 冲突）各有明确行动指引。

补充要求：同一错误在 toast、横幅、分镜卡、操作记录中使用同一标题；toast 只做短反馈，持久问题必须留在页面内，用户关闭 toast 后仍能恢复操作。

---

## 3. Phase 3 — UI 设计系统与视觉方向（P1.5）

### 3.1 历史视觉方向记录（已废止，不得继续实施）

> 2026-08-18 曾选择 B；该结论已被 2026-08-23 的 V2.0 决议覆盖。下表只保留为历史决策记录，衬线标题、暖纸编辑部和对应视觉资产均不得进入新页面或新组件。

| | A · 专业剪辑工具（深色） | B · 编辑部（浅色编辑排版） | C · Apple 风（激活 apple-design.css） |
|---|---|---|---|
| 参照 | DaVinci Resolve / 剪映专业版 | Linear / Notion / 编辑器类 SaaS | 仓库现成 token |
| 基调 | 深灰面板 `#1a1d24` 系 + 单一强调色，素材缩略图成为视觉主体 | 高对比浅色，衬线/半衬线标题 + 无衬线正文，大量留白与分栏 | 浅色 Apple 灰白 + SF 风格圆角/阴影 |
| 适合的叙事 | "这是生产工具"——凸显时间序、分镜流、素材密度 | "文稿驱动创作"——凸显文字、层级和阅读感，弱化机器感 | 通用精致，但方向感最弱 |
| 成本 | 高（全部页面换深色，需处理图片/表单对比度） | 中（保留浅色基底，重排层级与字体） | 低（token 现成，主要是接线） |
| 去"AI 味"效果 | 强 | 强 | 中 |
| **建议** | 若产品定位是"视频生产台"选 A | **若定位是"文稿到视频的创作工具"选 B（推荐，与产品名 InsightCut/文稿入口一致）** | 仅当追求最小成本 |

### 3.2 与方向无关的工程动作

1. **Token 层**：新建 `src/styles/tokens.css`（颜色/间距/字号/圆角/阴影/动效时长），将当前约 184 个不同硬编码 hex 逐步收敛为有限的语义 token；目标以主流程页面 100% 使用语义 token 为准，第三方/素材示例色可列白名单。`apple-design.css` 中可复用的结构并入后删除原文件。
2. **基础组件**：抽 `Button` / `Field` / `Card` / `Badge`(状态 chip) / `Tooltip` / `Skeleton` 到 `src/components/ui/`，替换各页面重复 JSX（`Field`/`Segmented` 目前私藏在 `SettingsPage.jsx` 内）。
3. **工作台信息架构整顿（不得改成两栏）**：保留已确认的三栏——左侧 `228–260px` 分镜导航，中间弹性宽度的作品预览/内容流，右侧约 `336px` 当前分镜与全片设置；顶部放 G1 步骤条，底部放阶段主操作。桌面端锁定应用视口，页面本身不滚动，三栏各自管理滚动；全文和分镜细节通过中栏层级/折叠降密度，不把预览塞进右侧检查器。
4. 状态色语义化：成功/警示/失败/进行中四色贯穿 chip、横幅、进度条。

**验收**：Playwright 在 320/768/1024/1440 四断点截图关键页面；桌面端页面无整体纵向/横向滚动、三栏滚动互不带动，窄屏切换“分镜/预览/设置”且无横向溢出；主流程颜色 lint 通过白名单规则；正文、控件、状态文本对比度满足 WCAG AA。

### 3.3 动效与微交互规范（M 系列）

> 原则：动效只用来**解释状态变化**，不做装饰。只动 `transform` / `opacity` / `clip-path`（合成器友好），不动 layout 属性；统一 token：`--duration-fast: 150ms`、`--duration-normal: 300ms`、`--ease-out: cubic-bezier(0.16,1,0.3,1)`；全站尊重 `prefers-reduced-motion`（现有 `app.css` 已有基础，扩展到新动效）。

| # | 场景 | 动效设计 | 说明 |
|---|---|---|---|
| M1 | 素材生成完成 | 分镜卡缩略图从 shimmer 骨架 **淡入 + 轻微 scale(0.98→1)** 揭示成品图 | 生成是产品的核心时刻，值得一个"揭晓"仪式感；逐段完成时依次触发，让用户看见流水线在动 |
| M2 | 生成中状态 | 骨架 shimmer（`linear-gradient` 位移动画）+ 状态 chip 呼吸（opacity 0.6↔1） | 替代静态"生成中"文字，区分"在动"和"卡死" |
| M3 | 失败聚合横幅（R4） | 出现时自顶部 **slide-down + fade**；重试后计数变化用数字滚动过渡 | 横幅是打断性信息，进入要有存在感，消失要轻 |
| M4 | 步骤条推进（G1） | 当前步高亮块 **横向位移过渡** 到下一步；完成步打勾用 `clip-path` 描画 | 让"阶段推进"可感知，替代瞬间跳变 |
| M5 | 分镜卡片操作 | hover 抬升（`translateY(-2px)` + 阴影加深）；选中态使用克制蓝色描边 | Spatial Pro 专业工具质感：克制、精确 |
| M6 | 保存状态 pill | "正在保存 → 已同步"用图标 morph + 颜色过渡，不闪跳 | 高频出现的元素，抖动最伤质感 |
| M7 | 检查器/预览切换 | 右栏 tab 切换用 **cross-fade**（150ms），不做滑动 | 编辑工具的切换要快于炫 |
| M8 | 弹窗/确认框 | `scale(0.96→1) + fade`（150ms）进入；遮罩 fade | 配合 T5 的焦点陷阱 |
| M9 | 列表增删（重新拆分后） | 新分镜卡依次 stagger 淡入（每张延迟 30ms） | 让"重拆"结果可扫读 |
| M10 | 错误 chip 出现 | 一次 `translateX` 微震（±3px，reduced-motion 下禁用） | 失败要被注意到，但只震一次 |

**实现约定**：优先 CSS transition/animation；需要编排的（M1/M9）用 `IntersectionObserver` + class 切换，不引入动画库；`will-change` 只在动画期间挂载。
**验收**：Playwright 录制 M1/M3/M4 关键动效帧；`prefers-reduced-motion: reduce` 下所有动效退化为瞬时切换且功能完整。

### 3.4 生图卡片与素材 UI 组件（C 系列）

> 生成的图片/音频是产品的核心产出，但现在只是"分镜卡里的一张缩略图"。这组组件把素材本身升级为一等公民 UI。后端能力大多已存在：`task_assets` 是 append-only 历史表，`POST …/segments/{i}/select-image` 可回选历史版本——差的是前端呈现。

**C1 · 生图卡片（ImageCard）** — 分镜卡内的核心组件，五种状态一体化设计：

| 状态 | 呈现 |
|---|---|
| 等待 | 浅色占位 + 比例框（按任务 ratio 16:9 / 9:16 / 3:4 撑开，杜绝布局跳动，CLS=0） |
| 生成中 | shimmer 骨架（M2）+ 已用时长 |
| 完成 | 图片淡入（M1）；hover 浮现操作层：**重新生成 / 上传替换 / 查看大图 / 历史版本** |
| 失败 | 比例框内显示错误分类图标 + 一句话原因（R3 的 error_code）+ 主按钮"重试" |
| stale（待更新） | 成品图上加半透明"待更新"斜角标 + 主按钮"更新此图"（配合 R1） |

**C2 · 图片历史版本抽屉（AssetHistory）** — 点击"历史版本"从卡片下方展开横向缩略图条：该分镜在 `task_assets` 中的所有 image 记录（generated / regenerated / upload 来源各有角标），点击即调 `select-image` 切换，当前使用项有墨色描边。让"重新生成"从赌博变成积累。

**C3 · 大图预览层（Lightbox）** — 点击图片全屏查看：左右键切换分镜、显示对应画面提示词、快捷操作（重新生成/替换）。专业工具风格：深色遮罩 + 半透明白色信息条。

**C4 · 音频卡片（AudioCard）** — 与 ImageCard 同构：波形占位（静态 SVG 波形即可）、播放/暂停、时长、音色名 chip、失败/stale 态同 C1；hover 操作：重新配音 / 换音色重配。

**C5 · 画面风格选择卡升级** — ManuscriptPage 的 6 张风格图改为统一卡片组件：选中态描边 + 打勾角标（M4 同款 clip-path），hover 放大预览，每种风格附一句话说明。

**C6 · 空态插画卡** — 各空状态（无项目、无素材、未配置）用统一的现代几何线描占位插画，替代纯文字空态。

**实现位置**：`src/components/ui/`（与 3.2 的基础组件同层），`ImageCard`/`AudioCard`/`AssetHistory`/`Lightbox` 各自独立文件；样式走 `tokens.css`。
**验收**：五状态 ImageCard 在 Storybook 式演示页或测试任务中逐一可见；历史版本切换后 workspace 载荷 `image_url` 正确更新；Playwright 截图 hover 操作层与 Lightbox。

---

## 4. 状态机、操作语义与系统不变量（P0 设计前置）

### 4.1 合法状态流转

> **适用范围**：本状态机适用于 `review_first` 任务（当前前端创建的所有新任务均为该模式）。`full` 旧任务不迁移状态、不套用本表（其真实路径为 `generating_assets → draft_building → ready`，不经过 `awaiting_finalization`）；对 full 旧任务只在恢复推导时做**只读映射**——按真实检查点映射到等价的 recovery 动作（见 R2），不改写其 `execution_mode` 与历史阶段记录。

| 当前阶段 | 触发条件 | 下一阶段 | 允许的模型调用 |
|---|---|---|---|
| `planning` | 文案、拆分、提示词全部完成 | `awaiting_confirmation` | 仅 LLM/提示词 |
| `planning` | 有提示词失败或服务中断 | `interrupted` | 停止后无调用；恢复仅补缺失提示词 |
| `awaiting_confirmation` | 用户确认音色和画面方案 | `generating_assets` | 生图 + TTS，按配置有界并发 |
| `generating_assets` | 全部素材完成 | `awaiting_finalization` | 后续无调用，等待用户确认 |
| `generating_assets` | 部分素材失败并已收敛 | `interrupted` | 恢复时只调用失败/缺失目标对应生成器 |
| `interrupted` | 用户继续预案 | `planning` | 只补缺失提示词 |
| `interrupted` | 用户重试/更新素材 | `repairing_assets` | 只调用精确 targets 对应生成器 |
| `repairing_assets` | 全部目标成功且素材齐全 | `awaiting_finalization` | 无 |
| `repairing_assets` | 仍有失败目标 | `interrupted` | 无，等待再次操作 |
| `awaiting_finalization` | 用户点击完成生产 | `finalizing` | 禁止 LLM/生图/TTS，只构建草稿包 |
| `finalizing` | 草稿构建成功 | `ready` | 无 |
| `finalizing` | 素材检查或构建失败 | `interrupted` | 无；按错误类型指向 repair 或 finalize retry |
| `ready` | 用户明确替换/更新某素材 | `repairing_assets` | 只调用明确目标；旧素材在成功前仍有效 |
| 任意运行态 | 取消/服务重启 | `interrupted` | 停止派发新请求，已完成检查点保留 |

`failed` 仅保留给无法通过当前产品操作恢复的数据损坏或未知系统错误；只要存在安全恢复路径，就使用 `interrupted + recovery`，避免“失败”既代表可恢复又代表不可恢复。

### 4.2 五种操作必须隔离

| 操作 | 输入范围 | 明确禁止 |
|---|---|---|
| 预案恢复 | 缺失/失败提示词 | 生图、TTS、草稿构建 |
| 失败素材重试 | 当前 failed/missing targets | stale、completed、提示词生成 |
| 待更新素材刷新 | 当前 stale targets | 未受影响 completed 素材、单段例外被全片覆盖 |
| 主动替换 | 用户选定的 completed target | 成功前覆盖旧素材 |
| finalize | 当前已完成且匹配快照的素材 | 任何模型调用、自动生成缺失素材 |

### 4.3 系统不变量

1. workspace 的按钮能力只由后端 `health + recovery + active_operation + capabilities` 推导，前端不得再用 stage 猜测素材是否真实存在。
2. 每个长操作都有 `operation_id`、幂等键、快照、逐目标状态和安全错误；同一项目同一时刻最多一个会改变素材/草稿的 operation。
3. `snapshot_key` 或 `expected_plan_version` 过期时返回 409，旧页面不得覆盖新内容；409 后保留本地待同步编辑并提示用户处理冲突。
4. completed 素材只有在用户明确替换且新文件成功后才原子切换；失败时路径、历史资产、mtime、数据库更新时间保持不变。
5. 文案、提示词、风格、比例、音色或 TTS 参数变化后，只标记受影响资产 stale，并立即令完整视频/MP4 失效；不自动消费额度。
6. 相对文件路径始终基于同一后端项目目录解析；“文件是否可用”必须由一个共享函数判断，健康检查、恢复、finalize、导出不得各自实现不同规则。
7. 离页、刷新、查看项目资产、打开 API 设置覆盖层不会取消后台操作，也不会丢失选中分镜、滚动位置和待同步编辑。

---

## 5. Phase 4 — 技术债（按依赖穿插，不先于可靠性清空代码）

| # | 事项 | 说明 | 前置于 |
|---|---|---|---|
| T0 | 冻结当前基线 | 先独立验收并提交当前精确重试改造，记录后端/前端测试基线和一个短任务 fixture；禁止与 Roadmap 新功能混提交 | 所有新改造 |
| T1 | 死代码审计与延后删除 | 先用 `rg`/路由测试证明无引用，再提取 `TaskProgress`、轮询纯逻辑与 token 候选；P0 可靠性完成后才删除旧页面/CSS/localStorage，兼容路由本身保留 | UI 收尾 |
| T2 | 拆分 `WorkspacePage.jsx` | 按已确认三栏拆为 `WorkspaceHeader` / `StoryboardNav` / `PreviewPane` / `SegmentInspector` / `ActionBar` + `useWorkspaceState`；每次只移动一个责任并保持行为测试通过 | Phase 3.3 |
| T3 | 轮询统一 | 新公共内核覆盖 workspace 与 export job：单飞、退避、错误上抛、generation token、页面可见性；旧 `usePolling` 只复用可验证的逻辑 | R6 |
| T4 | 合并重复分镜编辑器 | 卡片摘要与右侧检查器共享状态/保存逻辑，不强行共享不适合的展示 DOM | Phase 3 |
| T5 | ErrorBoundary + 焦点管理 | 顶层 ErrorBoundary 防白屏；Modal/设置 overlay/Lightbox 加焦点陷阱、焦点归还与 Esc 关闭 | — |
| T6 | 文档同步 | 更新 `frontend/README.md`：旧路由不是“删除”，而是兼容重定向；同步启动端口、工作流、导出语义 | 发布前 |
| T7 | 测试能力 | 在现有纯函数测试之外增加 React 渲染测试与 Playwright；测试依赖的引入单独提交，固定浏览器版本与 fixture | 各 Phase 验收 |

---

## 6. 实施批次、依赖与发布门禁

> 下列“会话”仅表示相对工作量，不是交付日期承诺；每一批必须独立可测试、可回退、可合并。

| 批次 | 内容 | 预计 | 合并门禁 |
|---|---|---|---|
| 0 | T0 基线冻结 + 状态机/fixture 测试骨架 | 1 次会话 | 当前全量测试与精确重试核心用例通过；无混入文档外改动 |
| 1 | R1 stale 恢复分流 + R2 full 兼容 + R7 finalize 分流 | 1–2 次会话 | A01–A06、A12、A13 通过；所有恢复态有唯一主动作 |
| 2 | T3 + R6 轮询可靠性 + R8 sweeper | 1 次会话 | A09–A11、A18 通过；无重叠轮询和永久 loading |
| 3 | R3 错误模型 + R5 部分失败 + R9 存储 warning | 1–2 次会话 | A07、A08、A17、A19 通过；错误脱敏扫描通过 |
| 4 | R4 失败横幅 + G7 文案 + R10 精确提示词/旧接口收口 + R11 readiness | 1–2 次会话 | A20、A23 + 关键失败 E2E；不再存在无动作错误页 |
| 5 | G1–G6 用户引导与无效控件清理 | 1–2 次会话 | 全阶段文案、键盘操作、冲突恢复验收通过 |
| 6 | token/基础组件 + T2/T4 渐进拆分 + B 视觉落地 | 2–3 次会话 | 四断点视觉回归、三栏滚动、WCAG AA 通过 |
| 7 | C 系列素材组件 + M 系列动效 | 1–2 次会话 | 五种素材状态、历史版本、reduced-motion 验收通过 |
| 8 | T1 最终删除 + T5/T6/T7 + 发布回归 | 1 次会话 | 全量自动化、真实短任务、迁移/回滚演练通过 |

### 关键依赖

- T0 是所有新工作的前置；未形成干净基线前不得开始视觉或结构重构。
- T3 是 R6 的前置，但不是 R1/R2/R7 的前置；修死胡同不应等待大组件重构。
- R3 是 G7 与完整 R4 的数据基础；R4 可先基于 targets 做计数，待 error_code 到位后增强行动文案。
- T2 必须在可靠性逻辑有测试保护后渐进执行；不得把 928 行组件一次性重写。
- C/M 系列依赖 token 和基础组件；V2.0 Spatial Pro 视觉不得改变第 0 节确认的三栏信息架构。

### 每批 Definition of Done

1. 本批需求逐项有自动化或可重复人工验收证据；不能只凭“页面看起来正常”。
2. 后端全量 `pytest`、前端 `npm test`、`npm run build` 通过；新增代码无未处理控制台错误。
3. 数据库迁移可在旧库重复启动且幂等，旧任务可打开；涉及 schema 时先备份测试库并完成降级说明。
4. 未修改目标以外 completed 资产；涉及重试的测试必须断言调用次数、路径、mtime 和 `updated_at`。
5. AGENTS.md 与 CLAUDE.md 若有项目约定变化必须同步；不涉及则不得顺手改写。
6. 每批独立提交，提交说明列出迁移、兼容性与回滚点；不得夹带下一批半成品。

---

## 7. 总体验收矩阵

| ID | 场景 | 可观察验收结果 | 自动化层级 |
|---|---|---|---|
| A01 | 29 段仅第 12 段缺图 | 仅 1 次生图、0 次 TTS/提示词；其余 57 条资产（58 条总数减去重生成的 1 条）路径、mtime、`updated_at` 不变 | 后端集成 |
| A02 | 失败图片 + 失败音频混合 | `scope=failed` 只解析失败/缺失 targets，按各自并发执行，单项失败不取消其他项 | 后端集成 |
| A03 | stale-only | 主动作是“更新受影响素材”而非“重试失败”；更新范围精确，旧完整视频立即过期 | 后端 + 前端 |
| A04 | 主动替换 completed 素材失败 | 原文件、当前选择、可用草稿均保留；失败 operation 可见 | 后端集成 |
| A05 | 素材齐全、草稿缺失 | 显示“完成生产并进入预览”；finalize 只构建草稿，模型调用总数为 0 | 后端 + E2E |
| A06 | full 旧任务四种检查点 | 无分镜/缺提示词/缺素材/缺草稿各有唯一正确恢复动作，旧链接可访问 | 后端 + E2E |
| A07 | LLM/生图/TTS 返回 401 | 停止对应新派发，保留已完成结果，提示检查具体配置；不盲重试 | 故障注入 |
| A08 | 429 与普通超时 | 429 遵循 Retry-After，普通失败按配置退避；一个 provider 不无故阻塞另一个 | 故障注入 |
| A09 | 工作台轮询时关闭后端 | 连续失败阈值后出现持久可恢复提示，无永久转圈；后端恢复后可续 | E2E |
| A10 | 完整视频 job 轮询失败 | loading 退出、出现重试；刷新后读取同一 job，不创建重复渲染 | 前端 + E2E |
| A11 | A/B 项目快速切换 | A 的迟到响应不覆盖 B；B 的选中分镜和本地编辑保持 | 前端集成 |
| A12 | 重复点击、刷新、离页返回 | 同一操作只有一个 operation_id；进度与 targets 可恢复 | 后端 + E2E |
| A13 | 旧 snapshot/plan_version | 返回 409；不覆盖新内容，本地待同步编辑仍可见并有明确处理动作 | 前后端集成 |
| A14 | 只生成部分素材时进入导出中心 | 可导出真实存在的素材包；不把不完整草稿或 MP4 标为可用 | E2E |
| A15 | 320/768/1024/1440 布局 | 无横向溢出；桌面三栏固定独立滚动；窄屏分区切换可操作 | 视觉 E2E |
| A16 | reduced-motion | shimmer、位移、微震退化为静态淡入/瞬时切换，功能和状态提示完整 | 视觉 E2E |
| A17 | 凭证/克隆音频异常 | 日志、SQLite、HTTP 响应不含 API Key、Authorization、DataURL | 安全测试 |
| A18 | 服务重启与 sweeper | processing 目标可重试，completed 不重复；等待确认/等待 finalize 不被误判超时 | 后端集成 |
| A19 | LocalUploader 失败 | 源文件存在时可预览并显示 storage warning；源文件不存在时才进入失败重试 | 后端 + 前端 |
| A20 | 创建任务前 readiness | 明确缺配置时不创建 task；unknown 可确认继续；只校验当前实际 provider | 前后端集成 |
| A21 | 完整连续播放 | 音频结束时画面同步切下一段；下一段未就绪则停止；自然播完后再次播放从第 1 段开始 | 前端 + E2E |
| A22 | 完整视频预览/导出复用 | 未点击不生成 MP4；生成时有可取消/可恢复状态；有效预览被正式导出复用，内容变化后立即失效 | 后端 + E2E |
| A23 | R10 精确重试通路 | 单段提示词重生成只调用 1 次 LLM、0 次生图/TTS，成功后该图片转待生成且不自动消耗生图额度；换全片音色后一键重配只覆盖"跟随全片"的分镜，单段例外音色的路径、mtime、音色快照不变 | 后端集成 |

### 测试与验收命令

- 后端：在 `ai-kepu-video-server/` 使用项目 venv 运行 `PYTHONPATH=. venv/bin/pytest -q`。
- 前端逻辑：在 `ai-kepu-video-web/frontend/` 运行 `npm test`。
- 前端构建：运行 `npm run build`。
- 浏览器：Playwright 使用固定 fixture 覆盖 A05、A06、A09–A16、A20–A22，并保存关键截图/视频；真实 Agnes/MiMo 只做 1–2 个短任务烟测，自动化主体使用可控 fake provider，避免消耗额度和受外部限流干扰。
- 静态检查：`git diff --check`；UI 可见文案禁用词扫描；敏感信息扫描；主流程硬编码颜色白名单扫描。

---

## 8. 成功指标、可观测性与发布策略

### 成功指标

当前项目没有完整产品埋点，因此不伪造历史基线。Phase 1 先记录以下本地聚合事件（不含文案、提示词、凭证或音频数据），再依据真实基线设目标：

- 每阶段耗时、provider 类型、分镜数、成功/失败/重试目标数。
- recovery action 展示次数、点击次数、operation 最终成功/部分失败/失败。
- 轮询连续失败次数、恢复次数、完整视频 job 重复创建拦截次数。
- 工作台出现“无 capability 且无 recovery”的死胡同次数（发布门禁目标固定为 0）。
- 精确重试误调用次数（自动化与发布烟测目标固定为 0）。

业务层“恢复成功率、平均恢复次数、任务完成率”的目标值在收集至少一个版本周期的本地匿名统计后再定，责任人为产品负责人；未获得用户授权前不上传遥测。

### 发布与回滚

1. 数据库只做向前兼容的增列/新表，不删除旧列；迁移前复制测试数据库演练，生产本地库由用户主动备份或工具创建带时间戳备份。
2. 可靠性批次优先发布，视觉批次后发；任何批次失败可回退代码而不要求回退用户资产文件。
3. 旧 `/production`、`/process`、`/preview` 保持重定向至少一个稳定版本；旧 regenerate/rebuild API 在兼容期内保留 wrapper 并记录弃用日志。
4. B 视觉上线前用同一组 fixture 做新旧截图对照；若关键操作缺失或三栏滚动回归，整批视觉改动回退，不影响已发布的 P0 状态机。
5. 发布烟测只用短文案：脚本模式 2–3 段、主题模式 2–3 段各一例；检查文案策略、音色确认、素材生成、单项失败重试、finalize、即时预览、完整视频与导出。

---

## 9. 决策、假设与待确认项

### 已记录决策

1. UI 视觉方向统一为 V2.0 Spatial Pro 白色专业工作台，三栏工作台结构不变。
2. G5“受众人群”控件按原文档记录删除，不新增无后端语义的表单项。
3. R5 不采用“失败率 >50%”阈值；改为错误分类驱动调度停止或继续。
4. stale 与 failed 永久分流；批量换音色用精确 targets，不新增 `audio_all` 模糊范围。

### 实施前待确认（不阻塞本轮计划成稿）

1. **V2.0 最终视觉样张**：以后续浏览器设计 QA 为准，确认无衬线字体、白色空间化基底、克制蓝色强调与信息密度；不得回退到暖纸或衬线方向。
2. **本地匿名统计**：是否允许记录第 8 节的纯技术事件到本地 SQLite；默认不上传、可清除。若不允许，则成功指标只以自动化和人工验收报告计算。
3. **旧 API 兼容期**：默认保留一个稳定版本；具体移除日期由下一次发布计划确定，在此之前 README 不宣称旧路由/API 已删除。
