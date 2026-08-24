#!/usr/bin/env python3
"""Validate public documentation media and frontend reference assets."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "ai-kepu-video-web" / "frontend"

DOCUMENTS = (
    (ROOT / "README.md", ROOT),
    (ROOT / "README_EN.md", ROOT),
    (ROOT / "docs" / "showcase" / "index.html", ROOT / "docs" / "showcase"),
)

MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
MARKDOWN_LINK_RE = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
HTML_ASSET_RE = re.compile(r'(?:src|poster|href)=["\']([^"\']+)["\']', re.IGNORECASE)
REFERENCE_ASSET_RE = re.compile(r"/reference-assets/([A-Za-z0-9._-]+)")
ASSET_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".mp4", ".webm", ".mov"}
SOURCE_SUFFIXES = {".js", ".jsx", ".ts", ".tsx", ".css"}


def normalized_local_asset(raw: str) -> Optional[str]:
    target = raw.strip().split("#", 1)[0].split("?", 1)[0]
    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc or Path(target).suffix.lower() not in ASSET_SUFFIXES:
        return None
    return target


def document_asset_errors() -> tuple[list[str], int]:
    errors: list[str] = []
    checked: set[Path] = set()

    for document, base_dir in DOCUMENTS:
        if not document.is_file():
            errors.append(f"missing public document: {document.relative_to(ROOT)}")
            continue

        text = document.read_text(encoding="utf-8")
        references: set[str] = set()
        for pattern in (MARKDOWN_IMAGE_RE, MARKDOWN_LINK_RE, HTML_ASSET_RE):
            references.update(pattern.findall(text))

        for raw in references:
            target = normalized_local_asset(raw)
            if target is None:
                continue
            path = (base_dir / target).resolve()
            checked.add(path)
            if not path.is_file():
                errors.append(
                    f"{document.relative_to(ROOT)} references missing asset: {target}"
                )

    return errors, len(checked)


def frontend_reference_errors() -> tuple[list[str], int]:
    references: set[str] = set()
    for path in (FRONTEND / "src").rglob("*"):
        if path.is_file() and path.suffix in SOURCE_SUFFIXES:
            references.update(REFERENCE_ASSET_RE.findall(path.read_text(encoding="utf-8")))

    assets_dir = FRONTEND / "public" / "reference-assets"
    missing = sorted(name for name in references if not (assets_dir / name).is_file())
    errors = [f"frontend references missing /reference-assets/{name}" for name in missing]
    return errors, len(references)


def main() -> int:
    document_errors, document_count = document_asset_errors()
    reference_errors, reference_count = frontend_reference_errors()
    errors = document_errors + reference_errors

    if errors:
        print("Repository asset check failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    print(
        "Repository asset check passed: "
        f"{document_count} public media files and {reference_count} frontend reference assets."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
