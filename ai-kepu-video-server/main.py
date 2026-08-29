"""
InsightCut 主入口
用法: python main.py [主题]
示例: python main.py 人工智能的未来
"""

import argparse
import json
import sys
import logging
import os
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)

env_path = Path(__file__).parent / ".env"
if env_path.exists():
    try:
        from dotenv import load_dotenv
        load_dotenv(env_path)
    except ImportError:
        pass

from src.core.pipeline import VideoEditorPipeline


def _api_request(method: str, path: str, payload=None):
    base_url = os.getenv("INSIGHTCUT_API_BASE_URL", "http://127.0.0.1:2002").rstrip("/")
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = Request(
        f"{base_url}{path}",
        data=body,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        try:
            detail = json.loads(error.read().decode("utf-8")).get("detail")
        except (ValueError, AttributeError):
            detail = None
        raise RuntimeError(detail or f"API 请求失败（HTTP {error.code}）") from None
    except URLError:
        raise RuntimeError(f"无法连接 InsightCut API：{base_url}") from None


def _run_batch_cli(arguments) -> None:
    parser = argparse.ArgumentParser(prog="python main.py batch", description="批量创建预案")
    parser.add_argument("--file", required=True, help="每行一个主题的 UTF-8 文本文件")
    parser.add_argument("--concurrency", type=int, choices=(1, 2, 3), default=1)
    parser.add_argument("--no-wait", action="store_true")
    parser.add_argument("--style", default="温暖感人")
    parser.add_argument("--ratio", choices=("16:9", "9:16", "3:4"), default="16:9")
    parser.add_argument("--length", type=int, default=300)
    options = parser.parse_args(arguments)
    file_path = Path(options.file)
    if not file_path.is_file():
        raise RuntimeError(f"主题文件不存在：{file_path}")
    topics = [line.strip() for line in file_path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    payload = {
        "items": [{"theme": topic} for topic in topics],
        "concurrency": options.concurrency,
        "style": options.style,
        "ratio": options.ratio,
        "length": options.length,
    }
    batch = _api_request("POST", "/ai/native/video/kepu/batches", payload)
    batch_id = batch["batch_id"]
    print(f"[已创建] 批次 {batch_id}，共 {batch['total_count']} 个主题")
    if options.no_wait:
        return
    terminal = {"completed", "completed_with_errors", "cancelled"}
    last_summary = None
    while batch.get("status") not in terminal:
        counts = batch.get("counts") or {}
        summary = (
            batch.get("status"),
            counts.get("running", 0),
            counts.get("awaiting_confirmation", 0),
            counts.get("failed", 0),
        )
        if summary != last_summary:
            print(
                f"[{summary[0]}] 运行 {summary[1]} / 待确认 {summary[2]} / 失败 {summary[3]}"
            )
            last_summary = summary
        time.sleep(2)
        batch = _api_request("GET", f"/ai/native/video/kepu/batches/{batch_id}")
    counts = batch.get("counts") or {}
    print(
        f"[结束] {batch['status']}：待确认 {counts.get('awaiting_confirmation', 0)}，"
        f"失败 {counts.get('failed', 0)}，取消 {counts.get('cancelled', 0)}"
    )


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "batch":
        try:
            _run_batch_cli(sys.argv[2:])
        except RuntimeError as error:
            print(f"[失败] {error}", file=sys.stderr)
            raise SystemExit(1)
        return
    if len(sys.argv) > 1:
        theme = " ".join(sys.argv[1:])
    else:
        theme = input("请输入视频主题: ").strip()
        if not theme:
            theme = "人工智能的未来"

    pipeline = VideoEditorPipeline(theme)
    draft_path = pipeline.run()
    print(f"\n[完成] 草稿已生成: {draft_path}")
    if pipeline.mp4_path:
        print(f"[完成] MP4 已导出: {pipeline.mp4_path}")
    print("请打开剪映，在草稿列表中找到该草稿，手动导出视频。")


if __name__ == "__main__":
    main()
