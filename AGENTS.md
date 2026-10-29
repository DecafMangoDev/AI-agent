# Repository instructions

This repository is a teaching project: a general-purpose OpenAI-compatible coding agent with SQLite persistence, Plan Mode, budgets, recovery, task review, change journaling, diff, conflict detection, and rollback.

## Engineering rules

- Use Python 3.9+ and UTF-8 for source, JSON, user messages, and text files.
- Keep model decisions separate from tool execution. The model never accesses the filesystem, shell, or SQLite directly.
- Store run history, messages, tool calls, attempts, results, budgets, plans, tasks, snapshots, and changes in SQLite.
- Keep CLI formatting in `agent/__main__.py`; core runtime must return structured results and must not print.
- Use small typed functions, domain errors, and narrow exception handling. Avoid broad `except Exception`.
- Do not use an agent framework. The model adapter may use the official provider SDK, but this project currently uses a dependency-free HTTP adapter.

## Workspace and tools

The control project lives in `workspace/`. Do not modify its tests to make an agent test pass. Paths resolve relative to the workspace and must not escape it, be absolute, or follow symlinks outside it.

Every tool has a name, description, JSON Schema, validation, and a registered executor. The minimum tool set is `list_files`, `read_file`, `write_file`, `delete_file`, and restricted `bash`.

Bash runs inside the workspace with a timeout and bounded output. Reject redirects, pipes, command substitution, source-changing commands, and unsafe path arguments. Any allowlist expansion must be explicit and tested.

Diagnostic allowlist: `python`/`python3` accept exactly `--version` or `-V`;
`ls` also accepts `-a`, `-l`, `-la`, `-al`, `-h`, `-lah`, and `-alh`.
Keep these forms covered by `tests/test_bash_allowlist.py`, including rejection
before process creation of compound commands, extra version arguments and escaping paths.

## State and recovery

Each run has a stable `run_id`. Before an external side effect, atomically reserve its budget and record the call as `started`. Save the result and final status afterward. If the process stops between execution and result persistence, the call is `unknown`; `/resume` must ask the user to retry, fail, or abort rather than replaying it silently.

Successful tool call IDs are idempotent. A changed tool name or argument set is a protocol error. A retry is a new attempt of the same call with the same arguments.

## Plans and tasks

Plan Mode allows read-only tools until approval. After approval, steps become dependent tasks. A task is only complete after a successful tool result is shown to the user and accepted. Rejection records the user's comment and returns the task to the model. Completed tasks must not run again after resume.

## Budgets

Budgets apply to the whole `run_id`, including planning, execution, resume, and retry:

- model requests;
- tool calls;
- potentially changing effects;
- total tool-result bytes.

For a multi-call response, reserve the complete tool/effect budget before starting any call. If the batch cannot fit, run none of it. Persist all counters in SQLite.

## Change journal and rollback

Before every potentially changing action, compare the current workspace with the latest snapshot. Afterward, persist original and final states, hashes, and a diff for every changed path. Rollback operates only on paths changed by the current `run_id`: verify every final hash first, then restore all paths. If any path has an external conflict, write nothing and report the reason.

## Verification and workflow

Run the relevant tests first, then the full suite:

```bash
python3 -m unittest discover -v
python3 -m ruff check agent tests
```

Do not commit `.env`, SQLite databases, caches, virtual environments, or generated logs. Before committing, inspect `git status`, preserve unrelated user changes, and keep commits focused.

## Reliability implementation notes

- Budget reservations must use `Store.transaction()` (`BEGIN IMMEDIATE`), never rely on a connection context manager in autocommit mode.
- Finish the same attempt that was recorded as started. Create another attempt only for an explicit retry; inspect latest attempts during recovery.
- A run is bound to its canonical workspace. Reject mismatches before executing or rolling back.
- Preserve exact file bytes in snapshots. A decoded text diff is a presentation, not restoration data. Refuse legacy binary snapshots that contain only hashes.
- Check result capacity before execution; a zero result budget must never permit a side effect.
- Treat rollback as a recorded terminal operation. Reject unresolved attempts and type conflicts before the first restore.
- Add regression tests in `tests/test_reliability.py`; keep fixtures in temporary directories and service state outside those workspaces.
- `REVIEW.md` is a historical audit. Track remaining limitations honestly in README; do not imply that all findings have been resolved merely because the existing tests pass.

## Phase and execution regressions

- Construct model system instructions from the current phase on every request; never carry planner-only restrictions into execution.
- Do not execute tool-shaped JSON/XML from assistant text. Return a corrective message and bound retries.
- Validate Python syntax before writes; never globally replace literal backslash escapes in source.
- Require successful test execution after Python changes. A filesystem write is not verification.
- Follow-up work must have new tasks; accepted tasks remain done. Headless operation must wait for explicit review.
- Recovery must reconstruct missing messages and require explicit attribution of observed changes. Bash is not automatically retryable.

Prefer `run_tests` for test requests. It invokes `python3 -m pytest` or `python3 -m unittest` directly; distinguish runner execution success from a nonzero test exit code. Keep test paths workspace-relative and validate them before execution.
