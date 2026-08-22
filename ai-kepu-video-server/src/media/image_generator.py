"""
图片生成模块
基于 OpenAI/兼容图像生成接口生成 AI 图像
"""

import base64
from collections import deque
import logging
import re
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

from src.config import Config
from src.api.error_model import (
    ClassifiedError,
    ErrorCode,
    classify_exception,
    make_safe_error,
)

logger = logging.getLogger(__name__)
_RATE_LIMIT_LOCK = threading.Lock()
_IMAGE_REQUEST_TIMESTAMPS = deque()
_IMAGE_RATE_LIMIT = 20
_IMAGE_RATE_WINDOW_SECONDS = 60.0

# 风格预设（附加到 prompt 末尾）
STYLE_PRESETS = {
    "写实风格":   "photorealistic, cinematic lighting, 4k, high detail",
    "电影质感":   "cinematic lighting, film still, dramatic composition, high detail",
    "电影胶片":   "film grain, cinematic, anamorphic lens, vintage color grading, 35mm film",
    "吉卜力":     "soft pastel colors, warm sunlight, peaceful, dreamy, Studio Ghibli inspired",
    "治愈系":     "soft pastel colors, warm sunlight, peaceful, dreamy, Studio Ghibli inspired",
    "3D动画":     "3D animated film style, stylized characters, soft lighting, detailed environment",
    "赛博朋克":   "cyberpunk, neon lights, rainy night, futuristic city, blade runner aesthetic",
    "国风":       "Chinese ink painting, traditional brush strokes, misty mountains, elegant, minimalist",
    "水墨国风":   "Chinese ink painting, traditional brush strokes, misty mountains, elegant, minimalist",
    "油彩画":     "oil painting, visible brush strokes, rich colors, painterly texture, gallery quality",
    "毛毡风":     "felt craft art style, handmade felt texture, wool fabric, cute flat illustration, soft tactile surface, warm pastel tones, cozy miniature diorama look",
}


class GeneratedImagePath(str):
    """String-compatible path carrying the prompt actually accepted upstream."""

    def __new__(
        cls,
        path: str,
        *,
        requested_prompt: str,
        submitted_prompt: str,
        fallback_used: bool,
    ):
        value = super().__new__(cls, path)
        value.requested_prompt = requested_prompt
        value.submitted_prompt = submitted_prompt
        value.fallback_used = bool(fallback_used)
        return value


