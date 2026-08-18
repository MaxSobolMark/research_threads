---
name: research-dashboard
description: "Register and maintain research threads on the Research Threads dashboard (local web app). Use when the user says to start/track a research thread, when they ask to note/record/send something to the dashboard, and — for sessions already registered as a thread — to keep the thread's Current Status section updated after significant progress and post notes/plots/links at milestones."
---

# Research Dashboard

The user tracks selected agent sessions as *research threads* on a local
dashboard (http://localhost:7878). Threads are user-initiated: a session
becomes a thread only when the user says so. Once registered, this session is
identified automatically (via `$CLAUDE_VTERM_NAME` and the working directory)
— no ids needed. Everything goes through the `rt` CLI (`~/.local/bin/rt`,
installed on PATH), which manages the server itself.

## Starting a thread — only when the user asks

When the user says "track this as a research thread", "start a research thread
about X", or similar:

```bash
rt start short-name -o "One-sentence objective of the investigation"
```

- `short-name`: kebab-case, specific (`value-fn-collapse`, not `experiments`).
- The objective states the research question or goal in one sentence. Draft
  it from what the user said; if the goal is genuinely unclear, ask.
- Never register a thread on your own initiative — ordinary coding sessions
  are not research threads.

## Keeping Current Status updated — your standing duty

Each thread shows a **Current Status** section: where the investigation
stands right now, plus outstanding TODOs. Once this session is a registered
thread, you own that section. Replace it with `rt update` whenever the state
of the investigation changes — after a result lands, a run is launched, a
direction changes — and always before ending a turn that moved the work:

```bash
rt update "H1 rejected: collapse persists without the value fn (3 seeds).
Now sweeping target-update tau. TODO: analyze failure episodes; rerun seed 2."
```

- 1–4 sentences: current state first, then `TODO:` with what remains.
- It replaces the previous status — always write the full current picture.
- Concrete numbers and configs, not "made progress".
- `rt objective "…"` only if the user redefines the goal itself.

## Notes, plots, links — the running log

```bash
rt note "success 41% -> 57% with lr 1e-4, 3 seeds"
rt plot results/loss_curve.png -m "Loss vs. baseline, 5 seeds, lr 1e-4"
rt link https://wandb.ai/entity/proj/runs/abc -m "sweep run"
rt note -   # multi-line note from stdin
```

Post at milestones: a result worth keeping, a decision made, a plot produced,
or hand-off state when stopping mid-task. One thought per note, headline
first, facts before interpretation. Don't narrate routine work, and never
post secrets. If `rt` reports no registered thread, do not register one
yourself — ask the user whether to start one.
