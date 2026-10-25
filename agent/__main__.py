"""Interactive CLI with resumable chat selection and visible progress."""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from typing import List, Optional

from .client import ModelConfig, ModelError, OpenAICompatibleClient
from .config import load_limits
from .runtime import AgentRuntime
from .storage import Store


class CliUsageError(ValueError):
    pass


class FriendlyArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CliUsageError(message)


def _configure_utf8() -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def _parser() -> FriendlyArgumentParser:
    parser = FriendlyArgumentParser(description="General purpose agent — interactive resumable chats")
    parser.add_argument("--workspace", default="workspace")
    parser.add_argument("--state", default=".agent/state.sqlite3")
    parser.add_argument("--config", default="agent.toml", help="limits file [limits]")
    parser.add_argument("--resume", nargs="?", const="latest", help="resume a chat on startup")
    parser.add_argument("--plan", action="store_true", help="enable Plan Mode for a new chat")
    parser.add_argument("--model")
    parser.add_argument("--base-url")
    return parser


def _progress(message: str) -> None:
    print("\r" + message + "\033[K", file=sys.stderr, flush=True)


def _read_prompt() -> str:
    return input().strip()


def _review_task(task_id: str, payload) -> tuple:
    print("\nTask {} completed technically.".format(task_id))
    tool = payload.get("tool", "unknown")
    arguments = json.dumps(payload.get("arguments", {}), ensure_ascii=False, sort_keys=True)
    result = json.dumps(payload.get("result", {}), ensure_ascii=False)
    print("Call: {}({})".format(tool, arguments[:1000]))
    print("Result: {}".format(result[:2000] + ("…" if len(result) > 2000 else "")))
    print("Is this result acceptable? [y/N] ", end="", flush=True)
    accepted = _read_prompt().lower() in {"y", "yes", "да", "д"}
    if accepted:
        return True, ""
    print("What should be improved? ", end="", flush=True)
    return False, _read_prompt()


def _new_runtime(client, store: Store, args, run_id: Optional[str] = None) -> AgentRuntime:
    return AgentRuntime(
        client,
        store,
        args.workspace,
        run_id=run_id,
        limits=load_limits(args.config),
        progress=_progress,
        review=_review_task,
    )


def _show_chats(store: Store) -> None:
    rows = store.recent_runs()
    if not rows:
        print("No saved chats.")
        return
    for row in rows:
        messages = store.messages(row["run_id"])
        preview = next(
            (message.get("content", "").replace("\n", " ") for message in messages
             if message.get("role") == "user" and message.get("content")),
            "no messages",
        )
        print("{}  {:12}  {}  {}".format(row["run_id"], row["status"], row["phase"], preview[:80]))


def _show_history(store: Store, run_id: str) -> None:
    """Print the persisted conversation before a chat is resumed."""
    if not store.run(run_id):
        print("Chat not found. Use /chats.")
        return
    for message in store.messages(run_id):
        role = message.get("role")
        if role == "system":
            continue
        if role == "tool":
            name = message.get("name", "tool")
            content = message.get("content", "")
            try:
                content = json.dumps(json.loads(content), ensure_ascii=False)
            except (TypeError, ValueError):
                pass
            print("[tool {}] {}".format(name, content[:500]))
            continue
        label = {"user": "you", "assistant": "model"}.get(role, role or "message")
        content = message.get("content") or ""
        if message.get("tool_calls"):
            names = ", ".join(call.get("function", {}).get("name", "?") for call in message["tool_calls"])
            content = (content + " " if content else "") + "calls: " + names
        print("[{}] {}".format(label, content))


def _resolve_unknown_calls(runtime: AgentRuntime, store: Store) -> None:
    rows = list(
        store.db.execute(
            "SELECT call_id, name FROM calls WHERE run_id=? AND status='unknown' ORDER BY rowid",
            (runtime.run_id,),
        )
    )
    for row in rows:
        print("\nUnknown call {} ({}).".format(row["call_id"], row["name"]))
        print("Choose an action: [r]etry, [f]ail, or [a]bort: ", end="", flush=True)
        action = _read_prompt().lower()
        actions = {"r": "retry", "retry": "retry", "f": "fail", "fail": "fail", "a": "abort", "abort": "abort"}
        while action not in actions:
            print("Enter retry, fail, or abort: ", end="", flush=True)
            action = _read_prompt().lower()
        print(runtime.resolve(row["call_id"], actions[action]))


