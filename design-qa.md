# InsightCut V2.0 Design QA

## Source visual

- Visual baseline: user-approved Spatial Pro white professional workspace.
- Implementation evidence: workspace, centered rotating reticle and asset-version inspector verified at `1440 × 900`.
- Screenshot comparisons are generated as local QA artifacts and intentionally excluded from the repository release.

## Environment and viewport

- Browser surface: Codex in-app browser
- Desktop viewport: `1440 × 900`
- Laptop/tablet viewport: `768 × 900`
- Narrow in-app-browser viewport: `480 × 844` (the browser surface clamps below 480px)
- `390px` behavior is additionally covered by the responsive CSS/Vitest assertions for pane switching and horizontal overflow.
- Project state: completed 9-segment task with images, audio, subtitle, asset history and export capability.

## Full-screen comparison

- Structure matches the approved direction: fixed global rail, 01–06 project steps, storyboard navigation, large central preview, fixed inspector and bottom action bar.
- The implementation preserves the reference's white spatial hierarchy while using the product's real task state, real assets and real controls.
- The global activity capsule is added above the reference composition without displacing the project steps.
- No serif headings, warm-paper styling, neon borders or decorative dead controls remain in the V2 surfaces.

## Focused comparison

- The inspector keeps the reference's four-tab hierarchy: current segment, asset versions, project assets and global settings.
- Asset versions are backed by immutable database records. The inspected segment exposes one unique selected version after legacy-backfill deduplication.
- Restoring a version remains a selection operation; it does not invoke image generation or TTS.
- The implementation intentionally separates current-segment editing from asset history instead of duplicating the removed “当前分镜素材／画面与配音” card group in the center column.

## Browser interactions verified

- Opened and switched all four inspector tabs.
- Expanded the global activity capsule and confirmed running/attention counts and recent task cards.
- From the export center, opened a different project through the activity drawer and verified it stayed in export context (`/export/:taskId`) instead of returning to the workspace.
- Verified the brand reticle changes between 90-degree states while its `26 × 26` bounding box remains fixed at the same coordinates, so it rotates around its own center and cannot fly out of the icon shell.
- Opened `/assets`, `/assets/:taskId`, `/templates` and the persistent `/workspace/:taskId` route.
- Opened and dismissed the production-template modal; native browser prompt/confirm UI is not used.
- Verified the asset library returns one image version for the selected segment after refresh.
- Verified desktop page dimensions are `1440/1440` and tablet dimensions are `768/768` for `scrollWidth/clientWidth`.
- Verified workspace center, storyboard and inspector remain independent scroll regions while the document itself stays viewport-locked.

## Console and network

- No browser console errors or failed API responses were observed.
- Activity polling uses one aggregate `/activity/tasks` request rather than one workspace request per task.
- Workspace and asset-library requests returned HTTP 200 throughout the inspection.

## Findings and iteration

1. Legacy workspace reads were creating duplicate history rows for an already-recorded file. The backfill now reuses the canonical asset record and safely removes only redundant `legacy` rows.
2. A first migration draft used a path-wide unique index, which was too strict for legitimate immutable history. It was replaced with a non-unique lookup index; real historical versions are preserved.
3. Cross-segment selection now creates a target-segment selection version with `origin_asset_id`, while same-segment history recovery only moves the selected pointer.
4. Template CRUD originally used native prompts. It now uses the shared accessible modal and confirmation components to match the V2 visual language.
5. Final source/implementation and focused-inspector comparisons show no blocking spacing, typography, overflow or hierarchy mismatch.
6. The left-rail brand no longer loads the standalone favicon animation; it now uses the AIPM-centered reticle structure with the glow clipped inside the icon shell.
7. Toasts, manuscript summaries, inline asset errors and export notes now use a complete low-saturation state border instead of a single colored left stripe.

## Automated verification

- Backend: `308 passed`
- Frontend Node tests: `118 passed`
- Frontend Vitest: `27 passed`
- Production build: passed
- Build warning: the existing main JavaScript chunk remains slightly above Vite's 500 kB advisory threshold (`507.64 kB`); it is not a functional or visual blocker.

Final result: passed
