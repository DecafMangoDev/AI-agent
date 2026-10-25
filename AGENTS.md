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
