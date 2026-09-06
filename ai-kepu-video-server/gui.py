"""
InsightCut 图形界面
双击 启动.bat 运行
"""

import json
import os
import sys
import threading
import subprocess
from pathlib import Path
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, filedialog

# 把项目目录加入 Python 路径
BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

GUI_SETTINGS = BASE_DIR / "data" / "gui.json"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("InsightCut")
        self.geometry("680x560")
        self.resizable(False, False)
        self.configure(bg="#1e1e2e")
        try:
            self.draft_root = Path(json.loads(GUI_SETTINGS.read_text()).get("draft_root") or BASE_DIR / "output")
        except (OSError, ValueError):
            self.draft_root = BASE_DIR / "output"
        self._build_ui()
        self._running = False

    # ── UI 构建 ────────────────────────────────────────────
    def _build_ui(self):
        BG = "#1e1e2e"
        PANEL = "#2a2a3e"
        ACCENT = "#7c5cbf"
        FG = "#e0e0f0"
        LOG_BG = "#13131f"

        # 标题
        tk.Label(self, text="InsightCut", bg=BG, fg=FG,
                 font=("微软雅黑", 16, "bold")).pack(pady=(18, 4))
        tk.Label(self, text="输入主题 → 自动生成可编辑视频草稿", bg=BG, fg="#888",
                 font=("微软雅黑", 10)).pack(pady=(0, 14))

        # 主题输入区
        frame_input = tk.Frame(self, bg=PANEL, bd=0)
        frame_input.pack(padx=24, fill="x")

        tk.Label(frame_input, text="视频主题（每行一个，支持批量）",
                 bg=PANEL, fg=FG, font=("微软雅黑", 10)).pack(anchor="w", padx=12, pady=(10, 4))

        self.txt_themes = tk.Text(frame_input, height=5, bg=LOG_BG, fg=FG,
                                  insertbackground=FG, font=("微软雅黑", 11),
                                  relief="flat", padx=8, pady=6, wrap="word")
        self.txt_themes.pack(padx=12, pady=(0, 12), fill="x")
        self.txt_themes.insert("1.0", "人工智能的未来")

        # 选项区
        frame_opts = tk.Frame(self, bg=BG)
        frame_opts.pack(padx=24, pady=8, fill="x")

        tk.Label(frame_opts, text="文章风格:", bg=BG, fg=FG,
                 font=("微软雅黑", 10)).grid(row=0, column=0, sticky="w")
        self.style_var = tk.StringVar(value="温暖感人")
        style_combo = ttk.Combobox(frame_opts, textvariable=self.style_var, width=12,
                                   values=["温暖感人", "励志向上", "科普知识", "幽默轻松", "深度思考"],
                                   state="readonly")
        style_combo.grid(row=0, column=1, padx=(6, 24), sticky="w")

        tk.Label(frame_opts, text="脚本字数:", bg=BG, fg=FG,
                 font=("微软雅黑", 10)).grid(row=0, column=2, sticky="w")
        self.length_var = tk.IntVar(value=300)
        length_spin = tk.Spinbox(frame_opts, from_=100, to=800, increment=50,
                                 textvariable=self.length_var, width=6,
                                 bg=LOG_BG, fg=FG, insertbackground=FG,
                                 buttonbackground=PANEL, relief="flat")
        length_spin.grid(row=0, column=3, padx=(6, 0), sticky="w")

        # 进度条
        self.progress = ttk.Progressbar(self, mode="indeterminate", length=632)
        self.progress.pack(padx=24, pady=(8, 0))

        # 状态标签
        self.lbl_status = tk.Label(self, text="就绪", bg=BG, fg="#aaa",
                                   font=("微软雅黑", 9))
        self.lbl_status.pack(pady=(4, 0))

        # 日志区
        self.log = scrolledtext.ScrolledText(self, height=10, bg=LOG_BG, fg="#b0ffb0",
                                             font=("Consolas", 9), relief="flat",
                                             state="disabled", wrap="word")
        self.log.pack(padx=24, pady=8, fill="both", expand=True)

        # 按钮区
        frame_btn = tk.Frame(self, bg=BG)
        frame_btn.pack(pady=(0, 16))

        self.btn_run = tk.Button(frame_btn, text="  开始生成  ", command=self._start,
                                 bg=ACCENT, fg="white", font=("微软雅黑", 11, "bold"),
                                 relief="flat", padx=18, pady=8, cursor="hand2",
                                 activebackground="#9370db", activeforeground="white")
        self.btn_run.pack(side="left", padx=8)

        self.btn_open = tk.Button(frame_btn, text="打开草稿文件夹", command=self._open_draft_folder,
                                  bg=PANEL, fg=FG, font=("微软雅黑", 10),
                                  relief="flat", padx=14, pady=8, cursor="hand2",
                                  activebackground="#3a3a5e", activeforeground=FG)
        self.btn_open.pack(side="left", padx=8)
        tk.Button(frame_btn, text="选择草稿位置", command=self._choose_draft_folder).pack(side="left", padx=8)

    # ── 日志写入 ───────────────────────────────────────────
    def _log(self, msg: str):
        self.log.configure(state="normal")
        self.log.insert("end", msg + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _status(self, msg: str):
        self.lbl_status.configure(text=msg)
        self.update_idletasks()

    # ── 开始生成 ───────────────────────────────────────────
    def _start(self):
        raw = self.txt_themes.get("1.0", "end").strip()
        themes = [t.strip() for t in raw.splitlines() if t.strip()]
        if not themes:
            messagebox.showwarning("提示", "请至少输入一个主题")
            return

        if self._running:
            return

        self._running = True
        self.btn_run.configure(state="disabled", text="  生成中...  ")
        self.progress.start(12)
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

        style = self.style_var.get()
        length = self.length_var.get()

        threading.Thread(target=self._run_pipeline,
                         args=(themes, style, length),
                         daemon=True).start()

    def _run_pipeline(self, themes: list, style: str, length: int):
        try:
            from src.core.pipeline import VideoEditorPipeline

            total = len(themes)
            for idx, theme in enumerate(themes, 1):
                self._log(f"\n{'='*50}")
                self._log(f"[{idx}/{total}] 开始生成: {theme}")
                self._status(f"正在生成 {idx}/{total}: {theme}")

                def log_redirect(msg):
                    self.after(0, self._log, msg)

                pipeline = VideoEditorPipeline(
                    theme=theme,
                    config_path=str(BASE_DIR / "config" / "settings.json"),
                    output_dir=str(BASE_DIR / "output"),
                )
                # 注入日志回调
                pipeline._log_callback = log_redirect

                draft_path = self._run_with_logging(pipeline, style, length)
                from src.export.draft_publication import publish_draft
                from src.api.routes import _normalize_draft_files_for_location, _draft_preflight, _server_target_os
                def prepare(staging, destination):
                    _normalize_draft_files_for_location(staging, str(destination.parent), destination.name, _server_target_os())
                def verify(path):
                    result = _draft_preflight(path, _server_target_os())
                    if not result["valid"]:
                        raise RuntimeError("；".join(result["issues"]))
                published = publish_draft(draft_path, self.draft_root, prepare=prepare, verify=verify)
                self._log(f"[完成] 草稿路径: {published['draft_path']}")
                if getattr(pipeline, "mp4_path", ""):
                    self._log(f"[完成] MP4 已导出: {pipeline.mp4_path}")

            self._log("\n所有主题生成完毕！请打开剪映查看草稿。")
            self.after(0, self._on_done, True)

        except Exception as e:
            self._log(f"\n[错误] {e}")
            import traceback
            self._log(traceback.format_exc())
            self.after(0, self._on_done, False)

    def _run_with_logging(self, pipeline, style: str, length: int) -> str:
        """运行流水线，同时把日志传给 GUI"""
        import logging

        class GUIHandler(logging.Handler):
            def __init__(self, callback):
                super().__init__()
                self.callback = callback

            def emit(self, record):
                self.callback(self.format(record))

        handler = GUIHandler(self._log)
        handler.setFormatter(logging.Formatter("%(message)s"))

        root_logger = logging.getLogger()
        root_logger.addHandler(handler)
        root_logger.setLevel(logging.INFO)

        try:
            return pipeline.run(style=style, length=length)
        finally:
            root_logger.removeHandler(handler)

    def _on_done(self, success: bool):
        self._running = False
        self.progress.stop()
        self.btn_run.configure(state="normal", text="  开始生成  ")
        if success:
            self._status("生成完成，请在剪映中查看草稿")
            self._open_draft_folder()
        else:
            self._status("生成失败，请查看日志")

    # ── 打开草稿文件夹 ─────────────────────────────────────
    def _choose_draft_folder(self):
        if self._running:
            return
        chosen = filedialog.askdirectory(title="选择剪映草稿位置", initialdir=str(self.draft_root))
        if chosen:
            self.draft_root = Path(chosen)
            GUI_SETTINGS.parent.mkdir(parents=True, exist_ok=True)
            GUI_SETTINGS.write_text(json.dumps({"draft_root": chosen}, ensure_ascii=False), encoding="utf-8")

    def _open_draft_folder(self):
        folder = self.draft_root
        if folder.exists():
            if os.name == "nt":
                os.startfile(str(folder))
            else:
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(folder)])
        else:
            messagebox.showinfo("提示", f"草稿目录不存在:\n{folder}")


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    app = App()
    app.mainloop()
