"""
FastAPI 应用入口
"""

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

# 本地开发时加载 .env 文件
env_path = Path(__file__).parent / '.env'
if env_path.exists():
    try:
        from dotenv import load_dotenv
        load_dotenv(env_path)
        print(f"已加载环境变量文件: {env_path}")
    except ImportError:
        print("未安装 python-dotenv，跳过 .env 文件加载（可使用系统环境变量）")

from src.api.routes import router
from src.api.task_manager import task_manager
from src.api.task_sweeper import task_sweeper
from src.api.batch_manager import batch_scheduler
from src.api.error_model import (
    ErrorCode,
    make_safe_error,
    sanitize_http_detail,
    sanitize_persisted_error_text,
)
from src.utils.logger import setup_logging, get_logger
from src.config import Config

# 配置日志系统
setup_logging(log_dir="logs", log_level=Config.LOG_LEVEL)
logger = get_logger(__name__)


async def startup_event():
    """应用启动事件；保持可直接调用以兼容现有测试。"""
    deleted_count = await asyncio.to_thread(task_manager.complete_deleting_tasks)
    if deleted_count:
        logger.warning(f"启动时已完成 {deleted_count} 个待删除任务的清理")
    interrupted_count = await asyncio.to_thread(
        task_manager.mark_orphaned_tasks_interrupted
    )
    if interrupted_count:
        logger.warning(f"启动时已将 {interrupted_count} 个遗留任务标记为中断，可继续生成")
    reconciled_count = await asyncio.to_thread(task_manager.reconcile_completed_tasks)
    if reconciled_count:
        logger.warning(
            f"启动时已将 {reconciled_count} 个素材不完整的完成任务恢复为可修复状态"
        )
    logger.info("=" * 60)
    logger.info("InsightCut API 启动")
    logger.info("API 文档: http://127.0.0.1:2002/docs")
    logger.info("健康检查: http://127.0.0.1:2002/health")
    logger.info("=" * 60)


async def shutdown_event():
    """应用关闭事件；保持可直接调用以兼容现有测试。"""
    logger.info("API 服务正在关闭...")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Own exactly one sweeper for the lifetime of this worker process."""
    await startup_event()
    task_sweeper.start()
    batch_scheduler.start()
    try:
        yield
    finally:
        task_sweeper.stop()
        batch_scheduler.stop()
        stopped = await asyncio.to_thread(task_sweeper.join, 30.0)
        if not stopped:
            logger.warning("后台任务巡检线程未在 30 秒内停止")
        batch_stopped = await asyncio.to_thread(batch_scheduler.join, 30.0)
        if not batch_stopped:
            logger.warning("批量预案调度线程未在 30 秒内停止")
        await shutdown_event()


# 创建 FastAPI 应用
app = FastAPI(
    title="InsightCut API",
    description="批量生成认知科普视频、MP4 与剪映/CapCut 草稿",
    version="1.0.0",
    lifespan=lifespan,
)


@app.exception_handler(HTTPException)
async def structured_http_error(_request: Request, exc: HTTPException):
    """Preserve FastAPI's detail contract while adding stable safe error fields."""

    header_code = (exc.headers or {}).get("X-Error-Code")
    if header_code:
        code = header_code
    elif exc.status_code in {401, 403}:
        code = ErrorCode.AUTH.value
    elif exc.status_code == 409:
        code = ErrorCode.CONFLICT.value
    elif exc.status_code == 429:
        code = ErrorCode.RATE_LIMIT.value
    elif exc.status_code in {408, 504}:
        code = ErrorCode.TIMEOUT.value
    else:
        code = ErrorCode.UNKNOWN.value
    safe = make_safe_error(code, http_status=exc.status_code)
    if isinstance(exc.detail, (dict, list, tuple)):
        detail = sanitize_http_detail(exc.detail)
    elif isinstance(exc.detail, str):
        detail = sanitize_persisted_error_text(exc.detail)
    else:
        detail = safe.safe_message
    headers = {
        key: value
        for key, value in (exc.headers or {}).items()
        if key.lower() not in {
            "x-error-code",
            "authorization",
            "proxy-authorization",
            "cookie",
            "set-cookie",
        }
    }
    return JSONResponse(
        status_code=exc.status_code,
        headers=headers,
        content={
            "detail": detail or safe.safe_message,
            "error_code": safe.code.value,
            "error_meta": safe.metadata(),
        },
    )

# 配置 CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:2001", "http://127.0.0.1:2001"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由
app.include_router(router)

# 本地媒体文件路由。
# 这里按文件头识别 Content-Type，避免生图接口返回 PNG 但文件名为 .jpg 时被浏览器 ORB 拦截。
# 支持两个目录：output/（新任务）和 data/media/（旧任务）
output_dir = Config.BASE_DIR / "output"
legacy_media_dir = Config.BASE_DIR / "data" / "media"
output_dir.mkdir(parents=True, exist_ok=True)
legacy_media_dir.mkdir(parents=True, exist_ok=True)


def _media_type_for_file(path: Path) -> str:
    try:
        header = path.read_bytes()[:16]
    except OSError:
        header = b""

    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return "image/webp"
    if header.startswith(b"RIFF") and header[8:12] == b"WAVE":
        return "audio/wav"
    if header.startswith(b"\x1aE\xdf\xa3"):
        return "video/webm"
    if len(header) >= 12 and header[4:8] == b"ftyp":
        return "video/mp4"

    suffix_map = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".wav": "audio/wav",
        ".mp3": "audio/mpeg",
        ".mp4": "video/mp4",
    }
    return suffix_map.get(path.suffix.lower(), "application/octet-stream")


@app.api_route("/media/{file_path:path}", methods=["GET", "HEAD"], name="media")
async def serve_media(file_path: str):
    # 优先尝试 output 目录（新任务），然后尝试 data/media（旧任务）
    for media_dir in [output_dir, legacy_media_dir]:
        requested = (media_dir / file_path).resolve()
        media_root = media_dir.resolve()

        # 安全检查：确保请求的文件在允许的目录内
        if media_root not in requested.parents and requested != media_root:
            continue

        # 如果文件存在，返回它
        if requested.is_file():
            return FileResponse(
                requested,
                media_type=_media_type_for_file(requested),
                headers={
                    "Cache-Control": "public, max-age=31536000, immutable",
                },
            )

    # 两个目录都没找到文件
    raise HTTPException(status_code=404, detail="媒体文件不存在")


@app.get("/")
async def root():
    """根路径"""
    return {
        "message": "InsightCut API",
        "docs": "/docs",
        "version": "1.0.0"
    }


@app.get("/health")
async def health():
    """健康检查"""
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=2002)
