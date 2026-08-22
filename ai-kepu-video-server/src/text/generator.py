"""
文章生成模块
基于 LLM API 生成高质量视频脚本
"""

import json
import logging
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from threading import Lock
import time
from typing import Optional

from src.config import Config
from src.api.error_model import (
    ClassifiedError,
    ErrorCode,
    SafeError,
    classify_exception,
)
from src.text.provider_catalog import (
    get_provider,
    sanitize_provider_options,
    should_pass_base_url,
)

logger = logging.getLogger(__name__)


_LLM_THROTTLE_LOCK = Lock()
_LLM_NOT_BEFORE = 0.0
_LEGACY_SAFE_LLM_MESSAGE = "LLM API 调用失败，请检查模型配置或稍后重试"


def _error_status_code(error: Exception) -> Optional[int]:
    status = getattr(error, "status_code", None)
    if status is None:
        status = getattr(getattr(error, "response", None), "status_code", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def _retry_after_seconds(error: Exception) -> Optional[float]:
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None) or getattr(error, "headers", None) or {}
    value = headers.get("retry-after") or headers.get("Retry-After")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        try:
            retry_at = parsedate_to_datetime(str(value))
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


def _pause_llm_requests(seconds: float) -> None:
    global _LLM_NOT_BEFORE
    with _LLM_THROTTLE_LOCK:
        _LLM_NOT_BEFORE = max(_LLM_NOT_BEFORE, time.monotonic() + max(0.0, seconds))


def _wait_for_llm_throttle() -> None:
    while True:
        with _LLM_THROTTLE_LOCK:
            remaining = _LLM_NOT_BEFORE - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(remaining)


def _run_completion_with_retries(completion, kwargs, generation_config=None):
    """Use sensitive request state internally and return only safe values."""

    generation_config = generation_config or Config.generation_config()
    retry_count = int(generation_config.get("retry_count", 2))
    retry_interval = max(
        1, min(60, int(generation_config.get("retry_interval_seconds", 5)))
    )
    max_attempts = max(1, min(6, retry_count + 1))
    for attempt in range(max_attempts):
        _wait_for_llm_throttle()
        try:
            response = completion(**kwargs)
            return response.choices[0].message.content, None
        except Exception as error:
            safe_error = classify_exception(error, provider="llm")
            if safe_error.code is ErrorCode.UNKNOWN:
                safe_error = SafeError(
                    code=ErrorCode.PROVIDER_ERROR,
                    retryable=True,
                    safe_message=_LEGACY_SAFE_LLM_MESSAGE,
                    provider="llm",
                )
            is_rate_limited = _error_status_code(error) == 429
            wait_time = (attempt + 1) * retry_interval
            if is_rate_limited:
                wait_time = max(wait_time, _retry_after_seconds(error) or 0)
                _pause_llm_requests(wait_time)
            if attempt == max_attempts - 1:
                logger.error("API 调用失败，已尝试 %s 次", max_attempts)
                error = None
                return None, safe_error
            logger.warning(
                "API 调用失败（第 %s 次），%s 秒后重试",
                attempt + 1,
                wait_time,
            )
            _wait_for_llm_throttle() if is_rate_limited else time.sleep(wait_time)


def _raise_safe_llm_failure(safe_error):
    raise ClassifiedError(safe_error) from None


