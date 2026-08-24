## Summary

<!-- What changed and why? Keep this short and specific. -->

## Changes

-

## Screenshots / Videos

<!-- Required for UI, generated-video, README, or workflow-visible changes. Use "Not applicable" only for backend-only/internal changes. -->

## Validation

- [ ] Backend check: `cd ai-kepu-video-server && python -m compileall src api_server.py`
- [ ] Frontend build: `cd ai-kepu-video-web/frontend && npm run build`
- [ ] Repository asset check: `python3 scripts/check_readme_assets.py`
- [ ] Manual page/task check described below, if relevant:

## Data / Migration Impact

- [ ] No local database, output media, API key, or generated raw asset is committed.
- [ ] No migration is required.
- [ ] If generated-task behavior changes, failed/partial tasks still preserve generated assets.

## Risk / Rollback

<!-- What could break, and how do we revert or disable the change? -->

## Checklist

- [ ] Branch name follows `codex/feature-*`, `codex/fix-*`, `codex/docs-*`, or `codex/chore-*`.
- [ ] PR is scoped to one intent: feature, fix, docs, chore, or cleanup.
- [ ] Public copy does not imply official affiliation with Jianying, CapCut, or ByteDance.
- [ ] README screenshots/videos are compressed showcase assets, not raw local output.
