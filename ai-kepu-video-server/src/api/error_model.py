"""Credential-safe, stable error classification shared by backend layers.

The classifier deliberately never copies ``str(exception)`` or response bodies into
its output.  Provider implementations may inspect the returned structure and store
only ``error_code`` plus ``error_meta`` at persistence boundaries.
"""

from __future__ import annotations

import asyncio
import errno
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from enum import Enum
from typing import Any, Dict, Mapping, Optional, Sequence


class ErrorCode(str, Enum):
    AUTH = "auth"
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    PROVIDER_ERROR = "provider_error"
    CONTENT_POLICY = "content_policy"
    NETWORK = "network"
    DISK = "disk"
    CONFIG_MISSING = "config_missing"
    CONFLICT = "conflict"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


SAFE_MESSAGES = {
    ErrorCode.AUTH: "服务凭证无效，请检查 API 配置后重试。",
    ErrorCode.RATE_LIMIT: "服务请求过于频繁，请稍后重试。",
    ErrorCode.TIMEOUT: "服务响应超时，请检查网络后重试。",
    ErrorCode.PROVIDER_ERROR: "生成服务暂时异常，请稍后重试。",
    ErrorCode.CONTENT_POLICY: "图片提示词未通过服务商内容检查，请修改提示词后重试。",
    ErrorCode.NETWORK: "无法连接生成服务，请检查网络后重试。",
    ErrorCode.DISK: "本地文件写入失败，请检查磁盘空间和目录权限。",
    ErrorCode.CONFIG_MISSING: "生成服务尚未配置完整，请先完成 API 配置。",
    ErrorCode.CONFLICT: "内容已在其他页面更新，请刷新后重试。",
    ErrorCode.CANCELLED: "本次操作已取消。",
    ErrorCode.UNKNOWN: "处理未完成，请稍后重试或检查配置。",
}

_DEFAULT_RETRYABLE = {
    ErrorCode.AUTH: False,
    ErrorCode.RATE_LIMIT: True,
    ErrorCode.TIMEOUT: True,
    ErrorCode.PROVIDER_ERROR: True,
    ErrorCode.CONTENT_POLICY: False,
    ErrorCode.NETWORK: True,
    ErrorCode.DISK: False,
    ErrorCode.CONFIG_MISSING: False,
    ErrorCode.CONFLICT: False,
    ErrorCode.CANCELLED: False,
    ErrorCode.UNKNOWN: False,
}

_DATA_URL_RE = re.compile(
    r"data:[^\s,;]+(?:;[^\s,]+)*;base64,[A-Za-z0-9+/=_-]+",
    re.IGNORECASE,
)
_AUTHORIZATION_RE = re.compile(
    r"\bauthorization\s*[:=]\s*(?:bearer\s+)?[^\s,;\]}]+",
    re.IGNORECASE,
)
_BEARER_RE = re.compile(r"\bbearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
_NAMED_SECRET_RE = re.compile(
    r"(?P<label>\b(?:api[_-]?key|access[_-]?token|secret|token)\b\s*[:=]\s*)"
    r"(?P<quote>['\"]?)[^\s,;\]}]+(?P=quote)",
    re.IGNORECASE,
)
_OPENAI_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b", re.IGNORECASE)
_SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_SENSITIVE_FIELD_RE = re.compile(
    r"(?:authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"client[_-]?secret|password|credential|secret)",
    re.IGNORECASE,
)


def sanitize_text(value: Any, *, max_length: int = 1000) -> str:
    """Redact common credential and inline binary forms from diagnostic text.

    This helper is suitable for already-approved diagnostic snippets.  The error
    classifier itself is stricter and never emits the exception message at all.
    """

    text = str(value or "")
    text = _DATA_URL_RE.sub("[REDACTED]", text)
    text = _AUTHORIZATION_RE.sub("Authorization: [REDACTED]", text)
    text = _BEARER_RE.sub("Bearer [REDACTED]", text)
    text = _NAMED_SECRET_RE.sub(
        lambda match: f"{match.group('label')}[REDACTED]",
        text,
    )
    text = _OPENAI_KEY_RE.sub("[REDACTED]", text)
    return text[: max(0, int(max_length))]


def sanitize_persisted_error_text(value: Any) -> Optional[str]:
    """Return a storage-safe legacy error string.

    Unstructured legacy fields cannot prove that a provider message is safe.  We
    retain ordinary operator-facing text, but if a known credential or inline
    binary form had to be redacted we replace the whole value with the generic
    public error.  This avoids leaving partially redacted request bodies in
    SQLite while keeping harmless historical messages readable.
    """

    if value in (None, ""):
        return None
    original = str(value)
    sanitized = sanitize_text(original, max_length=300).strip()
    if not sanitized:
        return SAFE_MESSAGES[ErrorCode.UNKNOWN]
    if sanitized != original[:300] or "[REDACTED]" in sanitized:
        return SAFE_MESSAGES[ErrorCode.UNKNOWN]
    return sanitized


