# Repository Hygiene

The public repository should contain source code, documentation, small showcase media, and safe examples. It should not contain private runtime state or raw local generation output.

## Commit

- Source code.
- Tests and test fixtures that do not include secrets.
- `.env.example` files with empty or placeholder values.
- Curated README screenshots under `docs/screenshots/` and brand assets under `docs/assets/`.
- Compressed showcase videos under `docs/showcase/`.
- Product and engineering documentation under `docs/`.

## Do Not Commit

- Real `.env` files.
- API keys, tokens, credentials, cookies, or local account data.
- SQLite databases such as `local.db`.
- `data/`, `output/`, `logs/`, `dist/`, `.vite/`, `node_modules/`, `venv/`, `.venv/`.
- Playwright/Codex/browser session state such as `.playwright-mcp/`.
- Local design exploration and QA output such as `artifacts/`, `design-qa-artifacts/`, `design-preview/`, `docs/design-exploration/`, and `docs/superpowers/`.
- Raw full-resolution generated videos unless there is an explicit release reason.

## Showcase Media Rules

- README demo videos should be compressed and placed in `docs/showcase/videos/`.
- README thumbnails should be placed in `docs/showcase/thumbs/`.
- Keep each public demo video under 10 MB when practical.
- Keep original generated media in local `output/` or `data/` only.

## Pre-PR Checks

```bash
git status --ignored --short
git ls-files | grep -E '(^|/)(\.env$|.*\.db$|.*\.sqlite$|.*\.sqlite3$|logs/|output/|data/local\.db|\.playwright-mcp/|artifacts/|design-qa-artifacts/|design-preview/|docs/design-exploration/|docs/superpowers/)'
machine_path='/Users'"/mima1234"
codex_runtime='\.cache/codex'"-runtimes"
git grep -n -I -E "${machine_path}|${codex_runtime}" -- .
python3 scripts/check_readme_assets.py
```

The two `git` checks should print nothing.
