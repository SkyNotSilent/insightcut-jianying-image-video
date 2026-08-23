"""统一 TTS 音色目录与参数归一化。"""

import json
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional


MIMO_PRESET_VOICES = [
    {
        "voice_id": "mimo_default",
        "name": "MiMo 默认",
        "gender": "auto",
        "language": "auto",
        "description": "根据集群自动选择的 MiMo 默认音色。",
        "sort_order": 100,
    },
    {"voice_id": "冰糖", "name": "冰糖", "gender": "female", "language": "zh", "description": "中文女声，清亮自然。", "sort_order": 90},
    {"voice_id": "茉莉", "name": "茉莉", "gender": "female", "language": "zh", "description": "中文女声，柔和克制。", "sort_order": 80},
    {"voice_id": "苏打", "name": "苏打", "gender": "male", "language": "zh", "description": "中文男声，干净年轻。", "sort_order": 70},
    {"voice_id": "白桦", "name": "白桦", "gender": "male", "language": "zh", "description": "中文男声，稳定沉着。", "sort_order": 60},
    {"voice_id": "Mia", "name": "Mia", "gender": "female", "language": "en", "description": "English female voice.", "sort_order": 50},
    {"voice_id": "Chloe", "name": "Chloe", "gender": "female", "language": "en", "description": "English female voice.", "sort_order": 40},
    {"voice_id": "Milo", "name": "Milo", "gender": "male", "language": "en", "description": "English male voice.", "sort_order": 30},
    {"voice_id": "Dean", "name": "Dean", "gender": "male", "language": "en", "description": "English male voice.", "sort_order": 20},
]

DOUBAO_PRESET_VOICES = [
    ("zh_female_shuangkuaisisi_moon_bigtts", "爽快思思", "female", "活泼开朗，适合轻松愉快的内容", 100),
    ("zh_female_wanwanxiaohe_moon_bigtts", "弯弯小鹤", "female", "温柔甜美，适合温馨治愈的内容", 90),
    ("zh_female_tianmeibeibei_moon_bigtts", "甜美贝贝", "female", "甜美可爱，适合少女风格内容", 80),
    ("zh_female_qingxinruoxi_moon_bigtts", "清新若溪", "female", "清新自然，适合文艺清新内容", 70),
    ("zh_female_wenrouxiaoya_moon_bigtts", "温柔小雅", "female", "温柔知性，适合知识科普内容", 60),
    ("zh_female_mizai_uranus_bigtts", "米仔", "female", "自然真实，适合故事讲述", 50),
    ("zh_male_wennuanahu_moon_bigtts", "温暖阿虎", "male", "温暖磁性，适合情感类内容", 40),
    ("zh_male_qingchexiaoxin_moon_bigtts", "清澈小新", "male", "清澈明朗，适合青春活力内容", 30),
    ("zh_male_jieshuoxiaoming_moon_bigtts", "讲解小明", "male", "明朗稳定，适合知识讲解和科普内容", 20),
    ("zh_male_chenwendongge_moon_bigtts", "沉稳东哥", "male", "沉稳大气，适合严肃专业内容", 10),
]

MIMO_DEFAULT_ENABLED_IDS = ("冰糖", "茉莉", "苏打", "白桦")
DOUBAO_DEFAULT_ENABLED_IDS = (
    "zh_female_shuangkuaisisi_moon_bigtts",
    "zh_male_jieshuoxiaoming_moon_bigtts",
)
MIMO_VOICE_IDS = frozenset(voice["voice_id"] for voice in MIMO_PRESET_VOICES)
DOUBAO_VOICE_IDS = frozenset(voice[0] for voice in DOUBAO_PRESET_VOICES)

SPEED_LEVELS = ("very_slow", "slow", "normal", "fast", "very_fast")
DOUBAO_SPEED_RATIOS = {
    "very_slow": 0.8,
    "slow": 0.9,
    "normal": 1.0,
    "fast": 1.25,
    "very_fast": 1.5,
}
MIMO_SPEED_INSTRUCTIONS = {
    "very_slow": "语速很慢，停顿充分，保持清晰。",
    "slow": "语速偏慢，表达舒展。",
    "normal": "",
    "fast": "语速偏快，节奏紧凑，保持清晰。",
    "very_fast": "语速很快，节奏明快，但保持清晰。",
}

SEGMENT_TTS_OVERRIDE_MARKER = "_segment_override"


@dataclass(frozen=True)
class VoiceSelection:
    provider: str
    voice_id: str
    kind: str = "preset"

    @property
    def key(self) -> str:
        prefix = "mimo-clone" if self.kind == "clone" else self.provider
        return f"{prefix}:{self.voice_id}"