def _command(prompt: str, runtime: AgentRuntime, store: Store, client, args):
    if not prompt.startswith("/"):
        return False
    try:
        parts = shlex.split(prompt)
    except ValueError as exc:
        print("Command error: {}".format(exc))
        return True
    command = parts[0].lower()
    if command in {"/help", "/?"}:
        print("/new /chats /resume [run_id] /approve /reject /status /budget /tasks /diff /rollback /resolve <id> <retry|fail|abort> /exit")
    elif command == "/new":
        if len(parts) != 1:
            print("Usage: /new")
        else:
            new_runtime = _new_runtime(client, store, args)
            print("New chat: {}".format(new_runtime.run_id))
            print("Chat history for {} is empty.".format(new_runtime.run_id))
            return ("switch", new_runtime)
    elif command == "/chats":
        _show_chats(store)
    elif command == "/resume":
        target = parts[1] if len(parts) > 1 else (runtime.run_id if store.run(runtime.run_id) else None)
        if target is None:
            rows = store.recent_runs(1)
            target = rows[0]["run_id"] if rows else None
        if target is None or store.run(target) is None:
            print("Chat not found. Use /chats.")
        else:
            new_runtime = _new_runtime(client, store, args, target)
            _show_history(store, target)
            try:
                result = new_runtime.resume()
                print(result)
                _resolve_unknown_calls(new_runtime, store)
            except RuntimeError as exc:
                print("Could not resume: {}".format(exc))
            return ("switch", new_runtime)
    elif command == "/approve":
        print(runtime.approve())
    elif command == "/reject":
        print(runtime.reject())
    elif command == "/status":
        row = store.run(runtime.run_id)
        print(dict(row) if row else "No active chat")
    elif command == "/budget":
        row = store.budget(runtime.run_id)
        print(dict(row) if row else "Budget is available after starting a chat")
    elif command == "/tasks":
        rows = store.tasks(runtime.run_id)
        marks = {"done": "x", "awaiting_review": "?", "running": ">"}
        print("\n".join("[{}] {} {}".format(marks.get(r["status"], " "), r["task_id"], r["description"]) for r in rows) or "No tasks yet")
    elif command == "/diff":
        rows = store.db.execute("SELECT path,diff FROM changes WHERE run_id=?", (runtime.run_id,))
        print("".join(row["diff"] for row in rows) or "No changes")
    elif command == "/rollback":
        runtime.rollback()
        print("Rollback completed.")
    elif command == "/resolve":
        if len(parts) != 3 or parts[2] not in {"retry", "fail", "abort"}:
            print("Usage: /resolve <call_id> <retry|fail|abort>")
        else:
            print(runtime.resolve(parts[1], parts[2]))
    elif command == "/exit":
        return "exit"
    else:
        print("Unknown command. Use /help")
    return True


def _chat(args) -> int:
    client = OpenAICompatibleClient(ModelConfig.from_environment(model_override=args.model, base_url_override=args.base_url))
    store = Store(args.state)
    run_id = None
    if args.resume:
        rows = store.recent_runs(1) if args.resume == "latest" else []
        run_id = rows[0]["run_id"] if rows else args.resume if store.run(args.resume) else None
        if run_id is None:
            print("Chat for resume not found. Start a new request.", file=sys.stderr)
    runtime = _new_runtime(client, store, args, run_id)
    print("General purpose agent. /chats — list chats, /resume [run_id] — switch chats.")
    if run_id:
        _show_history(store, run_id)
        try:
            result = runtime.resume()
            print(result)
            _resolve_unknown_calls(runtime, store)
        except RuntimeError as exc:
            print("Recovery error: {}".format(exc))
    while True:
        try:
            prompt = input("agent> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not prompt:
            continue
        result = _command(prompt, runtime, store, client, args)
        if result == "exit":
            break
        if isinstance(result, tuple) and result[0] == "switch":
            runtime = result[1]
            continue
        if result:
            continue
        try:
            if store.run(runtime.run_id) is None:
                if args.plan:
                    print(runtime.start(prompt, plan=True))
                    answer = input("Approve plan? [y/N]: ").strip().lower()
                    print(runtime.approve() if answer in {"y", "yes", "да", "д"} else runtime.reject())
                else:
                    print(runtime.start(prompt))
            else:
                print(runtime.continue_prompt(prompt))
        except KeyboardInterrupt:
            print("\nLoop interrupted. State was saved; use /resume to continue.")
        except (ModelError, RuntimeError, OSError) as exc:
            print("Error: {}".format(exc))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    _configure_utf8()
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if raw_argv and raw_argv[0] == "chat":
        raw_argv.pop(0)
    try:
        return _chat(_parser().parse_args(raw_argv))
    except CliUsageError as exc:
        print("Argument error: {}".format(exc), file=sys.stderr)
        return 2
    except (ModelError, RuntimeError, OSError, ValueError) as exc:
        print("Error: {}".format(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
