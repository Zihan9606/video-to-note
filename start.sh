#!/usr/bin/env bash
# VideoToNo 开发服务启动脚本（macOS / Linux）。
# 与 start.ps1 一一同构：同一套参数、同一份 .env 解析、同一个 .runtime/server.json 状态文件。
set -euo pipefail

FOREGROUND=0
RESTART=0
NO_BROWSER=0
BIND_HOST=""
PORT=""

while [ $# -gt 0 ]; do
    case "$1" in
        -f|--foreground) FOREGROUND=1 ;;
        -r|--restart) RESTART=1 ;;
        -n|--no-browser) NO_BROWSER=1 ;;
        --bind-host) BIND_HOST="${2:-}"; shift ;;
        --port) PORT="${2:-}"; shift ;;
        -h|--help)
            echo "Usage: ./start.sh [--foreground] [--restart] [--no-browser] [--bind-host HOST] [--port PORT]"
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            exit 2
            ;;
    esac
    shift
done

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$PROJECT_ROOT/.venv/bin/python"
RUNTIME_DIR="$PROJECT_ROOT/.runtime"
STATE_PATH="$RUNTIME_DIR/server.json"
STDOUT_PATH="$RUNTIME_DIR/server.stdout.log"
STDERR_PATH="$PROJECT_ROOT/.runtime/server.stderr.log"
ENV_PATH="$PROJECT_ROOT/.env"

if [ ! -x "$PYTHON" ]; then
    echo "Virtual environment not found. Create .venv and install backend/requirements.txt first." >&2
    echo "One-shot setup: python3 scripts/bootstrap.py" >&2
    exit 1
fi

# python.org 装的 Python 默认没配 CA 证书（macOS 通病），yt-dlp 与模型下载会
# certificate verify failed。只做本地文件判断，不联网探测。
if [ -z "${SSL_CERT_FILE:-}" ] && [ "$(uname -s)" = "Darwin" ]; then
    CAFILE="$("$PYTHON" -c 'import ssl; print(ssl.get_default_verify_paths().openssl_cafile or "")' 2>/dev/null || true)"
    if [ -n "$CAFILE" ] && [ ! -e "$CAFILE" ] && [ -f /etc/ssl/cert.pem ]; then
        export SSL_CERT_FILE=/etc/ssl/cert.pem
    fi
fi

# 读取 .env：KEY=VALUE，忽略注释与空行，去掉首尾引号（与 start.ps1 的正则一致）
env_value() {
    local key="$1"
    [ -f "$ENV_PATH" ] || return 1
    local line value
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in
            \#*|"") continue ;;
        esac
        case "$line" in
            "$key="*)
                value="${line#"$key="}"
                value="${value#"${value%%[![:space:]]*}"}"
                value="${value%"${value##*[![:space:]]}"}"
                value="${value#\"}"; value="${value%\"}"
                value="${value#\'}"; value="${value%\'}"
                printf '%s' "$value"
                return 0
                ;;
        esac
    done < "$ENV_PATH"
    return 1
}

open_page() {
    if [ "$NO_BROWSER" -eq 1 ]; then
        return 0
    fi
    open "$1" >/dev/null 2>&1 || true
}

if [ -z "$BIND_HOST" ]; then
    BIND_HOST="${HOST:-$(env_value HOST || true)}"
    BIND_HOST="${BIND_HOST:-127.0.0.1}"
fi
case "$BIND_HOST" in
    127.0.0.1|localhost|::1) ;;
    *)
        echo "VideoToNo 1.0 is local-only. BindHost must be 127.0.0.1, localhost, or ::1." >&2
        exit 1
        ;;
esac

if [ -z "$PORT" ]; then
    PORT="${PORT:-$(env_value PORT || true)}"
    PORT="${PORT:-8000}"
fi
case "$PORT" in
    ''|*[!0-9]*)
        echo "PORT must be a number, got: $PORT" >&2
        exit 1
        ;;
esac

if [ "$RESTART" -eq 1 ] && [ -f "$STATE_PATH" ]; then
    "$PROJECT_ROOT/stop.sh" --quiet
fi

pid_alive() {
    [ -n "${1:-}" ] && kill -0 "$1" 2>/dev/null
}

json_field() {
    # $1 = 文件, $2 = 键名
    "$PYTHON" - "$1" "$2" <<'PY'
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        payload = json.load(handle)
except Exception:
    raise SystemExit(1)
value = payload.get(sys.argv[2])
if value is None:
    raise SystemExit(1)
print(value)
PY
}

