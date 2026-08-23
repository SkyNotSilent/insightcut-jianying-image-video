import base64
import logging
import wave
from pathlib import Path

import pytest
import requests

from src.api.error_model import ClassifiedError
from src.config import Config
from src.draft.voice_preview import VoicePreviewService
from src.draft.voiceover import VoiceOverGenerator


class FakeResponse:
    status_code = 200
    headers = {}

    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def wav_bytes(seconds=0.05, sample_rate=24000):
    import io

    target = io.BytesIO()
    with wave.open(target, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(b"\x01\x00" * int(seconds * sample_rate))
    return target.getvalue()


def test_doubao_401_is_wrapped_as_safe_auth_error(tmp_path, monkeypatch, tts_config):
    secret = "doubao-provider-secret"

    class UnauthorizedResponse:
        status_code = 401
        headers = {"x-request-id": "req-doubao-auth"}

        def raise_for_status(self):
            raise requests.HTTPError(
                f"Authorization: Bearer {secret}", response=self
            )

    config = {**tts_config, "api_key": secret}
    monkeypatch.setattr(
        Config,
        "generation_config",
        classmethod(lambda cls: {"retry_count": 0, "retry_interval_seconds": 1}),
    )
    monkeypatch.setattr(
        "src.draft.voiceover.requests.post",
        lambda *_args, **_kwargs: UnauthorizedResponse(),
    )
    generator = VoiceOverGenerator(str(tmp_path), tts_config=config)

    with pytest.raises(ClassifiedError) as exc_info:
        generator.generate("测试语音", filename="unauthorized")

    safe = exc_info.value.safe_error
    assert safe.code.value == "auth"
    assert safe.request_id == "req-doubao-auth"
    assert secret not in str(exc_info.value)


@pytest.fixture
def tts_config():
    return {
        "provider": "doubao",
        "enabled_providers": ["doubao", "mimo"],
        "auth_method": "api_key",
        "api_url": "https://doubao.invalid/tts",
        "api_key": "doubao-key",
        "cluster": "volcano_tts",
        "default_voice": "zh_male_jieshuoxiaoming_moon_bigtts",
        "speed_level": "normal",
        "volume_ratio": 1.0,
        "mimo": {
            "base_url": "https://mimo.invalid/v1",
            "api_key": "mimo-key",
            "model": "mimo-v2.5-tts",
            "clone_model": "mimo-v2.5-tts-voiceclone",
            "default_voice": "冰糖",
            "format": "wav",
            "style_prompt": "自然清晰",
            "speed_level": "normal",
        },
    }


def test_routes_doubao_per_call_and_maps_speed_volume(tmp_path, monkeypatch, tts_config):
    captured = {}

    def fake_post(url, **kwargs):
        captured.update({"url": url, **kwargs})
        return FakeResponse({"code": 3000, "data": base64.b64encode(wav_bytes()).decode()})

    monkeypatch.setattr("src.draft.voiceover.requests.post", fake_post)
    generator = VoiceOverGenerator(str(tmp_path), tts_config=tts_config)

    result = generator.generate(
        "测试语音",
        filename="doubao",
        voice_type="doubao:zh_female_shuangkuaisisi_moon_bigtts",
        speed_level="very_fast",
        volume_ratio=1.8,
    )

    assert Path(result).exists()
    assert captured["url"] == "https://doubao.invalid/tts"
    assert captured["json"]["audio"] == {
        "voice_type": "zh_female_shuangkuaisisi_moon_bigtts",
        "encoding": "wav",
        "rate": 24000,
        "speed_ratio": 1.5,
        "volume_ratio": 1.8,
    }


def test_doubao_regular_retry_uses_configured_interval(tmp_path, monkeypatch, tts_config):
    attempts = 0
    sleeps = []

    def fake_post(_url, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary failure")
        return FakeResponse({"code": 3000, "data": base64.b64encode(wav_bytes()).decode()})

    monkeypatch.setattr(
        Config,
        "generation_config",
        classmethod(
            lambda cls: {"retry_count": 2, "retry_interval_seconds": 4}
        ),
    )
    monkeypatch.setattr("src.draft.voiceover.requests.post", fake_post)
    monkeypatch.setattr("src.draft.voiceover.time.sleep", sleeps.append)

    generator = VoiceOverGenerator(str(tmp_path), tts_config=tts_config)
    generator.generate("测试重试", filename="retry")

    assert attempts == 2
    assert sleeps == [4]


def test_routes_mimo_and_combines_style_with_speed_instruction(tmp_path, monkeypatch, tts_config):
    captured = {}

    def fake_post(url, **kwargs):
        captured.update({"url": url, **kwargs})
        audio = base64.b64encode(wav_bytes()).decode()
        return FakeResponse({"choices": [{"message": {"audio": {"data": audio}}}]})

    monkeypatch.setattr("src.draft.voiceover.requests.post", fake_post)
    generator = VoiceOverGenerator(str(tmp_path), tts_config=tts_config)
    generator.generate(
        "一段中文",
        filename="mimo",
        voice_type="mimo:茉莉",
        speed_level="slow",
        style_prompt="温柔有感情",
    )

    payload = captured["json"]
    assert captured["url"] == "https://mimo.invalid/v1/chat/completions"
    assert payload["model"] == "mimo-v2.5-tts"
    assert payload["audio"]["voice"] == "茉莉"
    assert payload["messages"][0]["role"] == "user"
    assert "温柔有感情" in payload["messages"][0]["content"]
    assert "语速偏慢" in payload["messages"][0]["content"]


def test_clone_uses_clone_model_and_in_memory_reference_without_logging_it(
    tmp_path, monkeypatch, tts_config, caplog
):
    captured = {}
    secret_data_url = "data:audio/wav;base64,DO_NOT_LOG_THIS_REFERENCE"

    class FakeCloneStore:
        def reference_data_url(self, clone_id):
            assert clone_id == "abc123"
            return secret_data_url

    def fake_post(url, **kwargs):
        captured.update({"url": url, **kwargs})
        audio = base64.b64encode(wav_bytes()).decode()
        return FakeResponse({"choices": [{"message": {"audio": {"data": audio}}}]})

    monkeypatch.setattr("src.draft.voiceover.requests.post", fake_post)
    generator = VoiceOverGenerator(
        str(tmp_path), tts_config=tts_config, clone_store=FakeCloneStore()
    )
    with caplog.at_level(logging.DEBUG):
        generator.generate(
            "克隆试听", filename="clone", voice_type="mimo-clone:abc123"
        )

    payload = captured["json"]
    assert payload["model"] == "mimo-v2.5-tts-voiceclone"
    assert payload["audio"]["voice"] == secret_data_url
    assert secret_data_url not in caplog.text
    assert "DO_NOT_LOG_THIS_REFERENCE" not in caplog.text


def test_preset_preview_cache_is_stable_per_voice(
    tmp_path, monkeypatch, tts_config
):
    calls = []

    def fake_generate(self, text, filename=None, **kwargs):
        calls.append({"text": text, "filename": filename, **kwargs})
        target = self.output_dir / f"{filename}.wav"
        target.write_bytes(wav_bytes())
        return str(target)

    monkeypatch.setattr(VoiceOverGenerator, "generate", fake_generate)
    service = VoicePreviewService(base_dir=tmp_path, tts_config=tts_config)

    first = service.generate("mimo:冰糖", "你好", {"speed_level": "normal"})
    second = service.generate("mimo:冰糖", "你好", {"speed_level": "normal"})
    changed_text = service.generate("mimo:冰糖", "你好呀", {"speed_level": "normal"})
    changed_voice = service.generate("mimo:茉莉", "你好", {"speed_level": "normal"})
    changed_options = service.generate("mimo:冰糖", "你好", {"speed_level": "fast"})
    changed_model = service.generate(
        "mimo:冰糖",
        "你好",
        {"speed_level": "normal"},
        config_override={"mimo": {**tts_config["mimo"], "model": "mimo-next"}},
    )

    assert first["cached"] is False
    assert second == {**first, "cached": True}
    assert len(calls) == 4
    assert changed_text["path"] == first["path"]
    assert changed_options["path"] != first["path"]
    assert changed_model["path"] != first["path"]
    assert changed_voice["path"] != first["path"]
    assert first["url"].startswith("/media/_voice_previews/")
