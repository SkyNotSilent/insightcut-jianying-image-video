# InsightCut Frontend

基于 React 19 + React Router 7 + Vite 8 的前端工作台，用于创建、预览、编辑和导出图片视频项目。

## 当前工作流

1. 在文稿页输入主题或完整脚本，并选择画面风格与比例。
2. 创建项目后进入稳定地址 `/workspace/:taskId`；工作台会渐进展示完整文案、分镜和提示词。
3. 用户确认全片音色与画面方案后，才开始生成图片和逐段配音。
4. 确认后后台连续生成图片、配音和 MP4；页面关闭也继续推进。
5. 在导出中心选择 MP4、按顺序整理的分镜素材包，或剪映草稿。

项目失败、刷新、离页或服务重启不会清空已经保存的文案、提示词、图片、音频和草稿。失败素材重试、待更新素材刷新、主动替换和完成生产是互相独立的操作，不会隐式重新生成已完成素材。

## 本地开发

```bash
npm install
cp .env.example .env.development
npm run dev
```

开发服务器固定使用 `http://localhost:2001`。默认后端地址为 `http://localhost:2002`，可在 `.env.development` 中通过 `VITE_API_BASE_URL` 修改；前后端端口不能相同。

## 构建

```bash
npm test
npm run test:components
npm run build
```

浏览器烟测使用可控 fixture，不会连接真实 Agnes/MiMo：

```bash
# 使用 Playwright 管理的浏览器
npm run test:e2e

# 本机尚未安装匹配的 Playwright 浏览器时可使用 Chrome
PLAYWRIGHT_BROWSER_CHANNEL=chrome npm run test:e2e
```

构建产物输出到 `dist/`。

## 主要路由

- `/`、`/manuscript/:draftId?`：文稿准备
- `/workspace/:taskId`：预案、素材生成、即时预览与编辑一体化工作台
- `/export/:taskId`：结果导出
- `/assets`：项目资产
- `/settings`：模型配置
- `/result/:taskId`：兼容旧链接并重定向到导出页
- `/production/:draftId`、`/process/:taskId`、`/preview/:taskId`：兼容旧链接并重定向到文稿页或一体化工作台；这些地址并未删除

## 导出语义

- 明确确认预案后自动生成 MP4；剪映草稿按需导出。
- “完整视频预览”与 MP4 导出复用同一份有效渲染结果，内容变化后会立即标记过期。
- 完整视频生成可安全取消；已经开始的片段会收尾，上一份有效视频在新渲染完整通过前不会被覆盖。
- 分镜素材包可在部分素材存在时导出真实可用内容，并明确列出缺失项。
- 剪映草稿既可写入本机草稿目录，也可准备 ZIP 下载。