def sanitize_http_detail(value: Any, *, max_depth: int = 5) -> Any:
    """Sanitize a JSON-compatible HTTPException detail without flattening it.

    Recovery responses intentionally use structured ``detail`` dictionaries
    (for example ``needs_prompt`` and ``operation_running``).  This function
    preserves those public fields, recursively redacts strings, and drops keys
    whose names identify credentials.  Values outside the JSON data model are
    replaced by the generic public message rather than stringified.
    """

    def clean(item: Any, depth: int) -> Any:
        if depth > max_depth:
            return SAFE_MESSAGES[ErrorCode.UNKNOWN]
        if item is None or isinstance(item, (bool, int, float)):
            return item
        if isinstance(item, str):
            return sanitize_persisted_error_text(item) or ""
        if isinstance(item, Mapping):
            result: Dict[str, Any] = {}
            for raw_key, raw_value in list(item.items())[:100]:
                key = str(raw_key)[:100]
                if _SENSITIVE_FIELD_RE.search(key):
                    continue
                result[key] = clean(raw_value, depth + 1)
            return result
        if isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
            return [clean(child, depth + 1) for child in list(item)[:100]]
        return SAFE_MESSAGES[ErrorCode.UNKNOWN]

    return clean(value, 0)


def _safe_identifier(value: Any) -> Optional[str]:
    if value is None:
        return None
    candidate = str(value).strip()
    if not _SAFE_IDENTIFIER_RE.fullmatch(candidate):
        return None
    if sanitize_text(candidate, max_length=128) != candidate:
        return None
    if _SENSITIVE_FIELD_RE.search(candidate):
        return None
    return candidate


def _status_code(error: BaseException) -> Optional[int]:
    value = getattr(error, "status_code", None)
    if value is None:
        value = getattr(getattr(error, "response", None), "status_code", None)
    try:
        status = int(value)
    except (TypeError, ValueError):
        return None
    return status if 100 <= status <= 599 else None


def _response_headers(error: BaseException) -> Mapping[str, Any]:
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None) or getattr(error, "headers", None)
    return headers if isinstance(headers, Mapping) else {}


def _header(headers: Mapping[str, Any], *names: str) -> Any:
    wanted = {name.lower() for name in names}
    for key, value in headers.items():
        if str(key).lower() in wanted:
            return value
    return None


