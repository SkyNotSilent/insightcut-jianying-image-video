"""
配置管理模块
"""
import json
import os
from copy import deepcopy
from pathlib import Path


def _load_local_env() -> None:
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))
        return
    load_dotenv(env_path)


_load_local_env()


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _clamp_int(value, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


class Config:
    """配置类 - 默认配置 + data/config.json 运行时覆盖"""

    BASE_DIR: Path = Path(
        os.getenv("INSIGHTCUT_DATA_ROOT") or Path(__file__).resolve().parent.parent
    ).expanduser().resolve()

    # LLM 配置。真实 key 请写入 .env、环境变量，或通过前端“模型配置”页保存到 data/config.json。
    LLM_PROVIDER: str = _env("LLM_PROVIDER", "")
    LLM_API_KEY: str = _env("LLM_API_KEY", "")
    LLM_BASE_URL: str = _env("LLM_BASE_URL", "")
    LLM_MODEL: str = _env("LLM_MODEL", "")
    LLM_PROTOCOL: str = _env("LLM_PROTOCOL", "openai")

    # 旧版 Anthropic 命名仍保留为回退，避免已有部署升级后配置失效。
    ANTHROPIC_API_KEY: str = _env("ANTHROPIC_API_KEY", _env("ANTHROPIC_AUTH_TOKEN", ""))
    ANTHROPIC_BASE_URL: str = _env("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    ANTHROPIC_MODEL: str = _env("ANTHROPIC_MODEL", "claude-sonnet-4-5")

    # 豆包 TTS 配置
    DOUBAO_TTS_API_URL: str = _env("DOUBAO_TTS_API_URL", "https://openspeech.bytedance.com/api/v1/tts")
    DOUBAO_TTS_AUTH_METHOD: str = _env("DOUBAO_TTS_AUTH_METHOD", "access_token")
    DOUBAO_TTS_APPID: str = _env("DOUBAO_TTS_APPID", "")
    DOUBAO_TTS_TOKEN: str = _env("DOUBAO_TTS_TOKEN", "")
    DOUBAO_TTS_API_KEY: str = _env("DOUBAO_TTS_API_KEY", "")
    DOUBAO_TTS_CLUSTER: str = _env("DOUBAO_TTS_CLUSTER", "volcano_tts")
    DOUBAO_TTS_DEFAULT_VOICE: str = _env(
        "DOUBAO_TTS_DEFAULT_VOICE",
        "zh_male_jieshuoxiaoming_moon_bigtts",
    )

    # 小米 MiMo TTS 配置。MiMo TTS 走 /v1/chat/completions，不走 /v1/audio/speech。
    TTS_PROVIDER: str = _env("TTS_PROVIDER", "doubao")
    MIMO_TTS_BASE_URL: str = _env("MIMO_TTS_BASE_URL", "https://token-plan-sgp.xiaomimimo.com/v1")
    MIMO_TTS_API_KEY: str = _env("MIMO_TTS_API_KEY", "")
    MIMO_TTS_MODEL: str = _env("MIMO_TTS_MODEL", "mimo-v2.5-tts")
    MIMO_TTS_CLONE_MODEL: str = _env("MIMO_TTS_CLONE_MODEL", "mimo-v2.5-tts-voiceclone")
    MIMO_TTS_DEFAULT_VOICE: str = _env("MIMO_TTS_DEFAULT_VOICE", "冰糖")
    MIMO_TTS_FORMAT: str = _env("MIMO_TTS_FORMAT", "wav")
    MIMO_TTS_STYLE_PROMPT: str = _env("MIMO_TTS_STYLE_PROMPT", "自然清晰，适合中文短视频旁白。")

    # 图像生成配置
    SEEDREAM_API_KEY: str = _env("SEEDREAM_API_KEY", "")
    SEEDREAM_API_URL: str = _env("SEEDREAM_API_URL", "https://apihub.agnes-ai.com/v1/images/generations")
    SEEDREAM_MODEL: str = _env("SEEDREAM_MODEL", "agnes-image-2.1-flash")

    # 日志配置
    LOG_LEVEL: str = _env("LOG_LEVEL", "INFO")

    CONFIG_FILE: Path = BASE_DIR / "data" / "config.json"
    LEGACY_CONFIG_FILE: Path = Path("data/config.json")

    @classmethod
    def default_model_config(cls) -> dict:
        return {
            "llm": {
                "provider": cls.LLM_PROVIDER,
                "base_url": cls.LLM_BASE_URL or cls.ANTHROPIC_BASE_URL,
                "api_key": cls.LLM_API_KEY or cls.ANTHROPIC_API_KEY,
                "model": cls.LLM_MODEL or cls.ANTHROPIC_MODEL,
                "protocol": cls.LLM_PROTOCOL,
                "provider_options": {},
            },
            "image": {
                "api_url": cls.SEEDREAM_API_URL,
                "api_key": cls.SEEDREAM_API_KEY,
                "model": cls.SEEDREAM_MODEL,
                "size": _env("SEEDREAM_SIZE", "auto"),
            },
            "tts": {
                "provider": cls.TTS_PROVIDER,
                "enabled_providers": ["doubao", "mimo"],
                "preview_text": "欢迎来到 InsightCut，让我们一起把灵感变成精彩视频。",
                "auth_method": cls.DOUBAO_TTS_AUTH_METHOD,
                "api_url": cls.DOUBAO_TTS_API_URL,
                "appid": cls.DOUBAO_TTS_APPID,
                "token": cls.DOUBAO_TTS_TOKEN,
                "api_key": cls.DOUBAO_TTS_API_KEY,
                "cluster": cls.DOUBAO_TTS_CLUSTER,
                "default_voice": cls.DOUBAO_TTS_DEFAULT_VOICE,
                "speed_level": "normal",
                "volume_ratio": 1.0,
                "mimo": {
                    "base_url": cls.MIMO_TTS_BASE_URL,
                    "api_key": cls.MIMO_TTS_API_KEY,
                    "model": cls.MIMO_TTS_MODEL,
                    "clone_model": cls.MIMO_TTS_CLONE_MODEL,
                    "default_voice": cls.MIMO_TTS_DEFAULT_VOICE,
                    "format": cls.MIMO_TTS_FORMAT,
                    "style_prompt": cls.MIMO_TTS_STYLE_PROMPT,
                    "speed_level": "normal",
                },
            },
            "generation": {
                "prompt_concurrency": 4,
                "tts_concurrency": _clamp_int(_env("TTS_CONCURRENCY", "1"), 1, 1, 8),
                "image_concurrency": _clamp_int(_env("IMAGE_CONCURRENCY", "8"), 8, 1, 8),
                "retry_count": _clamp_int(_env("GENERATION_RETRY_COUNT", "2"), 2, 0, 5),
                "retry_interval_seconds": _clamp_int(
                    _env("GENERATION_RETRY_INTERVAL_SECONDS", "5"), 5, 1, 60
                ),
            },
        }

    @classmethod
    def load_model_config(cls) -> dict:
        config = deepcopy(cls.default_model_config())
        config_file = cls._resolve_config_file()
        if not config_file.exists():
            cls._normalize_model_config(config)
            return config

        try:
            with config_file.open("r", encoding="utf-8") as f:
                overrides = json.load(f)
        except Exception:
            cls._normalize_model_config(config)
            return config

        for section in ("llm", "image", "tts", "generation"):
            if isinstance(overrides.get(section), dict):
                config[section].update({
                    key: value
                    for key, value in overrides[section].items()
                    if value is not None
                })
        cls._normalize_model_config(config)
        return config

    @classmethod
    def save_model_config(cls, config: dict) -> dict:
        current = cls.load_model_config()
        incoming = config or {}

        for section in ("llm", "image", "tts", "generation"):
            if isinstance(incoming.get(section), dict):
                current[section].update({
                    key: value
                    for key, value in incoming[section].items()
                    if value is not None
                })
        cls._normalize_model_config(current)

        cls.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with cls.CONFIG_FILE.open("w", encoding="utf-8") as f:
            json.dump(current, f, ensure_ascii=False, indent=2)
        return current

    @classmethod
    def _normalize_llm_config(cls, config: dict) -> None:
        from src.text.provider_catalog import (
            canonical_model_id,
            get_provider,
            infer_provider,
            sanitize_provider_options,
        )

        llm = config.setdefault("llm", {})
        explicit_provider = str(llm.get("provider") or "").strip().lower()
        provider = explicit_provider if get_provider(explicit_provider) else ""
        if not provider:
            legacy_config = dict(llm)
            legacy_config.pop("provider", None)
            provider = infer_provider(legacy_config)
        provider_record = get_provider(provider)
        if provider_record is None:
            provider = "custom"
            provider_record = get_provider(provider)
        llm["provider"] = provider

        protocol = str(llm.get("protocol") or "").strip().lower()
        llm["protocol"] = (
            protocol if protocol in {"openai", "anthropic"} else "openai"
        )

        llm["base_url"] = (
            llm.get("base_url") or provider_record.get("default_base_url") or ""
        )
        llm["api_key"] = llm.get("api_key") or ""

        raw_model = str(llm.get("model") or "").strip()
        if provider == "custom" or not raw_model:
            llm["model"] = raw_model
        else:
            llm["model"] = canonical_model_id(
                provider_record["litellm_provider"], raw_model
            )

        raw_options = llm.get("provider_options")
        if not isinstance(raw_options, dict):
            raw_options = {}
        llm["provider_options"] = sanitize_provider_options(provider, raw_options)

    @classmethod
    def _normalize_generation_config(cls, config: dict) -> None:
        generation = config.setdefault("generation", {})
        generation["prompt_concurrency"] = _clamp_int(
            generation.get("prompt_concurrency"), 4, 1, 8
        )
        generation["tts_concurrency"] = _clamp_int(
            generation.get("tts_concurrency"), 1, 1, 8
        )
        generation["image_concurrency"] = _clamp_int(
            generation.get("image_concurrency"), 8, 1, 8
        )
        generation["retry_count"] = _clamp_int(
            generation.get("retry_count"), 2, 0, 5
        )
        generation["retry_interval_seconds"] = _clamp_int(
            generation.get("retry_interval_seconds"), 5, 1, 60
        )

    @classmethod
    def _normalize_tts_config(cls, config: dict) -> None:
        from src.draft.voice_catalog import normalized_enabled_providers

        tts = config.setdefault("tts", {})
        provider = (tts.get("provider") or cls.TTS_PROVIDER or "doubao").strip().lower()
        tts["provider"] = provider if provider in {"doubao", "mimo"} else "doubao"
        tts["enabled_providers"] = list(
            normalized_enabled_providers(tts.get("enabled_providers"))
        )
        tts["preview_text"] = str(
            tts.get("preview_text")
            or "欢迎来到 InsightCut，让我们一起把灵感变成精彩视频。"
        )[:80]
        raw_auth_method = (tts.get("auth_method") or cls.DOUBAO_TTS_AUTH_METHOD or "").strip().lower()
        if raw_auth_method not in {"access_token", "api_key"}:
            raw_auth_method = "api_key" if tts.get("api_key") and not (tts.get("appid") and tts.get("token")) else "access_token"
        tts["auth_method"] = raw_auth_method
        tts["api_url"] = (
            tts.get("api_url")
            or tts.get("url")
            or (tts.get("base_url") if tts["provider"] == "doubao" else "")
            or cls.DOUBAO_TTS_API_URL
        )
        tts["api_key"] = (
            tts.get("api_key")
            or tts.get("key")
            or tts.get("x_api_key")
            or cls.DOUBAO_TTS_API_KEY
        )
        tts["appid"] = (
            tts.get("appid")
            or tts.get("app_id")
            or tts.get("appId")
            or cls.DOUBAO_TTS_APPID
        )
        tts["token"] = (
            tts.get("token")
            or tts.get("access_token")
            or tts.get("accessToken")
            or (tts.get("api_key") if tts["auth_method"] == "access_token" else "")
            or cls.DOUBAO_TTS_TOKEN
        )
        tts["cluster"] = tts.get("cluster") or cls.DOUBAO_TTS_CLUSTER
        tts["default_voice"] = (
            tts.get("default_voice")
            or tts.get("voice_type")
            or tts.get("voice")
            or cls.DOUBAO_TTS_DEFAULT_VOICE
        )
        tts["speed_level"] = (
            tts.get("speed_level")
            if tts.get("speed_level") in {"very_slow", "slow", "normal", "fast", "very_fast"}
            else "normal"
        )
        try:
            tts["volume_ratio"] = max(0.5, min(2.0, float(tts.get("volume_ratio", 1.0))))
        except (TypeError, ValueError):
            tts["volume_ratio"] = 1.0
        mimo_defaults = {
            "base_url": cls.MIMO_TTS_BASE_URL,
            "api_key": cls.MIMO_TTS_API_KEY,
            "model": cls.MIMO_TTS_MODEL,
            "clone_model": cls.MIMO_TTS_CLONE_MODEL,
            "default_voice": cls.MIMO_TTS_DEFAULT_VOICE,
            "format": cls.MIMO_TTS_FORMAT,
            "style_prompt": cls.MIMO_TTS_STYLE_PROMPT,
            "speed_level": "normal",
        }
        raw_mimo = tts.get("mimo") if isinstance(tts.get("mimo"), dict) else {}
        mimo = {
            key: (raw_mimo.get(key) if raw_mimo.get(key) is not None else default)
            for key, default in mimo_defaults.items()
        }
        mimo["base_url"] = mimo.get("base_url") or cls.MIMO_TTS_BASE_URL
        mimo["model"] = mimo.get("model") or cls.MIMO_TTS_MODEL
        mimo["clone_model"] = mimo.get("clone_model") or cls.MIMO_TTS_CLONE_MODEL
        mimo["default_voice"] = mimo.get("default_voice") or cls.MIMO_TTS_DEFAULT_VOICE
        mimo["format"] = (mimo.get("format") or cls.MIMO_TTS_FORMAT).lower()
        mimo["style_prompt"] = mimo.get("style_prompt") or cls.MIMO_TTS_STYLE_PROMPT
        if mimo.get("speed_level") not in {"very_slow", "slow", "normal", "fast", "very_fast"}:
            mimo["speed_level"] = "normal"
        tts["mimo"] = mimo

    @classmethod
    def _normalize_model_config(cls, config: dict) -> None:
        cls._normalize_llm_config(config)
        cls._normalize_tts_config(config)
        cls._normalize_generation_config(config)

    @classmethod
    def _resolve_config_file(cls) -> Path:
        if cls.CONFIG_FILE.exists():
            return cls.CONFIG_FILE

        legacy = cls.LEGACY_CONFIG_FILE
        try:
            legacy = legacy.resolve()
        except Exception:
            pass

        if legacy != cls.CONFIG_FILE and legacy.exists():
            return legacy
        return cls.CONFIG_FILE

    @classmethod
    def llm_config(cls) -> dict:
        return cls.load_model_config()["llm"]

    @classmethod
    def image_config(cls) -> dict:
        return cls.load_model_config()["image"]

    @classmethod
    def tts_config(cls) -> dict:
        return cls.load_model_config()["tts"]

    @classmethod
    def generation_config(cls) -> dict:
        return cls.load_model_config()["generation"]
