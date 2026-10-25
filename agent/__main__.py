"""Interactive CLI with resumable chat selection and visible progress."""

from __future__ import annotations

import argparse
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
    parser = FriendlyArgumentParser(description="Coding agent — интерактивные возобновляемые чаты")
    parser.add_argument("--workspace", default="workspace")
    parser.add_argument("--state", default=".agent/state.sqlite3")
    parser.add_argument("--config", default="agent.toml", help="файл лимитов [limits]")
    parser.add_argument("--resume", nargs="?", const="latest", help="продолжить чат при запуске")
    parser.add_argument("--plan", action="store_true", help="включить Plan Mode для нового чата")
    parser.add_argument("--model")
    parser.add_argument("--base-url")
    return parser


def _progress(message: str) -> None:
    print("\r" + message + "\033[K", file=sys.stderr, flush=True)


def _new_runtime(client, store: Store, args, run_id: Optional[str] = None) -> AgentRuntime:
    return AgentRuntime(client, store, args.workspace, run_id=run_id, limits=load_limits(args.config), progress=_progress)


def _show_chats(store: Store) -> None:
    rows = store.recent_runs()
    if not rows:
        print("Сохранённых чатов нет.")
        return
    for row in rows:
        print("{}  {:12}  {}".format(row["run_id"], row["status"], row["phase"]))


def _command(prompt: str, runtime: AgentRuntime, store: Store, client, args):
    if not prompt.startswith("/"):
        return False
    try:
        parts = shlex.split(prompt)
    except ValueError as exc:
        print("Ошибка команды: {}".format(exc))
        return True
    command = parts[0].lower()
    if command in {"/help", "/?"}:
        print("/chats /resume [run_id] /approve /reject /status /budget /tasks /diff /rollback /resolve <id> <retry|fail|abort> /exit")
    elif command == "/chats":
        _show_chats(store)
    elif command == "/resume":
        target = parts[1] if len(parts) > 1 else (runtime.run_id if store.run(runtime.run_id) else None)
        if target is None:
            rows = store.recent_runs(1)
            target = rows[0]["run_id"] if rows else None
        if target is None or store.run(target) is None:
            print("Чат не найден. Используйте /chats.")
        else:
            new_runtime = _new_runtime(client, store, args, target)
            print("Текущий чат: {}".format(target))
            try:
                print(new_runtime.resume())
            except RuntimeError as exc:
                print("Не удалось продолжить: {}".format(exc))
            return ("switch", new_runtime)
    elif command == "/approve":
        print(runtime.approve())
    elif command == "/reject":
        print(runtime.reject())
    elif command == "/status":
        row = store.run(runtime.run_id)
        print(dict(row) if row else "Активного чата нет")
    elif command == "/budget":
        row = store.budget(runtime.run_id)
        print(dict(row) if row else "Бюджет появится после старта")
    elif command == "/tasks":
        rows = store.tasks(runtime.run_id)
        print("\n".join("[{}] {} {}".format("x" if r["status"] == "done" else " ", r["task_id"], r["description"]) for r in rows) or "Задач пока нет")
    elif command == "/diff":
        rows = store.db.execute("SELECT path,diff FROM changes WHERE run_id=?", (runtime.run_id,))
        print("".join(row["diff"] for row in rows) or "Изменений нет")
    elif command == "/rollback":
        runtime.rollback()
        print("Откат выполнен.")
    elif command == "/resolve":
        if len(parts) != 3 or parts[2] not in {"retry", "fail", "abort"}:
            print("Использование: /resolve <id> <retry|fail|abort>")
        else:
            print(runtime.resolve(parts[1], parts[2]))
    elif command == "/exit":
        return "exit"
    else:
        print("Неизвестная команда. Используйте /help")
    return True


def _chat(args) -> int:
    client = OpenAICompatibleClient(ModelConfig.from_environment(model_override=args.model, base_url_override=args.base_url))
    store = Store(args.state)
    run_id = None
    if args.resume:
        rows = store.recent_runs(1) if args.resume == "latest" else []
        run_id = rows[0]["run_id"] if rows else args.resume if store.run(args.resume) else None
        if run_id is None:
            print("Чат для resume не найден. Начните новый запрос.", file=sys.stderr)
    runtime = _new_runtime(client, store, args, run_id)
    print("Coding agent. /chats — список чатов, /resume [run_id] — переключение.")
    if run_id:
        print("Продолжаю чат {}".format(run_id))
        try:
            print(runtime.resume())
        except RuntimeError as exc:
            print("Восстановление: {}".format(exc))
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
                    answer = input("Утвердить план? [y/N]: ").strip().lower()
                    print(runtime.approve() if answer in {"y", "yes", "да", "д"} else runtime.reject())
                else:
                    print(runtime.start(prompt))
            else:
                print(runtime.continue_prompt(prompt))
        except (ModelError, RuntimeError, OSError) as exc:
            print("Ошибка: {}".format(exc))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    _configure_utf8()
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if raw_argv and raw_argv[0] == "chat":
        raw_argv.pop(0)
    try:
        return _chat(_parser().parse_args(raw_argv))
    except CliUsageError as exc:
        print("Ошибка параметров: {}".format(exc), file=sys.stderr)
        return 2
    except (ModelError, RuntimeError, OSError, ValueError) as exc:
        print("Ошибка: {}".format(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
