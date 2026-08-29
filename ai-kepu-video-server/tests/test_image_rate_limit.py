import base64
from types import SimpleNamespace

import pytest
import requests

from src.api.error_model import ClassifiedError
from src.config import Config
from src.media import image_generator
from src.media.image_generator import ImageGenerator


def test_image_retry_delay_uses_configured_interval_for_regular_failures():
    generator = object.__new__(ImageGenerator)
    generator.retry_interval_seconds = 7

    assert generator._retry_delay(None, attempt=0) == 7
    assert generator._retry_delay(None, attempt=1) == 14


def test_image_retry_delay_prefers_provider_retry_after_for_rate_limits():
    generator = object.__new__(ImageGenerator)
    generator.retry_interval_seconds = 7
    response = SimpleNamespace(status_code=429, headers={"retry-after": "11"})

    assert generator._retry_delay(response, attempt=2) == 11


def test_image_rate_limiter_allows_measured_eight_request_burst(monkeypatch):
    image_generator._IMAGE_REQUEST_TIMESTAMPS.clear()
    monkeypatch.setattr(image_generator.time, "monotonic", lambda: 100.0)
    sleeps = []
    monkeypatch.setattr(image_generator.time, "sleep", sleeps.append)
    generator = object.__new__(ImageGenerator)

    for _ in range(8):
        generator._wait_for_rate_limit()

    assert len(image_generator._IMAGE_REQUEST_TIMESTAMPS) == 8
    assert sleeps == []