def _retry_after_seconds(headers: Mapping[str, Any]) -> Optional[float]:
    value = _header(headers, "retry-after")
    if value is None:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try:
            retry_at = parsedate_to_datetime(str(value))
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            seconds = (retry_at - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    seconds = max(0.0, min(86400.0, seconds))
    return int(seconds) if seconds.is_integer() else round(seconds, 3)


def _request_id(error: BaseException, headers: Mapping[str, Any]) -> Optional[str]:
    value = getattr(error, "request_id", None)
    if value is None:
        value = _header(
            headers,
            "x-request-id",
            "request-id",
            "x-amzn-requestid",
            "cf-ray",
        )
    return _safe_identifier(value)


def normalize_error_code(value: Any, *, has_error: bool = False) -> Optional[ErrorCode]:
    if isinstance(value, ErrorCode):
        return value
    try:
        return ErrorCode(str(value)) if value not in (None, "") else (
            ErrorCode.UNKNOWN if has_error else None
        )
    except ValueError:
        return ErrorCode.UNKNOWN if has_error or value else None


def _retryable_for(code: ErrorCode, status: Optional[int] = None) -> bool:
    if code is ErrorCode.PROVIDER_ERROR and status is not None:
        return status >= 500 or status in {408, 425}
    return _DEFAULT_RETRYABLE[code]


def normalize_error_metadata(
    code: Any,
    metadata: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Whitelist and normalize metadata before it crosses a persistence boundary."""

    normalized_code = normalize_error_code(code, has_error=True) or ErrorCode.UNKNOWN
    source = metadata if isinstance(metadata, Mapping) else {}
    http_status = source.get("http_status")
    try:
        http_status = int(http_status) if http_status is not None else None
    except (TypeError, ValueError):
        http_status = None
    if http_status is not None and not 100 <= http_status <= 599:
        http_status = None

    retry_after = source.get("retry_after_seconds")
    try:
        retry_after = float(retry_after) if retry_after is not None else None
    except (TypeError, ValueError):
        retry_after = None
    if retry_after is not None:
        retry_after = max(0.0, min(86400.0, retry_after))
        retry_after = int(retry_after) if retry_after.is_integer() else round(retry_after, 3)

    safe_message = sanitize_text(
        source.get("safe_message") or SAFE_MESSAGES[normalized_code],
        max_length=300,
    ).strip()
    if not safe_message:
        safe_message = SAFE_MESSAGES[normalized_code]

    result: Dict[str, Any] = {
        "retryable": (
            source["retryable"]
            if isinstance(source.get("retryable"), bool)
            else _retryable_for(normalized_code, http_status)
        ),
        "retry_after_seconds": retry_after,
        "safe_message": safe_message,
    }
    provider = _safe_identifier(source.get("provider"))
    request_id = _safe_identifier(source.get("request_id"))
    if provider:
        result["provider"] = provider
    if http_status is not None:
        result["http_status"] = http_status
    if request_id:
        result["request_id"] = request_id
    return result


@dataclass(frozen=True)
class SafeError:
    code: ErrorCode
    retryable: bool
    safe_message: str
    retry_after_seconds: Optional[float] = None
    provider: Optional[str] = None
    http_status: Optional[int] = None
    request_id: Optional[str] = None

    def metadata(self) -> Dict[str, Any]:
        return normalize_error_metadata(
            self.code,
            {
                "retryable": self.retryable,
                "retry_after_seconds": self.retry_after_seconds,
                "safe_message": self.safe_message,
                "provider": self.provider,
                "http_status": self.http_status,
                "request_id": self.request_id,
            },
        )

    def to_record(self) -> Dict[str, Any]:
        return {"error_code": self.code.value, "error_meta": self.metadata()}


class ClassifiedError(RuntimeError):
    """Exception boundary that carries only an already-sanitized error record.

    Provider adapters use this when the original exception may retain request
    headers, bodies or DataURLs in its traceback.  It is intentionally safe to
    persist and return, and never keeps a reference to the provider exception.
    """

    def __init__(self, safe_error: SafeError):
        self.safe_error = safe_error
        super().__init__(safe_error.safe_message)


def make_safe_error(
    code: Any,
    *,
    provider: Optional[str] = None,
    http_status: Optional[int] = None,
    retry_after_seconds: Optional[float] = None,
    request_id: Optional[str] = None,
) -> SafeError:
    """Build a normalized safe error for failures without a provider exception."""

    normalized_code = normalize_error_code(code, has_error=True) or ErrorCode.UNKNOWN
    return SafeError(
        code=normalized_code,
        retryable=_retryable_for(normalized_code, http_status),
        safe_message=SAFE_MESSAGES[normalized_code],
        retry_after_seconds=retry_after_seconds,
        provider=_safe_identifier(provider),
        http_status=http_status,
        request_id=_safe_identifier(request_id),
    )


def classify_exception(
    error: BaseException,
    *,
    provider: Optional[str] = None,
) -> SafeError:
    """Classify an exception without returning its message or response body."""

    if isinstance(error, ClassifiedError):
        return error.safe_error

    status = _status_code(error)
    headers = _response_headers(error)
    class_name = type(error).__name__.lower()
    module_name = type(error).__module__.lower()
    raw_message = str(error).lower()  # inspected only; never copied to the result

    if isinstance(error, asyncio.CancelledError) or "cancelled" in class_name:
        code = ErrorCode.CANCELLED
    elif status in {401, 403} or any(
        marker in class_name for marker in ("authentication", "permissiondenied")
    ):
        code = ErrorCode.AUTH
    elif status == 429 or "ratelimit" in class_name:
        code = ErrorCode.RATE_LIMIT
    elif status == 409:
        code = ErrorCode.CONFLICT
    elif isinstance(error, TimeoutError) or status in {408, 504} or "timeout" in class_name:
        code = ErrorCode.TIMEOUT
    elif isinstance(error, ConnectionError) or any(
        marker in class_name for marker in ("connection", "network", "dns")
    ):
        code = ErrorCode.NETWORK
    elif isinstance(error, OSError) and (
        isinstance(error, (PermissionError, FileNotFoundError, IsADirectoryError))
        or getattr(error, "errno", None)
        in {errno.EACCES, errno.EDQUOT, errno.ENOSPC, errno.EROFS, errno.EIO}
    ):
        code = ErrorCode.DISK
    elif isinstance(error, (ValueError, RuntimeError)) and any(
        marker in raw_message
        for marker in (
            "api key 未配置",
            "apikey 未配置",
            "api_key 未配置",
            "未配置",
            "not configured",
            "missing configuration",
            "missing api key",
        )
    ):
        code = ErrorCode.CONFIG_MISSING
    elif status is not None or any(
        marker in class_name for marker in ("apierror", "httperror", "providererror")
    ) or module_name.startswith(("openai", "litellm", "requests", "httpx", "httpcore")):
        code = ErrorCode.PROVIDER_ERROR
    else:
        code = ErrorCode.UNKNOWN

    retry_after = _retry_after_seconds(headers) if code is ErrorCode.RATE_LIMIT else None
    safe_provider = _safe_identifier(provider or getattr(error, "provider", None))
    return SafeError(
        code=code,
        retryable=_retryable_for(code, status),
        retry_after_seconds=retry_after,
        safe_message=SAFE_MESSAGES[code],
        provider=safe_provider,
        http_status=status,
        request_id=_request_id(error, headers),
    )