def build_voice_key(provider: str, voice_id: str, kind: str = "preset") -> str:
    provider = (provider or "").strip().lower()
    voice_id = (voice_id or "").strip()
    if not voice_id:
        raise ValueError("音色 ID 不能为空")
    if kind == "clone" or provider == "mimo-clone":
        return f"mimo-clone:{voice_id}"
    if provider not in {"doubao", "mimo"}:
        raise ValueError(f"不支持的 TTS provider: {provider}")
    return f"{provider}:{voice_id}"


def parse_voice_key(value: Optional[str], default_provider: str = "mimo") -> VoiceSelection:
    raw = (value or "").strip()
    fallback = (default_provider or "mimo").strip().lower()
    if fallback not in {"doubao", "mimo"}:
        fallback = "mimo"
    if raw.startswith("mimo-clone:"):
        return VoiceSelection("mimo", raw.split(":", 1)[1], "clone")
    if raw.startswith("mimo:") or raw.startswith("doubao:"):
        provider, voice_id = raw.split(":", 1)
        return VoiceSelection(provider, voice_id, "preset")
    if raw in MIMO_VOICE_IDS:
        return VoiceSelection("mimo", raw, "preset")
    if raw in DOUBAO_VOICE_IDS or raw.startswith(("zh_", "S_")):
        return VoiceSelection("doubao", raw, "preset")
    return VoiceSelection(fallback, raw, "preset")


def speed_instruction(level: str) -> str:
    return MIMO_SPEED_INSTRUCTIONS.get(level, "")


def _speed_level(value: Any, default: str = "normal") -> str:
    candidate = str(value or default).strip().lower()
    return candidate if candidate in SPEED_LEVELS else default


def _clamp_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def normalize_tts_options(
    options: Optional[Dict[str, Any]],
    provider_config: Optional[Dict[str, Any]],
    provider: str,
) -> Dict[str, Any]:
    incoming = options or {}
    defaults = provider_config or {}
    level = _speed_level(incoming.get("speed_level"), _speed_level(defaults.get("speed_level")))
    if provider == "doubao":
        volume = _clamp_float(
            incoming.get("volume_ratio", defaults.get("volume_ratio")), 1.0, 0.5, 2.0
        )
        return {
            "speed_level": level,
            "speed_ratio": DOUBAO_SPEED_RATIOS[level],
            "volume_ratio": volume,
        }
    style = str(incoming.get("style_prompt") or defaults.get("style_prompt") or "").strip()[:300]
    return {
        "speed_level": level,
        "style_prompt": style,
        "speed_instruction": speed_instruction(level),
    }


def _parse_tts_options(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def segment_tts_override(
    value: Any,
    *,
    segment_voice_type: str = "",
    task_voice_type: str = "",
    task_options: Optional[Dict[str, Any]] = None,
) -> tuple[Dict[str, Any], bool]:
    """Return the explicit per-segment TTS override and whether it is active.

    Older builds wrote the *effective* task options into every segment after
    generation.  Those rows are not overrides when they still match the task
    snapshot.  A differing legacy snapshot is exposed as an override so the UI
    never hides the parameters that produced the currently selected audio.
    """
    parsed = _parse_tts_options(value)
    marked = bool(parsed.pop(SEGMENT_TTS_OVERRIDE_MARKER, False))
    parsed.pop("speed_ratio", None)
    parsed.pop("speed_instruction", None)
    comparable_task = _parse_tts_options(task_options)
    comparable_task.pop("speed_ratio", None)
    comparable_task.pop("speed_instruction", None)
    legacy_difference = bool(parsed and parsed != comparable_task)
    active = marked or legacy_difference
    return (parsed if active else {}), active


def encode_segment_tts_override(options: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Mark user-selected segment parameters without confusing them with snapshots."""
    parsed = _parse_tts_options(options)
    parsed.pop(SEGMENT_TTS_OVERRIDE_MARKER, None)
    parsed.pop("speed_ratio", None)
    parsed.pop("speed_instruction", None)
    if not parsed:
        return {}
    return {**parsed, SEGMENT_TTS_OVERRIDE_MARKER: True}


def normalized_enabled_providers(value: Any) -> Iterable[str]:
    if not isinstance(value, (list, tuple, set)):
        return ("doubao", "mimo")
    providers = []
    for item in value:
        provider = str(item).strip().lower()
        if provider in {"doubao", "mimo"} and provider not in providers:
            providers.append(provider)
    return tuple(providers or ("doubao", "mimo"))
