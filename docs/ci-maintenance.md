# CI dependency and media-tool maintenance

## 2026-11-01 repair

[Scheduled run 40288605267](https://github.com/SkyNotSilent/insightcut-jianying-image-video/actions/runs/40288605267) passed the backend's 426 tests and the frontend tests, but failed both dependency audits. Its full-stack job never reached Playwright: the Ubuntu Azure mirror delivered FFmpeg dependencies slowly until the job's 20-minute timeout. Treat test failures, dependency alerts, and installation stalls as separate evidence.

The repair updates the affected npm lock entries within their existing supported ranges. The backend updates aiohttp, certifi, filelock, multidict, Pillow, and urllib3; aiohttp also requires a newer yarl. A fresh audit also found newer alerts in litellm 1.100.1 (CVE-2026-114612) and msgpack 1.2.2 (CVE-2026-114613); targeted patch upgrades address these as well. Directly declared security minimums are raised. Filelock and Pillow cross major versions, so audit success alone cannot establish compatibility: run the backend tests, cross-platform filesystem CI, and real media/draft verification.

### Reproduce dependency maintenance

From `ai-kepu-video-web/frontend`, use:

```sh
npm audit --package-lock-only --audit-level=high
npm audit fix --package-lock-only
npm ci
```

If npm 10's Arborist crashes with `Cannot read properties of null (reading 'edgesOut')`, use the compatible npm 11 CLI on Node.js 22:

```sh
npx -y npm@11 audit fix --package-lock-only
```

Inspect the resulting lock diff. Do not use `--force` or change the audit threshold. The current lock retains five moderate findings in the ESLint/minimatch dependency chain ([GHSA-g3hv-75wg-m249](https://github.com/advisories/GHSA-g3hv-75wg-m249)). npm's proposed forced remedy downgrades ESLint to 4, which is incompatible with this project's configuration. Keep these findings visible and reassess a supported upstream fix; passing the existing high-severity gate does not mean zero vulnerabilities.

From `ai-kepu-video-server`, regenerate the universal Python 3.11 lock with hashes and targeted upgrades:

```sh
uv pip compile requirements-dev.txt --python-version 3.11 --universal \
  --generate-hashes --output-file requirements-dev.lock \
  --upgrade-package aiohttp --upgrade-package certifi \
  --upgrade-package filelock --upgrade-package 'multidict>=6.9.1,<7' \
  --upgrade-package pillow --upgrade-package urllib3 \
  --upgrade-package 'litellm>=1.100.4,<1.101' \
  --upgrade-package 'msgpack>=1.2.3,<1.3'
python -m pip install --require-hashes -r requirements-dev.lock
python -m pip check
pip-audit --disable-pip --no-deps -r requirements-dev.lock
```

Review transitive changes and all platform markers. Do not hand-edit package hashes or suppress vulnerability IDs.

### Linux media tools

Both Linux media jobs call `scripts/install_ci_media_tools.sh`; the backend adds `--pdf`. The helper runs `ffmpeg` and `ffprobe` (plus `pdftotext` and `pdftoppm` for PDF support) before deciding whether installation is needed, and verifies them again afterward. A successful package-manager exit alone is insufficient.

When tools are missing or broken, APT uses an isolated temporary source list pointing to official Ubuntu archive/security HTTPS mirrors. Distribution signature verification stays enabled. APT gets two retries and 30-second network timeouts; separate wall-clock limits allow 90 seconds for updating indexes and 240 seconds for installation. The workflow step allows six minutes, retaining the job's existing 20-minute limit. A continuously slow mirror therefore cannot consume the entire test budget. Failure or timeout exits nonzero.

The four offline behavior tests in `scripts/test_ci_media_tools.py` cover already-working tools, installation sources/bounds, APT failure, and broken tools after installation. Real CI validates the Linux execution path; local mocked tests do not prove mirror availability.

The weekly CI cron and weekly Dependabot schedules remain enabled. Both security audits and all browser/media checks remain required; no vulnerability ignore list or `continue-on-error` was added.
