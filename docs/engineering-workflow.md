# Engineering Workflow

This repository uses a stable `master` branch with pull requests for all product, engineering, and documentation changes.

## Branches

- `master`: stable local-first product version. Do not push directly.
- `codex/feature-*`: user-facing feature or workflow change.
- `codex/fix-*`: bug fix or regression repair.
- `codex/docs-*`: README, PRD, screenshots, GitHub, or documentation changes.
- `codex/chore-*`: CI, cleanup, dependency, or repository maintenance.

Large or risky rewrites should use a separate worktree, for example:

```bash
git worktree add ../Auto-jianji-v2 -b codex/feature-redesign master
```

## Pull Requests

Each PR should have one intent. Avoid mixing UI redesign, backend behavior, generated assets, and repository cleanup in one PR unless they are inseparable.

Every PR must include:

- Summary of the user or engineering problem.
- Concrete change list.
- Screenshots or videos for UI and generated-output changes.
- Validation commands and manual checks.
- Data/migration impact.
- Risk and rollback note.

## Validation Gates

Run these before requesting review:

```bash
cd ai-kepu-video-server
python -m compileall src api_server.py

cd ../ai-kepu-video-web/frontend
npm run build

cd ../..
python3 scripts/check_readme_assets.py
```

For generation pipeline changes, also validate that failed or partially failed tasks still preserve generated scripts, prompts, images, audio, and draft assets.

## Release Rhythm

- Merge only PRs that keep the local app runnable.
- A master update is not a versioned release. Create a version tag only for an explicitly requested version release.
- Release notes should include new behavior, fixes, validation, screenshots/videos, and known limitations.

## GitHub Settings

Protect `master` after this workflow lands:

- Require a pull request before merging.
- Require CI checks to pass.
- Block force pushes.
- Block deletion of `master`.
- Apply branch protection to administrators. Do not bypass PRs or CI.

For external Fork setup, development dependencies and full validation commands, follow [CONTRIBUTING.md](../CONTRIBUTING.md).
