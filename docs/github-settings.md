# GitHub Settings

## Repository Identity

Recommended public repository name:

```text
insightcut-jianying-image-video
```

Product name:

```text
InsightCut
```

Recommended description:

```text
InsightCut — AI 图片视频与可编辑剪映草稿工作台：文稿转分镜、图片、配音和字幕，支持 Jianying draft / CapCut draft、MP4 与素材包。
```

Recommended topics:

```text
ai-video
ai-video-generation
ai-video-generator
image-to-video
script-to-video
video-automation
explainer-video
storyboard
tts
voice-cloning
local-first
fastapi
react
ffmpeg
sqlite
capcut
capcut-draft
jianying
jianying-draft
jianying-caogao
```

Public disclaimer:

```text
This project is not affiliated with Jianying, CapCut, or ByteDance.
```

## Branch Protection

Protect `master` with:

- Require a pull request before merging.
- Require status checks to pass before merging.
- Required checks:
  - `Backend tests and audit`
  - `Frontend tests, build and audit`
  - `Repository hygiene`
  - `Real full-stack Playwright`
  - `Filesystem (windows-latest)`
  - `Filesystem (macos-latest)`
  - `Workflow and secret checks`
  - `Website build and browser checks`
- Block force pushes.
- Block branch deletion.
- Apply protection to administrators; require resolved discussions. Single-maintainer PRs do not require a second approver; external PRs need maintainer review.

## PR Policy

- Default to draft PRs until validation is complete.
- Use PR template fields as the merge checklist.
- UI changes need screenshots.
- Generated-output changes need screenshots or compressed video.
- Pipeline changes must preserve assets for failed and partially failed tasks.

## Release Policy

- Distinguish master updates from explicitly requested versioned releases.
- Release notes should include:
  - Product changes.
  - Engineering changes.
  - Validation commands.
  - Screenshots or showcase videos.
  - Known limitations.
