import asyncio

from fastapi.middleware.cors import CORSMiddleware

import api_server


def test_cors_is_limited_to_local_frontend_origins():
    middleware = next(
        item for item in api_server.app.user_middleware if item.cls is CORSMiddleware
    )

    assert set(middleware.kwargs["allow_origins"]) == {
        "http://localhost:2001",
        "http://127.0.0.1:2001",
    }
    assert middleware.kwargs["allow_credentials"] is False
    assert "*" not in middleware.kwargs["allow_origins"]


def test_media_response_does_not_add_wildcard_cors(monkeypatch, tmp_path):
    output = tmp_path / "output"
    legacy = tmp_path / "legacy"
    output.mkdir()
    legacy.mkdir()
    (output / "sample.png").write_bytes(b"\x89PNG\r\n\x1a\ncontent")
    monkeypatch.setattr(api_server, "output_dir", output)
    monkeypatch.setattr(api_server, "legacy_media_dir", legacy)

    response = asyncio.run(api_server.serve_media("sample.png"))

    assert response.media_type == "image/png"
    assert "access-control-allow-origin" not in response.headers
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"
