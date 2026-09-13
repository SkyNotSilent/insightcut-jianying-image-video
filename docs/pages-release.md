# Pages 发布、检查与回退

## 公开站点与本地应用

[InsightCut 网站](https://skynotsilent.github.io/insightcut-jianying-image-video/)提供产品介绍、真实案例和文档。FastAPI / React 工作台仍在本机运行，端口为 2002 / 2001；发布网站不会部署用户数据库、媒体目录或模型凭证。

## 贡献到发布的路径

1. 从 `master` 建分支并提交 PR。外部贡献者可 Fork；第一次贡献的 CI 可能需要维护者批准运行。CI 批准与代码审查是两件事。
2. 8 项必需检查全部通过，所有讨论解决后才能合并。管理员同样受保护；单维护者场景不要求另一个批准者，外部 PR 仍由维护者审查后合并。
3. 合并后，`CI` 再检查实际的 `master` 提交，构建静态站并上传 `pages-site`。
4. `Publish Pages` 只接受本仓库、`master`、push 事件产生的成功 CI。门禁检查每一项 CI 均成功，以及提交仍为最新 `master`。
5. 仅下载该次 CI 的站点文件；发布前再次核对 `build-info.json` 中的 SHA 和当前 `master`。特权部署步骤不执行产物中的代码。
6. 发布后自动读取公开首页、关键文档、品牌资产、截图、视频 Range 和 `build-info.json`，验证部署内容对应提交。

PR、失败或取消的 CI、手动调度的 CI、过期 `master` 运行都不能发布。Pages 运行串行处理，正在发布的运行不被中途取消。

## 仓库设置

- Pages → Build and deployment → Source：**GitHub Actions**，不再使用 `master:/docs` 的独立 Jekyll 发布。
- `github-pages` environment 只允许 `master` 部署。
- Actions 默认 token 为只读。只有发布 job 授予 `pages: write` 和 `id-token: write`。
- 必需检查名称与[GitHub 设置](github-settings.md)一致；修改名称时同时更新分支保护和 `scripts/pages_gate.py`。
- 已开启秘密扫描、推送保护、Dependabot 安全更新和私密漏洞反馈。常规依赖更新每周建 PR，不自动合并。

## 本地验证

先安装 `requirements-dev.lock` 和前端锁定依赖。项目根目录：

```sh
python scripts/check_contribution.py
python scripts/test_pages_gate.py
python scripts/build_site.py
python scripts/check_site.py
```

前端目录：

```sh
npx playwright test --config=playwright.site.config.js
```

新增文档或素材须先 `git add` 才会进入构建。只复制 Git 登记的公开文件，不遍历用户数据目录；保留 `/showcase/` 和原有 Markdown 对应的 `.html` 地址。

## 如何判断真的上线

以 [Publish Pages 运行结果](https://github.com/SkyNotSilent/insightcut-jianying-image-video/actions/workflows/pages.yml)、公开网页和 [build-info.json](https://skynotsilent.github.io/insightcut-jianying-image-video/build-info.json) 为准。CI 通过、分支推送或旧站点返回 200 都不足以证明新版本已发布。

若 `master` 在部署前再次更新，旧运行会跳过或停止发布，等待新提交 CI 成功。网络或 Pages 平台故障可在 Actions 重跑对应 `Publish Pages`；门禁会重新验证原 CI 和最新 SHA，过期版本仍不会发布。

## 故障证据与回退

- CI 中的 `full-stack-evidence` 包含完整媒体摘要与失败诊断，`website-evidence` 包含 390/768/1440 的截图和失败 trace，保存 **7 天**。需要长期排查时及时下载并脱敏。
- 修复与回退都通过新分支、PR、CI、合并完成。可在新分支执行 `git revert <需要撤回的提交>`，回退后的新提交重新生成站点；不要直接推送或强制重置 `master`。
- 发布发生错误时，已有站点继续保留。不要为绕过红色 CI 重新启用独立的 `master:/docs` 发布。
- 本轮未变动本机 Python 服务环境、真实媒体与数据库，无需用户数据迁移。

## 本轮验证记录（2026-09-13）

- [治理与 MIT PR #11](https://github.com/SkyNotSilent/insightcut-jianying-image-video/pull/11)、[CI PR #12](https://github.com/SkyNotSilent/insightcut-jianying-image-video/pull/12)：已通过检查并合并。
- [CI 运行 34730811650](https://github.com/SkyNotSilent/insightcut-jianying-image-video/actions/runs/34730811650)：后端 426、前端逻辑 139、组件 43、浏览器 7、真实后端 E2E 6 通过；Windows/macOS 文件保护子集通过。完整输出经 ffprobe 检查为 1920×1080、约 1.043 秒并带音轨，涵盖 MP4、SRT、VTT、两类素材包、草稿及副本/备份。
- [网站 PR #22](https://github.com/SkyNotSilent/insightcut-jianying-image-video/pull/22)执行过一次有意失败演练：[运行 34731411209](https://github.com/SkyNotSilent/insightcut-jianying-image-video/actions/runs/34731411209)。网站检查为失败、非草稿 PR 的 `mergeStateStatus=BLOCKED`；截图、错误上下文及 trace 均成功下载，trace ZIP 完整。临时失败探针随后删除，再执行正常检查。
- 本地网站验收：390 / 768 / 1440 宽度无横向溢出，图片实际加载，视频播放推进和拖动正常，单次只播放一个案例；键盘、复制命令及原有文档地址通过。
- 未验收：另一位真实外部贡献者从 Fork 首次提交的完整操作；真实模型服务商的当前质量；真实剪映客户端导入。前者已检查仓库批准策略和无凭证 CI 配置，后二者不由本轮静态网站与假服务商测试代替。