if [ -f "$STATE_PATH" ]; then
    EXISTING_PID="$(json_field "$STATE_PATH" pid 2>/dev/null || true)"
    EXISTING_URL="$(json_field "$STATE_PATH" url 2>/dev/null || true)"
    if pid_alive "$EXISTING_PID"; then
        echo "VideoToNo is already running (PID $EXISTING_PID): $EXISTING_URL"
        open_page "${EXISTING_URL:-http://${BIND_HOST}:$PORT}"
        exit 0
    fi
    rm -f "$STATE_PATH"
fi

port_listener_pid() {
    lsof -nP -iTCP:"$1" -sTCP:LISTEN -t 2>/dev/null | head -1 || true
}

if [ -n "$(port_listener_pid "$PORT")" ]; then
    echo "Port $PORT is already in use. Choose another port with ./start.sh --port <port>." >&2
    exit 1
fi

RELOAD_VALUE="${RELOAD:-$(env_value RELOAD || true)}"
RELOAD_VALUE="${RELOAD_VALUE:-false}"

cd "$PROJECT_ROOT"
mkdir -p "$RUNTIME_DIR"

if [ "$FOREGROUND" -eq 1 ]; then
    FOREGROUND_ARGS=(-m uvicorn backend.main:app --host "$BIND_HOST" --port "$PORT")
    if [ "$RELOAD_VALUE" = "true" ]; then
        FOREGROUND_ARGS+=(--reload)
    fi
    echo "Starting VideoToNo in foreground: http://${BIND_HOST}:$PORT"
    exec "$PYTHON" "${FOREGROUND_ARGS[@]}"
fi

UVICORN_ARGS=(-m uvicorn backend.main:app --host "$BIND_HOST" --port "$PORT")
if [ "$RELOAD_VALUE" = "true" ]; then
    UVICORN_ARGS+=(--reload)
fi

"$PYTHON" "${UVICORN_ARGS[@]}" >"$STDOUT_PATH" 2>"$STDERR_PATH" &
LAUNCHER_PID=$!

URL="http://${BIND_HOST}:$PORT"
HEALTHY=0
for _ in $(seq 1 40); do
    if ! kill -0 "$LAUNCHER_PID" 2>/dev/null; then
        break
    fi
    if curl -fsS -m 1 "$URL/api/health" >/dev/null 2>&1; then
        HEALTHY=1
        break
    fi
    sleep 0.25
done

if [ "$HEALTHY" -ne 1 ]; then
    if kill -0 "$LAUNCHER_PID" 2>/dev/null; then
        kill "$LAUNCHER_PID" 2>/dev/null || true
        wait "$LAUNCHER_PID" 2>/dev/null || true
    fi
    rm -f "$STATE_PATH"
    DETAILS="No server error log was produced."
    if [ -s "$STDERR_PATH" ]; then
        DETAILS="$(tail -8 "$STDERR_PATH")"
    fi
    printf 'VideoToNo failed to start.\n%s\n' "$DETAILS" >&2
    exit 1
fi

# uvicorn 与启动它的就是同一个进程，除非 --reload 会派生子进程；两种情况都以
# 真正监听端口的 PID 为准，stop.sh 校验的就是这个值。
LISTENER_PID="$(port_listener_pid "$PORT")"
LISTENER_PID="${LISTENER_PID:-$LAUNCHER_PID}"

"$PYTHON" - "$STATE_PATH" "$LISTENER_PID" "$LAUNCHER_PID" "$PROJECT_ROOT" "$PYTHON" "$URL" <<'PY'
import json, sys
from datetime import datetime, timezone
path, listener, launcher, root, executable, url = sys.argv[1:7]
with open(path, "w", encoding="utf-8") as handle:
    json.dump(
        {
            "pid": int(listener),
            "launcher_pid": int(launcher),
            "project_root": root,
            "executable": executable,
            "url": url,
            "started_at": datetime.now(timezone.utc).isoformat(),
        },
        handle,
        ensure_ascii=False,
        indent=2,
    )
PY

echo "VideoToNo started (PID $LISTENER_PID): $URL"
echo "Stop it with ./stop.sh (orphaned instances: ./stop.sh --all); logs are in .runtime/."
open_page "$URL"
