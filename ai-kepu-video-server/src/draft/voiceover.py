"""
自动配音模块
支持豆包 TTS 和小米 MiMo TTS
"""

import base64
import logging
import time
import uuid
from pathlib import Path

import requests

from src.config import Config
from src.api.error_model import (
    ClassifiedError,
    ErrorCode,
    classify_exception,
    make_safe_error,
)
from src.draft.voice_catalog import (
    MIMO_PRESET_VOICES,
    normalize_tts_options,
    parse_voice_key,
)

logger = logging.getLogger(__name__)


MIMO_TTS_VOICES = [
    {
        "id": voice["voice_id"],
        "name": voice["name"],
        "gender": voice["gender"],
        "language": voice["language"],
        "provider": "mimo",
        "description": voice["description"],
    }
    for voice in MIMO_PRESET_VOICES
]


class VoiceOverGenerator:
    """配音生成器 - 按配置分发到豆包或小米 MiMo TTS"""

    def __init__(
        self,
        output_dir: str = "output/voiceovers",
        tts_config: dict = None,
        clone_store=None,
        generation_config: dict = None,
    ):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.tts_config = tts_config or Config.tts_config()
        self.provider = (self.tts_config.get("provider") or "doubao").lower()
        self.auth_method = (self.tts_config.get("auth_method") or "access_token").lower()
        self.api_url = self.tts_config.get("api_url") or Config.DOUBAO_TTS_API_URL
        self.appid = self.tts_config.get("appid") or Config.DOUBAO_TTS_APPID
        self.token = self.tts_config.get("token") or Config.DOUBAO_TTS_TOKEN
        self.api_key = self.tts_config.get("api_key") or Config.DOUBAO_TTS_API_KEY
        self.cluster = self.tts_config.get("cluster") or Config.DOUBAO_TTS_CLUSTER
        self.default_voice = self.tts_config.get("default_voice") or Config.DOUBAO_TTS_DEFAULT_VOICE
        self.mimo_config = self.tts_config.get("mimo") or {}
        self.clone_store = clone_store
        generation_config = generation_config or Config.generation_config()
        retry_count = generation_config.get("retry_count", 2)
        self.max_attempts = max(1, min(6, int(retry_count) + 1))
        self.retry_interval_seconds = max(
            1,
            min(60, int(generation_config.get("retry_interval_seconds", 5))),
        )

    def _clone_store(self):
        if self.clone_store is None:
            from src.database import db_client
            from src.draft.voice_clone import VoiceCloneStore

            self.clone_store = VoiceCloneStore(Config.BASE_DIR, db_client)
        return self.clone_store

    def generate(
        self,
        text: str,
        filename: str = None,
        voice_type: str = None,
        speed_ratio: float = None,
        volume_ratio: float = None,
        speed_level: str = None,
        style_prompt: str = None,
    ) -> str:
        """
        生成配音，返回 wav 文件路径。

        Args:
            text: 文本内容
            filename: 输出文件名（不含扩展名）
            voice_type: 声音类型
            speed_ratio: 兼容旧调用的豆包数字语速
            volume_ratio: 豆包音量倍率，0.5-2.0
            speed_level: 统一五档语速
            style_prompt: MiMo 风格指令
        """
        if voice_type:
            raw_voice = voice_type
        elif self.provider == "mimo":
            raw_voice = self.mimo_config.get("default_voice") or Config.MIMO_TTS_DEFAULT_VOICE
        else:
            raw_voice = self.default_voice
        selection = parse_voice_key(raw_voice, default_provider=self.provider)
        incoming_options = {}
        if speed_level is not None:
            incoming_options["speed_level"] = speed_level
        if volume_ratio is not None:
            incoming_options["volume_ratio"] = volume_ratio
        if style_prompt is not None:
            incoming_options["style_prompt"] = style_prompt
        provider_config = self.tts_config if selection.provider == "doubao" else self.mimo_config
        options = normalize_tts_options(incoming_options, provider_config, selection.provider)

        if selection.provider == "mimo":
            clone_data_url = None
            if selection.kind == "clone":
                clone_data_url = self._clone_store().reference_data_url(selection.voice_id)
            return self._generate_mimo(
                text,
                filename,
                selection.voice_id,
                options["style_prompt"],
                options["speed_instruction"],
                clone_data_url=clone_data_url,
            )
        numeric_speed = speed_ratio if speed_ratio is not None else options["speed_ratio"]
        return self._generate_doubao(
            text,
            filename,
            selection.voice_id,
            numeric_speed,
            options["volume_ratio"],
        )

    def _output_path(self, text: str, filename: str = None) -> Path:
        if not filename:
            safe = "".join(c for c in text[:10] if c.isalnum() or c in "_ ")
            filename = safe.strip() or "voice"
        return self.output_dir / f"{filename}.wav"

    def _generate_doubao(
        self,
        text: str,
        filename: str = None,
        voice_type: str = None,
        speed_ratio: float = 1.25,
        volume_ratio: float = 1.0,
    ) -> str:
        output_path = self._output_path(text, filename)
        logger.debug(f"生成配音: {text[:30]}... -> {output_path}")
        logger.debug(
            "使用 TTS 配置 - METHOD: %s, APPID: %s, TOKEN: %s, API_KEY: %s",
            self.auth_method,
            "已设置" if self.appid else "未设置",
            "已设置" if self.token else "未设置",
            "已设置" if self.api_key else "未设置",
        )

        if self.auth_method == "api_key":
            if not self.api_key:
                raise ValueError("豆包 TTS API Key 配置未完成，请在 API 配置页填写 DOUBAO_TTS_API_KEY")
        elif not self.appid or not self.token:
            raise ValueError("TTS 配置未完成，请在模型配置页或 .env 中填写 DOUBAO_TTS_APPID / DOUBAO_TTS_TOKEN")

        voice = voice_type or self.default_voice
        reqid = uuid.uuid4().hex

        headers = {
            "Content-Type": "application/json",
        }
        if self.auth_method == "api_key":
            headers["X-Api-Key"] = self.api_key
        else:
            headers["Authorization"] = f"Bearer;{self.token}"

        app_config = {"cluster": self.cluster}
        if self.appid:
            app_config["appid"] = self.appid
        if self.auth_method != "api_key" and self.token:
            app_config["token"] = self.token

        payload = {
            "app": app_config,
            "user": {"uid": "auto_video"},
            "audio": {
                "voice_type": voice,
                "encoding": "wav",
                "rate": 24000,
                "speed_ratio": speed_ratio,
                "volume_ratio": volume_ratio,
            },
            "request": {
                "reqid": reqid,
                "text": text,
                "operation": "query",
            },
        }

        for attempt in range(self.max_attempts):
            try:
                resp = requests.post(self.api_url, headers=headers, json=payload, timeout=30)
                if resp.status_code == 429:
                    wait = int(resp.headers.get("Retry-After", 30 * (attempt + 1)))
                    if attempt == self.max_attempts - 1:
                        resp.raise_for_status()
                    logger.warning(f"TTS 限流 429（第{attempt+1}次），等待 {wait}s 后重试")
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                break
            except requests.exceptions.HTTPError as error:
                raise ClassifiedError(
                    classify_exception(error, provider="doubao")
                ) from None
            except Exception as e:
                if attempt == self.max_attempts - 1:
                    raise ClassifiedError(
                        classify_exception(e, provider="doubao")
                    ) from None
                wait = self.retry_interval_seconds * (attempt + 1)
                logger.warning(
                    "TTS 请求失败（第%s次），%ss 后重试",
                    attempt + 1,
                    wait,
                )
                time.sleep(wait)

        data = resp.json()
        if data.get("code") != 3000 or not data.get("data"):
            raise ClassifiedError(
                make_safe_error(ErrorCode.PROVIDER_ERROR, provider="doubao")
            ) from None

        audio_bytes = base64.b64decode(data["data"])
        output_path.write_bytes(audio_bytes)

        logger.debug(f"配音生成成功: {output_path} ({len(audio_bytes)} 字节)")
        return str(output_path)

    def _generate_mimo(
        self,
        text: str,
        filename: str = None,
        voice_type: str = None,
        style_prompt: str = "",
        speed_instruction: str = "",
        clone_data_url: str = None,
    ) -> str:
        output_path = self._output_path(text, filename)
        base_url = (self.mimo_config.get("base_url") or Config.MIMO_TTS_BASE_URL).rstrip("/")
        llm_config = Config.llm_config()
        api_key = (
            self.mimo_config.get("api_key")
            or (self.tts_config.get("api_key") if self.provider == "mimo" else "")
            or Config.MIMO_TTS_API_KEY
            or (llm_config.get("api_key") if isinstance(llm_config, dict) else "")
        )
        if clone_data_url:
            model = self.mimo_config.get("clone_model") or Config.MIMO_TTS_CLONE_MODEL
        else:
            model = self.mimo_config.get("model") or Config.MIMO_TTS_MODEL
        audio_format = (self.mimo_config.get("format") or Config.MIMO_TTS_FORMAT or "wav").lower()
        voice = voice_type or self.mimo_config.get("default_voice") or Config.MIMO_TTS_DEFAULT_VOICE
        instructions = [
            value.strip()
            for value in (
                style_prompt or self.mimo_config.get("style_prompt") or Config.MIMO_TTS_STYLE_PROMPT,
                speed_instruction,
            )
            if value and value.strip()
        ]
        instruction_text = "\n".join(instructions)

        if not api_key:
            raise ValueError("小米 MiMo TTS 配置未完成，请在模型配置页填写 API Key")

        endpoint = base_url if base_url.endswith("/chat/completions") else f"{base_url}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }
        payload = {
            "model": model,
            "messages": [
                {"role": "user", "content": instruction_text},
                {"role": "assistant", "content": text},
            ],
            "audio": {
                "format": audio_format,
                "voice": clone_data_url or voice,
            },
        }

        logger.debug(
            "生成 MiMo 配音: provider=mimo model=%s voice=%s format=%s -> %s",
            model,
            "local-reference" if clone_data_url else voice,
            audio_format,
            output_path,
        )

        resp = None
        for attempt in range(self.max_attempts):
            try:
                resp = requests.post(endpoint, headers=headers, json=payload, timeout=90)
                if resp.status_code == 429:
                    wait = int(resp.headers.get("Retry-After", 60))
                    if attempt == self.max_attempts - 1:
                        resp.raise_for_status()
                    logger.warning(f"小米 MiMo TTS 限流 429（第{attempt+1}次），等待 {wait}s 后重试")
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                break
            except requests.exceptions.HTTPError as error:
                raise ClassifiedError(
                    classify_exception(error, provider="mimo")
                ) from None
            except Exception as e:
                if attempt == self.max_attempts - 1:
                    raise ClassifiedError(
                        classify_exception(e, provider="mimo")
                    ) from None
                wait = self.retry_interval_seconds * (attempt + 1)
                logger.warning(
                    "小米 MiMo TTS 请求失败（第%s次），%ss 后重试",
                    attempt + 1,
                    wait,
                )
                time.sleep(wait)

        data = resp.json()
        try:
            audio_data = data["choices"][0]["message"]["audio"]["data"]
        except (KeyError, IndexError, TypeError) as exc:
            exc = None
            raise ClassifiedError(
                make_safe_error(ErrorCode.PROVIDER_ERROR, provider="mimo")
            ) from None
        if not audio_data:
            raise ClassifiedError(
                make_safe_error(ErrorCode.PROVIDER_ERROR, provider="mimo")
            ) from None

        audio_bytes = base64.b64decode(audio_data)
        output_path.write_bytes(audio_bytes)

        logger.debug(f"小米 MiMo 配音生成成功: {output_path} ({len(audio_bytes)} 字节)")
        return str(output_path)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    gen = VoiceOverGenerator()
    path = gen.generate("人工智能正在改变我们的世界，带来无限可能。", filename="test")
    print(f"音频保存到: {path}")