def test_image_rate_limiter_waits_after_twenty_requests_in_rolling_minute(monkeypatch):
    image_generator._IMAGE_REQUEST_TIMESTAMPS.clear()
    image_generator._IMAGE_REQUEST_TIMESTAMPS.extend([100.0] * 20)
    now = [100.0]
    sleeps = []

    monkeypatch.setattr(image_generator.time, "monotonic", lambda: now[0])

    def advance(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(image_generator.time, "sleep", advance)
    generator = object.__new__(ImageGenerator)

    generator._wait_for_rate_limit()

    assert sleeps == [60.0]
    assert list(image_generator._IMAGE_REQUEST_TIMESTAMPS) == [160.0]


def test_image_401_is_wrapped_as_safe_auth_error(tmp_path, monkeypatch):
    secret = "sk-image-provider-secret"

    class UnauthorizedResponse:
        status_code = 401
        headers = {"x-request-id": "req-image-auth"}

        def raise_for_status(self):
            raise requests.HTTPError(
                f"Authorization: Bearer {secret}", response=self
            )

    monkeypatch.setattr(
        Config,
        "image_config",
        classmethod(lambda cls: {
            "api_url": "https://image.invalid/v1/images/generations",
            "api_key": secret,
            "model": "fake-image",
        }),
    )
    monkeypatch.setattr(
        Config,
        "generation_config",
        classmethod(lambda cls: {"retry_count": 0, "retry_interval_seconds": 1}),
    )
    monkeypatch.setattr(image_generator.requests, "post", lambda *_args, **_kwargs: UnauthorizedResponse())
    monkeypatch.setattr(ImageGenerator, "_wait_for_rate_limit", lambda self: None)
    generator = ImageGenerator(str(tmp_path))

    with pytest.raises(ClassifiedError) as exc_info:
        generator.generate("safe prompt")

    safe = exc_info.value.safe_error
    assert safe.code.value == "auth"
    assert safe.request_id == "req-image-auth"
    assert secret not in str(exc_info.value)


def test_content_policy_rejection_uses_safe_fallback_without_sleep(tmp_path, monkeypatch):
    calls = []
    sleeps = []

    class FakeResponse:
        def __init__(self, status_code, body, headers=None):
            self.status_code = status_code
            self._body = body
            self.headers = headers or {}

        def json(self):
            return self._body

        def raise_for_status(self):
            if self.status_code >= 400:
                raise requests.HTTPError("provider rejected request", response=self)

    responses = [
        FakeResponse(
            400,
            {
                "error": {
                    "type": "invalid_request_error",
                    "code": "content_policy_violation",
                    "message": "prompt rejected",
                    "param": "prompt",
                }
            },
            {"cf-ray": "req-policy-1"},
        ),
        FakeResponse(
            200,
            {
                "data": [
                    {
                        "b64_json": base64.b64encode(
                            b"\x89PNG\r\n\x1a\npolicy-fallback"
                        ).decode()
                    }
                ]
            },
        ),
    ]

    def fake_post(_url, *, headers, json, timeout):
        calls.append(dict(json))
        return responses.pop(0)

    monkeypatch.setattr(
        Config,
        "image_config",
        classmethod(lambda cls: {
            "api_url": "https://image.invalid/v1/images/generations",
            "api_key": "safe-test-key",
            "model": "fake-image",
        }),
    )
    monkeypatch.setattr(
        Config,
        "generation_config",
        classmethod(lambda cls: {"retry_count": 2, "retry_interval_seconds": 5}),
    )
    monkeypatch.setattr(image_generator.requests, "post", fake_post)
    monkeypatch.setattr(image_generator.time, "sleep", sleeps.append)
    monkeypatch.setattr(ImageGenerator, "_wait_for_rate_limit", lambda self: None)

    generator = ImageGenerator(str(tmp_path))
    path = generator.generate(
        "A hopeful person surrounded by broken chains and flowers",
        style="油彩画",
    )

    assert len(calls) == 2
    assert "broken chains" in calls[0]["prompt"]
    assert "broken chains" not in calls[1]["prompt"]
    assert sleeps == []
    assert path.fallback_used is True
    assert path.submitted_prompt == calls[1]["prompt"]
    assert path.requested_prompt == calls[0]["prompt"]
    assert (tmp_path / "segment_000.png").is_file()


def test_non_retryable_bad_request_is_not_replayed(tmp_path, monkeypatch):
    calls = []

    class BadRequestResponse:
        status_code = 400
        headers = {"x-request-id": "req-bad-request"}

        def json(self):
            return {
                "error": {
                    "type": "invalid_request_error",
                    "code": "invalid_size",
                    "message": "unsupported size",
                }
            }

        def raise_for_status(self):
            raise requests.HTTPError("bad request", response=self)

    monkeypatch.setattr(
        Config,
        "image_config",
        classmethod(lambda cls: {
            "api_url": "https://image.invalid/v1/images/generations",
            "api_key": "safe-test-key",
            "model": "fake-image",
        }),
    )
    monkeypatch.setattr(
        Config,
        "generation_config",
        classmethod(lambda cls: {"retry_count": 5, "retry_interval_seconds": 1}),
    )

    def fake_post(*_args, **_kwargs):
        calls.append(1)
        return BadRequestResponse()

    monkeypatch.setattr(image_generator.requests, "post", fake_post)
    monkeypatch.setattr(ImageGenerator, "_wait_for_rate_limit", lambda self: None)
    generator = ImageGenerator(str(tmp_path))

    with pytest.raises(ClassifiedError) as exc_info:
        generator.generate("safe prompt")

    assert len(calls) == 1
    assert exc_info.value.safe_error.code.value == "provider_error"
    assert exc_info.value.safe_error.retryable is False


def test_agnes_text_to_image_payload_uses_supported_shape(tmp_path, monkeypatch):
    calls = []

    class SuccessResponse:
        status_code = 200
        headers = {}

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "data": [{
                    "b64_json": base64.b64encode(
                        b"\x89PNG\r\n\x1a\nagnes-payload"
                    ).decode()
                }]
            }

    monkeypatch.setattr(
        Config,
        "image_config",
        classmethod(lambda cls: {
            "api_url": "https://apihub.agnes-ai.com/v1/images/generations",
            "api_key": "safe-test-key",
            "model": "agnes-image-2.1-flash",
            "size": "auto",
        }),
    )
    monkeypatch.setattr(
        Config,
        "generation_config",
        classmethod(lambda cls: {"retry_count": 0, "retry_interval_seconds": 1}),
    )

    def fake_post(_url, *, headers, json, timeout):
        calls.append(dict(json))
        return SuccessResponse()

    monkeypatch.setattr(image_generator.requests, "post", fake_post)
    monkeypatch.setattr(ImageGenerator, "_wait_for_rate_limit", lambda self: None)

    ImageGenerator(str(tmp_path)).generate(
        "safe prompt", width=1920, height=1080
    )

    assert calls == [{
        "model": "agnes-image-2.1-flash",
        "prompt": "safe prompt, photorealistic, cinematic lighting, 4k, high detail",
        "n": 1,
        "size": "1024x576",
        "extra_body": {"response_format": "url"},
    }]
