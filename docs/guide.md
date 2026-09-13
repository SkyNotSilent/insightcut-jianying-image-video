# 开始使用 InsightCut

InsightCut 在自己的电脑上运行。这里是介绍与文档网站；启动本地服务后，在浏览器打开工作台。

## 准备环境

- Python **3.11**、Node.js **22** 与 npm。
- 系统 PATH 中的 **FFmpeg** 和 **ffprobe**。PDF 文稿提取另需 Poppler 的 **pdftotext**。
- 用于生成文字、图片和配音的服务商账号与凭证。项目本身使用 MIT 许可证，模型调用可能产生服务商费用。

先验证工具可用：

```sh
node --version
ffmpeg -version
ffprobe -version
```

macOS 可使用 Homebrew 安装 FFmpeg 和 Poppler。Windows 可使用对应项目的安装包并将工具目录加入 PATH。更详细的依赖与测试说明见[贡献指南](../CONTRIBUTING.md)。

## 1. 获取源码

```sh
git clone https://github.com/SkyNotSilent/insightcut-jianying-image-video.git
cd insightcut-jianying-image-video
```

## 2. 配置后端

在项目根目录，macOS / Linux 运行：

```sh
cd ai-kepu-video-server
python3.11 -m venv venv311
source venv311/bin/activate
python -m pip install -r requirements-dev.lock --require-hashes
cp .env.example .env
```

Windows PowerShell 运行：

```powershell
cd ai-kepu-video-server
py -3.11 -m venv venv311
.\venv311\Scripts\python.exe -m pip install -r requirements-dev.lock --require-hashes
Copy-Item .env.example .env
```

新安装时复制配置示例即可。已有 `.env` 请继续使用，避免覆盖自己的设置。

## 3. 启动服务

在后端目录启动，仅监听本机：

macOS / Linux（已激活虚拟环境）：

```sh
python -m uvicorn api_server:app --host 127.0.0.1 --port 2002 --reload
```

Windows PowerShell：

```powershell
.\venv311\Scripts\python.exe -m uvicorn api_server:app --host 127.0.0.1 --port 2002 --reload
```

再开一个终端，从项目根目录运行：

```sh
cd ai-kepu-video-web/frontend
npm ci
npm run dev
```

打开 [本地工作台](http://localhost:2001)。后端健康检查位于 [localhost:2002/health](http://localhost:2002/health)。这两个地址只在你已启动本地服务时可用。

## 4. 连接模型服务商

打开工作台的“设置”：

1. 配置生文服务商、凭证与模型。
2. 配置生图服务商与凭证。
3. 启用豆包、MiMo 或两者，选择开放音色和配音参数。
4. 保存配置。需要克隆声音时，先确认声音授权、上传参考录音并完成试听。

项目与素材保存在本机；生成所需文本或参考素材会发送给所选服务商。不要在 Issue、日志截图或代码中分享 API Key。

## 5. 完成第一条视频

输入主题，或粘贴完整文稿 → 生成预案 → 按需预览和编辑 → **确认并生成视频**。

确认后，图片、配音与 MP4 会在后台连续推进。预览和试听可选；剪映草稿只在请求导出时构建。失败时，先检查具体阶段的错误，已有素材继续保留。

批量预案支持同批 **2–50** 个主题或完整文稿，同时运行数 **1–10**；FFmpeg 视频渲染单独排队。参见[批量预案与成片流程](batch-video-workflow.md)。

## 6. 导出并继续剪辑

成片可以下载为 MP4，也可以在导出页请求剪映 / CapCut 草稿，选择自己习惯的本机目录。同名草稿默认另存副本；选择替换时保留备份。

剪映版本差异可能影响导入，请在客户端实际打开确认。项目与剪映、CapCut 无隶属或合作关系。备份与恢复说明见[内容保护与可靠性](reliability-remediation.md)。

## 遇到问题

- [公开问题反馈](https://github.com/SkyNotSilent/insightcut-jianying-image-video/issues)：附上复现步骤、系统版本和脱敏后的错误。
- [安全问题反馈](../SECURITY.md)：敏感内容走私密渠道。
- [参与贡献](../CONTRIBUTING.md)：Fork、分支、测试和 PR 的完整流程。
