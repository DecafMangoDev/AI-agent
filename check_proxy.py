"""Send a small HTTP request to LM Proxy without importing or running the agent."""

import argparse
import json
import os
import sys
import time
from pathlib import Path
from urllib import error, request


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", help="Override OPENAI_MODEL")
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")

    values = dict(os.environ)
    dotenv = Path(__file__).resolve().parent / ".env"
    if dotenv.is_file():
        for raw_line in dotenv.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            values.setdefault(name.strip(), value.strip().strip("\"'"))

    key = values.get("OPENAI_API_KEY", "").strip()
    if not key:
        print("OPENAI_API_KEY is missing in .env or environment", file=sys.stderr)
        return 1
    base = values.get("OPENAI_BASE_URL", "https://llm-proxy.spirit.culab.ru").strip()
    model = args.model or values.get("OPENAI_MODEL", "openai/gpt-6-astra").strip()
    url = base.rstrip("/") + "/chat/completions"
    payload = {"model": model, "messages": [{"role": "user", "content": "Ответь ровно OK."}]}
    req = request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
        method="POST",
    )
    print("POST", url, flush=True)
    print("Model:", model, flush=True)
    started = time.monotonic()
    try:
        with request.urlopen(req, timeout=args.timeout) as response:
            body = response.read(16384).decode("utf-8", errors="replace")
            print("HTTP", response.status)
    except error.HTTPError as exc:
        body = exc.read(16384).decode("utf-8", errors="replace")
        print("HTTP", exc.code, file=sys.stderr)
        print(body.replace(key, "[REDACTED]"), file=sys.stderr)
        return 1
    except (error.URLError, TimeoutError) as exc:
        print("Connection failed:", str(exc).replace(key, "[REDACTED]"), file=sys.stderr)
        return 1
    finally:
        print("Elapsed: {:.1f}s".format(time.monotonic() - started), flush=True)
    print(body.replace(key, "[REDACTED]"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
