# Managed workflow reference

## Plan contract

All fields below are required. Unknown fields are rejected.

```json
{
  "version": 1,
  "objective": "Implement the requested behavior",
  "acceptance": ["The integrated behavior matches the specification"],
  "integration_checks": ["unit"],
  "tasks": [{
    "id": "T1",
    "tier": "luna",
    "objective": "Implement a bounded change",
    "scope": ["src/component.py", "tests/test_component.py"],
    "acceptance": ["State an observable, verifiable behavior"],
    "checks": ["unit"],
    "depends_on": []
  }]
}
```

Use `luna`, `terra`, or `sol` for tier. `astra` is reserved for review/global
validation, not automatic implementation. IDs use letters, digits, `.`, `_`, `-`;
`global` is reserved. No duplicate IDs, unknown dependencies or cycles.

Scope uses exact files or a directory prefix ending in `/`; no globs, `..`,
absolute paths or symlink escapes. `.git`, `.codex`, `.agents`, and
`orchestrator.toml` are protected. Changing the harness itself is a separate,
explicitly authorized maintenance task outside a managed run.

Checks are argv arrays in project configuration, not shell snippets supplied by
an agent. They run in the isolated worktree. Commands must not modify tracked or
non-ignored source. Configure formatters in check mode, not write mode.

## Per-task state machine

`pending -> implementing -> testing -> reviewing -> approved`

A failed check returns directly to implementation without a reviewer call.
A review requesting changes returns to the same executor. After the configured
attempt limit the next tier takes over; the next tier's reviewer must review.
A blocked implementation escalates immediately. An unavailable CLI/model,
malformed output, unauthorized edit, stale proof or blocked review stops the run.
Sol exhausting its budget requires replanning; Astra does not author and approve
its own patch.

Rejections and failed checks retain the diff; the next attempt inspects and fixes
it. The controller does not silently reset, discard, or overwrite that work.

## Optional delegation

A Terra/Sol implementation can return `status: "delegate"` BEFORE changing source,
with `children` following the task contract except `depends_on`. Each child is a
lower tier, inherits a subset of the parent's scope and uses configured checks.
Child ids are namespaced under the parent's id. Children run sequentially in the
same isolated run worktree. This version deliberately avoids parallel writers.
A parent may delegate once, then receives child proofs and implements integration.
Luna cannot delegate. Integration repair tasks cannot delegate again.

This is a hierarchy of responsibility, not a mandatory forwarding chain:
Astra's initial plan can target Luna directly. Sol can propose Luna directly.
A Terra child may propose Luna; this gives the optional Sol -> Terra -> Luna tree.
Lower-tier outputs do not automatically prove integration correctness.

## Reviews and final validation

Reviews use a fresh Codex invocation and a read-only sandbox. The input contains
the task contract, baseline, snapshot fingerprint, real check logs and child
proof references. It does not contain the implementation conversation transcript.
A review must cover every acceptance criterion exactly once and match the current
snapshot. An approval with findings or failed criteria is invalid.

The controller makes local checkpoint commits after valid reviews. Git commit
hooks are honored; if a hook changes the reviewed tree or leaves edits, approval
is rejected. Checkpoints use the explicit Codex Orchestrator identity and are
unsigned. Nothing is pushed or merged into the user's branch automatically.

After all tasks, all named task checks plus integration checks run on the assembled
code. Astra receives a consolidated contract and artifact references. If checks
fail, Astra may diagnose and return targeted findings, but may NOT approve.
Findings naming an existing task create bounded repair tasks within that scope;
findings needing new/global scope stop for replanning. Each repair follows the
same executor/reviewer route. Final checks and Astra validation repeat within the
global-round budget. No post-failure changes are treated as already approved.

## Hooks and persistence

SessionStart supplies a compact session id. Stop checks only a session enrolled
with `--session-id`; unrelated sessions are untouched. A pending run can request
one continuation; `stop_hook_active` prevents an unbounded loop. A blocked or
interrupted run may end with an explicit failure report. Managed child processes
skip these hooks because the controller owns their phase gates.

There is intentionally no SubagentStop review launcher: `codex exec` processes
are not native `spawn_agent` threads. A hook never starts a model, parses private
transcripts, or waits for a reviewer before allowing an implementer to finish.

State/artifacts live under the Git common directory in `codex-orchestrator/runs`.
JSON state is atomically replaced; an OS file lock prevents concurrent controllers.
Each run has its own branch/worktree, recorded model IDs/efforts, request/response
artifacts, JSONL CLI events, check logs and a history of review results.

`status` aggregates reported token fields by model role and stage. Cached input
is part of input, not extra input. Reasoning is reported separately as supplied by
the CLI; do not add it again to output. Missing usage is explicitly counted.
No synthetic quality score, cost estimate or promised savings is generated.
