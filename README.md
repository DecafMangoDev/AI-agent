# General Purpose Agent

This repository contains a small OpenAI-compatible coding agent. The model decides when to answer directly and when to call a registered workspace tool. Runtime state, tool attempts, budgets, tasks, snapshots, changes, and chat history are stored in SQLite.

## Setup

```bash
python3 -m pip install -r requirements-dev.txt
cp .env.example .env
```

Configure the provider in `.env`:

```text
OPENAI_API_KEY=your-key
OPENAI_BASE_URL=http://localhost:11434/v1/
OPENAI_MODEL=qwen3:4b
```

The default example targets a local Ollama-compatible endpoint. Any OpenAI-compatible provider can be used by changing the endpoint and model.

## Run

```bash
python3 -m agent --workspace workspace
```

The interactive CLI uses `prompt-toolkit` for Unicode-aware editing, including Cyrillic input and backspace. Normal conversation is answered directly; coding and workspace work uses tools selected by the model.

Available tools are `list_files`, `read_file`, `write_file`, `delete_file`, and restricted `bash`. Paths are resolved inside the workspace. Bash rejects shell features that could bypass the change journal, such as pipes, redirects, command substitution, and source-changing commands.

## Chat commands

```text
/new                              start a new chat
/chats                            list saved chats and their first request
/resume [run_id]                  show and continue a saved chat
/approve                          approve a saved plan
/reject                           reject a saved plan
/status                           show the current run
/budget                           show remaining budgets
/tasks                            show the task checklist
/diff                             show changes for the current run
/rollback                         restore the current run when no conflict exists
/resolve <call_id> retry|fail|abort resolve an unknown call manually
/exit                             leave the CLI
```

When `/resume` finds a tool call that was interrupted before its result was saved, it shows the call and asks whether to retry, fail, or abort. The manual `/resolve` command expects a `call_id`, not a `run_id`.

## Plans and task review

Start Plan Mode with:

```bash
python3 -m agent --plan --workspace workspace
```

Before approval, only read-only tools are allowed. After approval, plan steps become dependent tasks. A successful tool call produces a candidate result; the CLI shows the tool, arguments, and shortened result and asks whether the result is acceptable. Approval marks the task as `done`. Rejection records the user's comment and sends the task back to the model.

## Budgets

Limits are configured in `agent.toml` and apply to the complete `run_id`, including planning, approval, resume, and retry:

```toml
[limits]
model = 20
tools = 40
effects = 15
result = 120000
```

`model` counts model requests, `tools` counts tool calls, `effects` counts potentially changing actions, and `result` limits the total size of tool results in bytes. Multi-call batches reserve their complete tool and effect budget before any call starts. Progress events show the remaining counters.

## Change journal and rollback

Before a changing action, runtime compares the workspace with the latest snapshot. After the action it records original and final states, hashes, and a unified diff in SQLite. `/diff` shows the current run's journal.

`/rollback` checks every final hash before writing anything. If a file changed outside the run, rollback is blocked and reports the expected and current hash. If all hashes match, only files changed by the current run are restored. Unrelated files are left untouched.

## Verification

```bash
python3 -m unittest discover -v
python3 -m ruff check agent tests
```
