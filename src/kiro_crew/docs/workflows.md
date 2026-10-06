# Dynamic workflows

A dynamic workflow turns a goal into a multi-phase run you can watch, restart in
part, and save for reuse. Ask for one in plain language and the agent authors the
orchestration script itself, then runs it in the background — you get a run id
immediately and the result arrives back in the chat when it finishes.

Reach for a workflow instead of a plain sub-agent fan-out when the work has
distinct phases, when you want to see it progress, or when you expect to re-run
part of it after changing your mind about one step.

## Starting one

Say what you want. "Use a workflow to compare these three approaches and
recommend one" is enough — the agent passes your goal as the intent, and the
authoring and launching happen in one step. You do not write the script.

Two watching surfaces show a live run:

- The chat side panel's **Workflows** tab, next to Changes and Subagents.
- **Customize → Workflows**, which is the saved library rather than the
  live view: it creates, edits, and runs reusable workflows.

A run streams to the panel while it executes and injects its result into the
chat on completion, so you do not have to poll it yourself.

### Route steps across ACP backends

Each `ctx.agent()` step accepts `backend=` and `model=`. `backend` selects a
selectable ACP harness (not a vendor API directly); `model` selects a model
available through that harness. This lets one workflow plan, implement and review
on different backends. Backend-routed steps start isolated sessions rather than
using the warm workflow pool.

Pass backend/model choices as workflow arguments, rather than hard-coding a model
for every account:

```python
plan = await ctx.agent(
    "Plan the change", backend=ctx.args["planner_backend"],
    model=ctx.args.get("planner_model"),
)
implementation = await ctx.agent(
    "Implement this plan: " + str(plan),
    backend=ctx.args["implementation_backend"],
    model=ctx.args.get("implementation_model"),
)
review = await ctx.agent(
    "Review the implementation", backend=ctx.args["review_backend"],
    model=ctx.args.get("review_model"),
)
```

## Parallel and pipeline execution

### `ctx.parallel(thunks)`

Run multiple agent calls concurrently and wait for all of them (barrier). Returns
results in input order; a failing call resolves to `None` rather than raising.

```python
# Two independent reviewers run at the same time
review_a, review_b = await ctx.parallel([
    lambda: ctx.agent("Review from security perspective", backend="claude-code"),
    lambda: ctx.agent("Review from performance perspective", backend="codex"),
])
```

Each thunk is a zero-arg callable returning an awaitable (`lambda: ctx.agent(...)`).
Passing an already-created coroutine (`ctx.agent(...)`) also works. Concurrency is
bounded by the run's global agent limit.

### `ctx.pipeline(items, *stages)`

Process a list of items through multiple stages without inter-stage barriers. Each
item flows through all stages in its own chain — item B can reach stage 2 while
item A is still in stage 1. Wall-clock time equals the slowest single-item chain,
not the sum of slowest-per-stage.

```python
# Process multiple files: each flows through read → analyze → report independently
results = await ctx.pipeline(
    files,
    lambda path: ctx.agent(f"Read and summarize {path}"),
    lambda summary: ctx.agent(f"Find issues in: {summary}"),
    lambda issues: ctx.agent(f"Write report for: {issues}"),
)
```

A stage that raises drops that item to `None` and skips its remaining stages.
Results are returned in input order.

## Watching and steering a run

| Ask for | What you get |
|---|---|
| The live status | Whether the run is running, finished, failed, or cancelled, plus how many agents and events it has produced |
| The full result | Every phase, each agent's outcome, the logs, and the final return value |
| Recent runs | The newest runs first, with their status |
| Cancel | The run stops |

A run that ended without a usable return value still reports the agent payloads
that did complete, plus a per-agent failure reason for each one that did not — so
a partial failure is diagnosable rather than a bare error.

## Restarting part of a run

This is the reason to prefer a workflow over a one-shot fan-out. A restart
replays the unchanged prefix from cache and re-executes only from the step you
name: agent calls before that point reuse the prior run's results without
spending a model call, and calls from that point on run fresh. Restarting from
the very beginning re-runs everything.

Each restart produces a new run id, so the original run's record is preserved
rather than overwritten.

## Saving one for reuse

A workflow saved to the library can be run again by name instead of re-authored
from intent. Ask what is already saved before describing a new workflow — an
existing one that fits skips the authoring step entirely.

## What comes back

Credentials and exfiltration-shaped URLs are stripped from every workflow
response before it reaches you, including from mapping *keys* and not only
values — agent output is parsed into these structures, so a credential can arrive
as a key.

## Private member memory

Start a workflow from the member's chat to keep its workers in that member's
memory. Saved workflows and restarted steps keep the same memory boundary.
Changing a worker's role does not switch its memory. Other members cannot read
or control that run through their tools.

If the member's memory is missing, damaged or archived, the run stops rather
than switching to Global memory. Restore the member's memory before retrying.
Private execution needs the supported Linux/WSL namespace sandbox or macOS outer
Seatbelt sandbox; turning the sandbox off is not a recovery path.

## Related docs

- [Subagents](subagents.md): parallel fan-out when the work needs no phases or restarts
- [Task runner](task-runner.md): autonomous multi-step execution from a spec file
- [Dashboard](dashboard.md): the chat side panel and the Capabilities tabs
