# Content Automation Implementation Roadmap

This directory holds human-approved implementation briefs for Autobuild. It is
an execution index, not a second product roadmap: current product status stays
in [`PROJECT_STATE.md`](../../PROJECT_STATE.md), durable decisions stay in
[`docs/decisions`](../decisions), and validation history stays in
[`docs/evaluations`](../evaluations).

## Layout

```text
docs/roadmap/
  README.md
  <milestone>/
    <implementation-id>-<short-name>.md
```

Use milestone names that match the existing project sequence, for example
`milestone-4.1/`. Each brief must use Autobuild's installed
`templates/implementation-brief.md` contract.

## Planning Workflow

From the repository root, start `codex` or `claude` and ask it to use the
implementation-planning workflow. The planner inspects the relevant project
state, decisions, architecture and code, then works through scope, risks,
dependencies, acceptance criteria and autonomy with you.

The planner writes nothing executable until you explicitly approve the plan.
After approval it creates the milestone directory and brief, updates the index
below, runs `autobuild brief`, reports the autonomy gate and stops. It never
starts `autobuild run`.

## Approved Implementations

| Order | ID | Title | Autonomy | Status | Brief |
| --- | --- | --- | --- | --- | --- |
| 1 | M4.1A | Build Instagram OAuth integration | GREEN | Implemented; pending review | [Brief](milestone-4.1/M4.1A-instagram-oauth-integration.md) |
| 2 | M4.1B | Validate Instagram OAuth against Meta | YELLOW | Live connection proven (@picklebatchapp); token-refresh observation pending | [Brief](milestone-4.1/M4.1B-live-meta-oauth-validation.md) |
| 3 | M4.2 | Instagram Reels publishing | YELLOW (live publishing) | Live Reel published 2026-10-10; restart test pending | Manual brief; see [evaluation](../evaluations/productization/milestone-4.2-instagram-reels-publishing.md) |
| 4 | M4.2.1 | Instagram media normalization | YELLOW (live publishing) | Committed; Supabase-path test, delete cleanup and live 4K pending | [Handover](milestone-4.2.1/M4.2.1-handover.md); see [evaluation](../evaluations/productization/milestone-4.2.1-instagram-media-normalization.md) |
| 5 | M4.2.2 | Retry legacy Instagram media failures | YELLOW (live publishing) | Committed; live retry reached normalization, which was OOM-killed (fixed in M4.2.3) | Manual brief; see [evaluation](../evaluations/productization/milestone-4.2.1-instagram-media-normalization.md) |
| 6 | M4.2.3 | Resource-safe normalization + retry backoff | YELLOW (live publishing) | Implemented and tested; live retry of video 18 pending deploy | Manual brief; see [evaluation](../evaluations/productization/milestone-4.2.3-resource-safe-normalization.md) |

## Handoff

Use the exact path reported by the planner:

```bash
autobuild brief docs/roadmap/<milestone>/<brief>.md
```

Content Automation has `require_clean_git_before_start: true`. Review and
commit the approved roadmap files after validation; the planner does not commit
them. Then:

```bash
autobuild run docs/roadmap/<milestone>/<brief>.md --project . --dry-run
autobuild run docs/roadmap/<milestone>/<brief>.md --project .
```