class ImageGenerator:
    """图片生成器 - OpenAI/兼容 images generations 接口"""

    def __init__(self, output_dir: str = "output/images", generation_config: dict = None):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.image_config = Config.image_config()
        self.api_url = self.image_config.get("api_url") or Config.SEEDREAM_API_URL
        self.api_key = self.image_config.get("api_key") or Config.SEEDREAM_API_KEY
        self.model = self.image_config.get("model") or Config.SEEDREAM_MODEL
        self.size = self.image_config.get("size") or "auto"
        generation_config = generation_config or Config.generation_config()
        retry_count = generation_config.get("retry_count", 2)
        self.max_attempts = max(1, min(6, int(retry_count) + 1))
        self.retry_interval_seconds = max(
            1,
            min(60, int(generation_config.get("retry_interval_seconds", 5))),
        )

    def generate(
        self,
        prompt: str,
        index: int = 0,
        width: int = 1920,
        height: int = 1080,
        style: str = "写实风格",
        style_suffix: str = None,
        filename: str = None,
    ) -> str:
        """
        生成 AI 图像，返回本地路径。

        Args:
            prompt: 英文图像描述
            index: 片段序号（用于文件命名）
            width: 图片宽度
            height: 图片高度
            style: 风格预设
            style_suffix: 自定义风格 prompt 后缀，优先级高于 style 预设
            filename: 自定义文件名（不含扩展名），如果为 None 则使用 segment_{index:03d}
        """
        output_stem = filename or f"segment_{index:03d}"

        # 组合 prompt + 风格
        suffix = (style_suffix or "").strip() or STYLE_PRESETS.get(style, STYLE_PRESETS["写实风格"])
        full_prompt = f"{prompt}, {suffix}"

        if not self.api_key:
            raise ValueError("图像生成 API Key 未配置")

        # 尺寸映射（可配置，auto 时按宽高比选择常见兼容规格）
        size = self._pick_size(width, height)

        logger.debug(f"生成图像 [{index+1}]: {full_prompt[:60]}...")

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        payload = {
            "model": self.model,
            "prompt": full_prompt,
            "size": size,
        }
        if not self._is_openai_official_endpoint():
            # Agnes 等中转/兼容接口要求 response_format 放在 extra_body 中。
            payload["extra_body"] = {"response_format": "url"}
            payload["stream"] = False

        resp = None
        attempt = 0
        fallback_prompts = iter(self._content_policy_fallbacks(prompt, suffix))
        fallback_used = False
        while True:
            try:
                self._wait_for_rate_limit()
                resp = requests.post(self.api_url, headers=headers, json=payload, timeout=90)
                resp.raise_for_status()
                break
            except requests.HTTPError as e:
                if self._is_content_policy_rejection(resp):
                    try:
                        payload["prompt"] = next(fallback_prompts)
                    except StopIteration:
                        raise ClassifiedError(
                            make_safe_error(
                                ErrorCode.CONTENT_POLICY,
                                provider="agnes",
                                http_status=getattr(resp, "status_code", 400),
                                request_id=self._response_request_id(resp),
                            )
                        ) from None
                    fallback_used = True
                    logger.warning("图片提示词未通过内容检查，已改用安全表达重试")
                    continue
                safe = classify_exception(e, provider="agnes")
                if not safe.retryable or attempt >= self.max_attempts - 1:
                    raise ClassifiedError(safe) from None
                wait_seconds = self._retry_delay(resp, attempt)
                logger.warning(
                    "图像生成失败（第%s次），%.0f秒后重试",
                    attempt + 1,
                    wait_seconds,
                )
                time.sleep(wait_seconds)
                attempt += 1
            except Exception as e:
                safe = classify_exception(e, provider="agnes")
                if not safe.retryable or attempt >= self.max_attempts - 1:
                    raise ClassifiedError(safe) from None
                wait_seconds = self._retry_delay(None, attempt)
                logger.warning(
                    "图像生成失败（第%s次），%.0f秒后重试",
                    attempt + 1,
                    wait_seconds,
                )
                time.sleep(wait_seconds)
                attempt += 1
        data = resp.json()

        if not data.get("data"):
            raise ClassifiedError(
                make_safe_error(ErrorCode.PROVIDER_ERROR, provider="agnes")
            ) from None

        image_bytes = self._extract_image_bytes(data)
        output_path = self.output_dir / f"{output_stem}{self._detect_image_extension(image_bytes)}"
        output_path.write_bytes(image_bytes)
        logger.debug(f"图像保存: {output_path} ({len(image_bytes)} 字节)")
        return GeneratedImagePath(
            str(output_path),
            requested_prompt=full_prompt,
            submitted_prompt=str(payload["prompt"]),
            fallback_used=fallback_used,
        )

    @staticmethod
    def _response_request_id(resp) -> str:
        headers = getattr(resp, "headers", {}) or {}
        return str(
            headers.get("x-request-id")
            or headers.get("request-id")
            or headers.get("cf-ray")
            or ""
        )

    @staticmethod
    def _is_content_policy_rejection(resp) -> bool:
        if getattr(resp, "status_code", None) != 400:
            return False
        try:
            body = resp.json()
        except Exception:
            return False
        error = body.get("error") if isinstance(body, dict) else None
        if not isinstance(error, dict):
            return False
        code = str(error.get("code") or "").strip().lower()
        error_type = str(error.get("type") or "").strip().lower()
        return code in {
            "content_policy_violation",
            "content_filter",
            "prompt_rejected",
        } or error_type in {"content_policy_violation", "content_filter"}

    @staticmethod
    def _content_policy_fallbacks(prompt: str, suffix: str):
        """Return deterministic, model-free fallbacks for a rejected image prompt."""
        replacements = (
            (r"\bbroken\s+chains?\b", "a winding open path"),
            (r"\bchains?\b", "flowing ribbons"),
            (r"\btrauma(?:tic)?\b", "personal growth"),
            (r"\bblood(?:y)?\b", "deep crimson tones"),
            (r"\b(?:guns?|rifles?|pistols?|firearms?)\b", "everyday objects"),
            (r"\b(?:knives?|blades?|swords?)\b", "crafted objects"),
            (r"\b(?:dead|death|corpse|wound(?:ed)?|injur(?:y|ed))\b", "stillness"),
            (r"\b(?:violent|violence|attack|fighting|warfare)\b", "challenge"),
        )
        softened = str(prompt or "")
        for pattern, replacement in replacements:
            softened = re.sub(pattern, replacement, softened, flags=re.IGNORECASE)
        softened = re.sub(r"\s+", " ", softened).strip(" ,")
        first = (
            f"{softened}, calm positive educational scene, clear subject, warm natural light, {suffix}"
            if softened
            else ""
        )
        generic = (
            "A calm hopeful person moving forward through warm sunlight and blooming "
            "natural surroundings, symbolic personal growth and emotional resilience, "
            f"clear educational composition, {suffix}"
        )
        original = f"{prompt}, {suffix}".strip(" ,")
        seen = {original}
        for candidate in (first, generic):
            normalized = re.sub(r"\s+", " ", candidate).strip(" ,")
            if normalized and normalized not in seen:
                seen.add(normalized)
                yield normalized

    def _is_openai_official_endpoint(self) -> bool:
        parsed = urlparse(self.api_url)
        return parsed.netloc in {"api.openai.com", "api.openai.com:443"}

    def _wait_for_rate_limit(self) -> None:
        with _RATE_LIMIT_LOCK:
            while True:
                now = time.monotonic()
                while (
                    _IMAGE_REQUEST_TIMESTAMPS
                    and now - _IMAGE_REQUEST_TIMESTAMPS[0] >= _IMAGE_RATE_WINDOW_SECONDS
                ):
                    _IMAGE_REQUEST_TIMESTAMPS.popleft()
                if len(_IMAGE_REQUEST_TIMESTAMPS) < _IMAGE_RATE_LIMIT:
                    _IMAGE_REQUEST_TIMESTAMPS.append(now)
                    return
                wait_seconds = max(
                    0.01,
                    _IMAGE_RATE_WINDOW_SECONDS - (now - _IMAGE_REQUEST_TIMESTAMPS[0]),
                )
                time.sleep(wait_seconds)

    def _retry_delay(self, resp, attempt: int = 0) -> float:
        if resp is not None and resp.status_code == 429:
            retry_after = resp.headers.get("retry-after")
            try:
                return max(0.0, float(retry_after))
            except (TypeError, ValueError):
                return 60.0
        return float(self.retry_interval_seconds * (attempt + 1))

    def _extract_image_bytes(self, data: dict) -> bytes:
        item = data["data"][0]
        if item.get("url"):
            # 下载阶段沿用统一失败重试次数。
            for attempt in range(self.max_attempts):
                try:
                    img_resp = requests.get(item["url"], timeout=90)
                    img_resp.raise_for_status()
                    return img_resp.content
                except Exception as e:
                    if attempt == self.max_attempts - 1:
                        raise ClassifiedError(
                            classify_exception(e, provider="agnes")
                        ) from None
                    wait_seconds = self._retry_delay(None, attempt)
                    logger.warning(
                        "图片下载失败（第%s次），%.0f秒后重试",
                        attempt + 1,
                        wait_seconds,
                    )
                    time.sleep(wait_seconds)
        if item.get("b64_json"):
            return base64.b64decode(item["b64_json"])
        if item.get("base64"):
            return base64.b64decode(item["base64"])
        raise ClassifiedError(
            make_safe_error(ErrorCode.PROVIDER_ERROR, provider="agnes")
        ) from None

    def _detect_image_extension(self, image_bytes: bytes) -> str:
        if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
            return ".png"
        if image_bytes.startswith(b"\xff\xd8\xff"):
            return ".jpg"
        if image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
            return ".webp"
        return ".jpg"

    def _pick_size(self, width: int, height: int) -> str:
        """根据宽高比选择常见 OpenAI/兼容接口支持的 size 参数。"""
        if self.size and self.size != "auto":
            return self.size

        ratio = width / height
        model = (self.model or "").lower()
        if "dall-e-3" in model or "dalle-3" in model:
            if ratio >= 1.6:
                return "1792x1024"
            elif ratio <= 0.65:
                return "1024x1792"
            return "1024x1024"

        if ratio >= 1.6:
            return "1536x1024"
        elif ratio <= 0.65:
            return "1024x1536"
        else:
            return "1024x1024"


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    gen = ImageGenerator()
    path = gen.generate(
        prompt="A futuristic city with flying cars, golden hour, wide angle",
        index=0,
        style="电影胶片",
    )
    print(f"图片保存到: {path}")