class ArticleGenerator:
    """文章生成器 - 基于 LLM API"""

    def __init__(
        self,
        config_path: str = "config/prompts/article_generation.json",
        generation_config: dict = None,
    ):
        self.config_path = Path(config_path)
        self.generation_config = generation_config or Config.generation_config()
        self.prompt_config = self._load_config()
        self.llm_config = Config.llm_config()
        self.provider = self.llm_config.get("provider") or "custom"
        self.protocol = (self.llm_config.get("protocol") or "openai").lower()
        self.api_key = self.llm_config.get("api_key") or ""
        self.model = self.llm_config.get("model") or Config.LLM_MODEL or Config.ANTHROPIC_MODEL
        self.base_url = (self.llm_config.get("base_url") or "").rstrip("/")
        raw_provider_options = self.llm_config.get("provider_options") or {}
        self.provider_options = (
            raw_provider_options if isinstance(raw_provider_options, dict) else {}
        )

        provider_config = get_provider(self.provider)
        credential_fields = (
            provider_config.get("credential_fields", []) if provider_config else []
        )
        requires_api_key = provider_config is None or any(
            field.get("id") == "api_key" and field.get("required")
            for field in credential_fields
        )
        if requires_api_key and not self.api_key:
            raise ValueError("LLM API Key 未配置")

    def _load_config(self) -> dict:
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {
                "system": "你是一位专业的短视频脚本作家，擅长创作引人入胜、情感丰富的内容。",
                "user": "请为主题「{theme}」写一篇约{length}字的短视频旁白脚本。风格：{style}。要求：每个自然段只有1-2句话，语言口语化，适合配音朗读。直接输出脚本正文，不要标题和说明。",
            }

    def _build_litellm_model(self) -> str:
        """根据协议和模型名构建 LiteLLM 的 model 参数。"""
        model = self.model
        if "/" in model or self.provider != "custom":
            return model
        if self.protocol == "anthropic":
            return f"anthropic/{model}"
        return f"openai/{model}"

    def _build_completion_kwargs(self, messages: list, max_tokens: int) -> dict:
        """构建传给 LiteLLM 的 provider-aware completion 参数。"""
        kwargs = {
            "model": self._build_litellm_model(),
            "messages": messages,
            "max_tokens": max_tokens,
            "timeout": 90,
        }
        if self.api_key:
            kwargs["api_key"] = self.api_key
        if self.base_url and should_pass_base_url(self.provider, self.base_url):
            kwargs["api_base"] = self.base_url
        kwargs.update(
            sanitize_provider_options(self.provider, self.provider_options)
        )
        return kwargs

    def _call_api(self, messages: list, max_tokens: int = 2048) -> str:
        import litellm

        kwargs = self._build_completion_kwargs(messages, max_tokens)
        content, failure = _run_completion_with_retries(
            litellm.completion, kwargs, self.generation_config
        )

        # The public exception traceback includes this frame. Clear every
        # credential-bearing reference before crossing that boundary.
        self = None
        messages = None
        max_tokens = None
        litellm = None
        kwargs = None
        if failure is not None:
            content = None
            _raise_safe_llm_failure(failure)
        return content

    def _extract_text(self, data: dict) -> str:
        """兼容 Anthropic Messages 与 OpenAI Chat Completions 的常见返回格式。"""
        if self.protocol == "openai":
            choice = (data.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            content = message.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                parts = []
                for item in content:
                    if isinstance(item, dict):
                        parts.append(item.get("text") or item.get("content") or "")
                    elif isinstance(item, str):
                        parts.append(item)
                text = "".join(parts).strip()
                if text:
                    return text
            if choice.get("text"):
                return choice["text"]
            raise RuntimeError(f"OpenAI 兼容接口返回缺少文本内容: {data}")

        content = data.get("content")
        if isinstance(content, list):
            text = "".join(
                item.get("text", "") if isinstance(item, dict) else str(item)
                for item in content
            ).strip()
            if text:
                return text
        if isinstance(content, str):
            return content
        raise RuntimeError(f"Anthropic 兼容接口返回缺少文本内容: {data}")

    def generate(
        self,
        theme: str,
        length: int = 300,
        style: str = "温暖感人",
        platform: str = "短视频平台",
    ) -> str:
        logger.info(f"生成文章: 主题={theme}, 长度={length}")

        system_prompt = self.prompt_config.get("system", "")
        user_prompt = self.prompt_config.get("user", "").format(
            theme=theme, length=length, style=style, platform=platform
        )

        combined_prompt = f"{system_prompt}\n\n{user_prompt}" if system_prompt else user_prompt

        article = None
        failure = None
        try:
            article = self._call_api(
                messages=[{"role": "user", "content": combined_prompt}],
                max_tokens=2048,
            )
        except ClassifiedError as error:
            failure = error.safe_error
        if failure is not None:
            self = None
            article = None
            _raise_safe_llm_failure(failure)
        logger.info(f"文章生成成功，长度: {len(article)} 字")
        return article

    def generate_image_prompts(self, segments: list, style: str = "写实风格") -> list:
        """为每个文本段落生成对应的英文图像 prompt"""
        logger.info(f"生成图像 prompts，共 {len(segments)} 段")

        segments_text = "\n".join(f"{i+1}. {s}" for i, s in enumerate(segments))
        user_prompt = f"""以下是一个短视频的分段旁白文本，请为每段生成一个简洁的英文图像描述（image prompt），用于 AI 图像生成。

要求：
- 每段对应一个 prompt，直接描述画面内容
- 英文输出，不超过 20 个单词
- 风格参考：{style}
- 只输出 prompt 列表，每行一个，不要序号和解释

旁白分段：
{segments_text}"""

        text = None
        failure = None
        try:
            text = self._call_api(
                messages=[{"role": "user", "content": user_prompt}],
                max_tokens=1024,
            )
        except ClassifiedError as error:
            failure = error.safe_error
        if failure is not None:
            self = None
            text = None
            _raise_safe_llm_failure(failure)

        lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
        while len(lines) < len(segments):
            lines.append(f"cinematic scene related to: {segments[len(lines)][:30]}")
        return lines[:len(segments)]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    gen = ArticleGenerator()
    print(gen.generate("人工智能的未来"))
