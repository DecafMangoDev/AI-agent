# Coding agent

Консольный coding agent с OpenAI-compatible tool calling, SQLite-восстановлением, Plan Mode, единым бюджетом, задачами с зависимостями и журналом изменений с rollback.

## Запуск

```bash
python3 -m agent --workspace workspace
```

Настройте `.env` по примеру `.env.example`. Лимиты лежат в `agent.toml` и действуют на весь `run_id`, включая plan, approve, resume и retry.

```toml
[limits]
model = 20
tools = 40
effects = 15
result = 120000
```

Доступные инструменты: `list_files`, `read_file`, `write_file`, `delete_file`, `bash`. Bash ограничен командами поиска, тестирования и `git status/diff`; пути не могут выйти из workspace.

## Чаты и восстановление

После каждого события CLI печатает прогресс в stderr. SQLite по умолчанию находится в `.agent/state.sqlite3`. Внутри сеанса:

```text
/chats                         список сохранённых run_id
/resume [run_id]               продолжить или переключить чат
/approve /reject               подтвердить или отклонить сохранённый план
/status  /budget  /tasks       состояние, расход и чеклист задач
/diff                          diff текущего run_id
/resolve <id> retry|fail|abort разрешить неизвестный вызов после сбоя
/rollback                      откатить изменения текущего run_id
/exit
```

Можно выбрать чат до запуска: `python3 -m agent --resume <run_id>`. Без идентификатора `--resume` продолжает самый свежий чат.

## Plan Mode и задачи

```bash
python3 -m agent --plan --workspace workspace
```

До подтверждения плана разрешены только чтение и просмотр. После approve шаги становятся задачами с зависимостями; задача получает статус `done` только после сохранённого evidence от успешного инструмента. Полное проектное обоснование находится в [DESIGN.md](DESIGN.md).

## Проверки

```bash
python3 -m unittest discover -v
python3 -m ruff check agent tests
```
