const copy = document.querySelector("#copy-command");
copy?.addEventListener("click", async () => {
  const status = document.querySelector("#copy-status");
  try {
    await navigator.clipboard.writeText(
      document.querySelector("#clone-command").textContent,
    );
    status.textContent = "已复制。按照安装文档继续配置环境。";
    copy.textContent = "已复制 ✓";
  } catch {
    status.textContent = "未能访问剪贴板，请选中上方命令手动复制。";
  }
});
// Play one example at a time; playback always starts with an explicit user action.
document.querySelectorAll("video").forEach((video) => {
  video.addEventListener("play", () => {
    document.querySelectorAll("video").forEach((other) => {
      if (other !== video) other.pause();
    });
  });
});
