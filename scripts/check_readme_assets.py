#!/usr/bin/env python3
"""Validate public documentation links, brand assets, and frontend references."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "ai-kepu-video-web" / "frontend"

DOCUMENTS = (
    (ROOT / "README.md", ROOT),
    (ROOT / "README_EN.md", ROOT),
    (ROOT / "docs" / "showcase" / "index.html", ROOT / "docs" / "showcase"),
)

MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
MARKDOWN_LINK_RE = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
HTML_REFERENCE_RE = re.compile(
    r'(?:src|srcset|poster|href)=["\']([^"\']+)["\']', re.IGNORECASE
)
REFERENCE_ASSET_RE = re.compile(r"/reference-assets/([A-Za-z0-9._-]+)")
ASSET_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".mp4", ".webm", ".mov"}
SOURCE_SUFFIXES = {".js", ".jsx", ".ts", ".tsx", ".css"}
BRAND_BLUE = "#315fea"
BRAND_ORANGE = "#d46f44"
STALE_BRAND_COLORS = {"#2563eb", "#f472b6", "#f08a4b"}


def normalized_local_target(raw: str) -> Optional[str]:
    target = raw.strip()
    if target.startswith("#"):
        return None
    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc or not parsed.path:
        return None
    return unquote(parsed.path)


def document_reference_errors() -> tuple[list[str], int, int]:
    errors: list[str] = []
    checked: set[Path] = set()
    checked_media: set[Path] = set()

    for document, base_dir in DOCUMENTS:
        if not document.is_file():
            errors.append(f"missing public document: {document.relative_to(ROOT)}")
            continue

        text = document.read_text(encoding="utf-8")
        references: set[str] = set()
        for pattern in (MARKDOWN_IMAGE_RE, MARKDOWN_LINK_RE, HTML_REFERENCE_RE):
            references.update(pattern.findall(text))

        for raw in references:
            target = normalized_local_target(raw)
            if target is None:
                continue
            path = (base_dir / target).resolve()
            try:
                path.relative_to(ROOT)
            except ValueError:
                errors.append(
                    f"{document.relative_to(ROOT)} references path outside repository: {target}"
                )
                continue
            checked.add(path)
            if path.suffix.lower() in ASSET_SUFFIXES:
                checked_media.add(path)
            if not path.exists():
                errors.append(
                    f"{document.relative_to(ROOT)} references missing local target: {target}"
                )

    return errors, len(checked), len(checked_media)


def brand_identity_errors() -> tuple[list[str], int]:
    errors: list[str] = []
    tokens_path = FRONTEND / "src" / "styles" / "tokens.css"
    index_path = FRONTEND / "index.html"
    brand_files = (
        ROOT / "docs" / "assets" / "insightcut-mark.svg",
        ROOT / "docs" / "assets" / "insightcut-mark-dark.svg",
        FRONTEND / "public" / "favicon.svg",
    )

    tokens = tokens_path.read_text(encoding="utf-8").lower()
    if not re.search(r"--color-accent:\s*#315fea\b", tokens):
        errors.append("frontend brand blue token must be #315FEA")
    if not re.search(r"--color-orange:\s*#d46f44\b", tokens):
        errors.append("frontend warm orange token must be #D46F44")

    for path in brand_files:
        if not path.is_file():
            errors.append(f"missing brand asset: {path.relative_to(ROOT)}")
            continue
        content = path.read_text(encoding="utf-8").lower()
        if BRAND_BLUE not in content or BRAND_ORANGE not in content:
            errors.append(
                f"{path.relative_to(ROOT)} must use the canonical blue and warm orange"
            )
        stale = sorted(color for color in STALE_BRAND_COLORS if color in content)
        if stale:
            errors.append(
                f"{path.relative_to(ROOT)} contains stale brand colors: {', '.join(stale)}"
            )

    index = index_path.read_text(encoding="utf-8").lower()
    if not re.search(r'<meta\s+name="theme-color"\s+content="#315fea"\s*/?>', index):
        errors.append("frontend theme-color must match brand blue #315FEA")

    return errors, len(brand_files) + 2


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
    document_errors, document_count, media_count = document_reference_errors()
    reference_errors, reference_count = frontend_reference_errors()
    brand_errors, brand_count = brand_identity_errors()
    errors = document_errors + reference_errors + brand_errors

    if errors:
        print("Repository asset check failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    print(
        "Repository asset check passed: "
        f"{document_count} public local targets ({media_count} media), "
        f"{brand_count} brand checks, and {reference_count} frontend reference assets."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
