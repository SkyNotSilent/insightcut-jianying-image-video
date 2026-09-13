# Contributing to InsightCut

欢迎修复问题、改善使用体验和文档。大功能请先开 Issue 讨论；一个 PR 解决一个问题。

## Fork → PR

1. 在 GitHub Fork 本仓库，然后克隆自己的 Fork。
2. 添加上游并同步（将 YOUR_USERNAME 换成你的账号）：

   ```sh
   git clone https://github.com/YOUR_USERNAME/insightcut-jianying-image-video.git
   cd insightcut-jianying-image-video
   git remote add upstream https://github.com/SkyNotSilent/insightcut-jianying-image-video.git
   git fetch upstream
   git switch -c fix/your-change upstream/master
   ```

3. 使用已绑定 GitHub 的邮箱或账号 noreply 邮箱提交。普通贡献者可用 `feature/`、`fix/`、`docs/`、`chore/` 分支；代理使用 `codex/`。
4. 按下面的步骤验证，提交后 `git push -u origin HEAD`。
5. 在 GitHub 创建 **上游仓库 master ← 你的 Fork 分支** 的 PR，填写模板。
6. 首次贡献可能显示 Awaiting approval：维护者检查代码后批准运行 CI。这不代表批准合并。后续修改继续推送同一分支即可。
7. 外部 PR 由维护者审查；最新提交的必需检查通过、讨论解决后合并。无需购买 API 服务或交付任何真实密钥。

## 开发环境

使用 Python 3.11、Node.js 22 和 npm。系统必须同时提供 `ffmpeg`、`ffprobe`；PDF 测试需要 `pdftotext`。`imageio-ffmpeg` 不能替代 ffprobe。

| 系统 | 系统工具 | 创建并启用后端环境 |
|---|---|---|
| macOS | `brew install python@3.11 node@22 ffmpeg poppler` | `python3.11 -m venv venv311`，`source venv311/bin/activate` |
| Ubuntu | 安装 Python 3.11、Node 22；`sudo apt-get install ffmpeg poppler-utils` | 同 macOS |
| Windows PowerShell | 安装 Python 3.11、Node 22；`winget install Gyan.FFmpeg`；用包管理器安装 Poppler 并加入 PATH | `py -3.11 -m venv venv311`，`.\venv311\Scripts\Activate.ps1` |

在 `ai-kepu-video-server` 中创建虚拟环境后运行：

```sh
python -m pip install -r requirements-dev.txt
ffmpeg -version
ffprobe -version
pdftotext -v
```

前端在 `ai-kepu-video-web/frontend` 中运行：

```sh
npm ci
npx playwright install chromium
```

日常后端：`python -m uvicorn api_server:app --host 127.0.0.1 --port 2002 --reload`；前端：`npm run dev`（2001）。只连接本机，不改变默认端口或开放公网监听。

## 提交前验证

以下命令分别在标明的目录运行，使用刚安装的虚拟环境。

| 目录 | 命令 |
|---|---|
| 仓库根 | `python scripts/check_readme_assets.py`，`git diff --check` |
| ai-kepu-video-server | `python -m compileall src api_server.py`，`python -m pytest -q` |
| ai-kepu-video-server | 导出相关修改运行 `python scripts/verify_local_flow.py` |
| ai-kepu-video-web/frontend | `npm run test:all`，`npm run build`，`npm run test:e2e` |
| ai-kepu-video-web/frontend | `npx playwright test --config=playwright.fullstack.config.js` |

全栈使用隔离数据库、假供应商和实际 PNG/WAV/FFmpeg。不要指向自己的数据库，不要传真实 API Key。测试配置中的独立端口不可被其他测试占用；若使用旧版测试配置，先停止 2001/2002 的开发服务。真实供应商音画质量和剪映客户端打开需单独注明是否验收。

UI 修改附桌面/手机截图；产物变化附压缩后的视频或结构验证结果。失败与取消必须保留已有内容，迁移注明备份和回退方法。

## 审查、安全与授权

- 不提交真实配置、密钥、数据库、原始生成输出或他人的个人信息。
- 安全问题按 [SECURITY.md](SECURITY.md) 私密报告。
- 提交贡献表示你有权提交相关内容，并同意其贡献以本项目 [MIT](LICENSE) 许可证提供。保留第三方许可证、版权与署名。
- MIT 覆盖本项目有权授权的软件；第三方商标、服务商 API、依赖及非自有媒体仍遵循其原有条款。
- 单维护者当前不要求其自己的 PR 获得第二人批准，但必须走 PR 和全部必需 CI。外部 PR 必须经过维护者审查。任何日常发布不得使用管理员绕过。
