#!/usr/bin/env bash
# VideoToNo 开发服务停止脚本（macOS / Linux）。
# 与 stop.ps1 一一同构：同一套 --quiet/--all、同一个 .runtime/server.json 状态文件，
# 以及同一条底线——校验不出这个 PID 属于本项目的这一份，就不动它。
set -euo pipefail

QUIET=0
ALL=0

while [ $# -gt 0 ]; do
    case "$1" in
        -q|--quiet) QUIET=1 ;;
        -a|--all) ALL=1 ;;
        -h|--help)
            echo "Usage: ./stop.sh [--quiet] [--all]"
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
STATE_PATH="$PROJECT_ROOT/.runtime/server.json"
EXPECTED_PYTHON="$PROJECT_ROOT/.venv/bin/python"

say() {
    if [ "$QUIET" -eq 0 ]; then
        echo "$1"
    fi
}

command_of() {
    ps -p "$1" -o command= 2>/dev/null || true
}

pid_alive() {
    [ -n "${1:-}" ] && kill -0 "$1" 2>/dev/null
}

json_field() {
    "$PROJECT_ROOT/.venv/bin/python" - "$1" "$2" <<'PY'
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

if [ "$ALL" -eq 1 ]; then
    # 默认路径只停状态文件记的那一个实例；状态文件被覆盖或删掉后留下的孤儿
    # 就再没有入口了——开发实例没有托盘图标，除了脚本无处可关。
    # 判归属不能只看命令行：venv 启动的进程在 macOS 上显示的是框架 Python 路径，
    # 项目路径根本不在里面，所以以"命令行是这个服务 + 工作目录就是本项目"为准。
    cwd_of() {
        local raw
        raw="$(lsof -a -p "$1" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -1)"
        # lsof 会把非 ASCII 字节写成 \xe4 这种形式，中文路径不还原就永远比不相等
        [ -n "$raw" ] && printf '%b' "$raw"
        return 0
    }
    STOPPED=0
    for pid in $(pgrep -f 'backend\.main:app' 2>/dev/null || true); do
        command="$(command_of "$pid")"
        case "$command" in
            *backend.main:app*) ;;
            *) continue ;;
        esac
        if [ "$(cwd_of "$pid")" != "$PROJECT_ROOT" ]; then
            continue
        fi
        say "Stopped VideoToNo dev server (PID $pid)."
        kill "$pid" 2>/dev/null || true
        STOPPED=$((STOPPED + 1))
    done
    if [ "$STOPPED" -eq 0 ]; then
        say "No VideoToNo dev server process found for this project."
    fi
    rm -f "$STATE_PATH"

    # 打包版不在此列：它可能正跑着任务，且有托盘图标可以退，脚本不越俎代庖
    for pid in $(pgrep -x VideoToNo 2>/dev/null || true); do
        say "Packaged instance still running (PID $pid) - not touched; quit it from its tray icon."
    done
    exit 0
fi

if [ ! -f "$STATE_PATH" ]; then
    say "VideoToNo is not running (no PID state file)."
    exit 0
fi

RECORDED_PID="$(json_field "$STATE_PATH" pid 2>/dev/null || true)"
if [ -z "$RECORDED_PID" ]; then
    echo "The runtime state file is malformed: $STATE_PATH" >&2
    exit 1
fi
LAUNCHER_PID="$(json_field "$STATE_PATH" launcher_pid 2>/dev/null || echo "$RECORDED_PID")"
STATE_ROOT="$(json_field "$STATE_PATH" project_root 2>/dev/null || true)"
RECORDED_EXECUTABLE="$(json_field "$STATE_PATH" executable 2>/dev/null || true)"

if [ "$STATE_ROOT" != "$PROJECT_ROOT" ]; then
    echo "The runtime state belongs to another project directory; refusing to stop its processes." >&2
    exit 1
fi

# 只接受本项目虚拟环境里的解释器：状态文件被别的工具改写时，不拿它当杀人依据。
if [ -n "$RECORDED_EXECUTABLE" ] && [ "$RECORDED_EXECUTABLE" != "$EXPECTED_PYTHON" ]; then
    echo "Launcher PID $LAUNCHER_PID does not belong to this project's virtual environment; refusing to stop it." >&2
    exit 1
fi

# macOS 上 `ps` 显示的是符号链接解析后的解释器（.venv/bin/python -> 框架 Python），
# 所以路径只在状态文件里校验（上面两段），进程这一侧改成和 stop.ps1 一致的三条判据：
# 命令行是这个服务 + (是启动它的那个进程 / 是它的子进程 / 命令行里带着本项目路径)。
verify_process() {
    local pid="$1" command parent
    command="$(command_of "$pid")"
    case "$command" in
        *backend.main:app*|*"$PROJECT_ROOT"*|*"$EXPECTED_PYTHON"*) ;;
        *) return 1 ;;
    esac
    if [ "$pid" = "$LAUNCHER_PID" ]; then
        return 0
    fi
    case "$command" in
        *backend.main:app*) ;;
        *) return 1 ;;
    esac
    case "$command" in
        *"$PROJECT_ROOT"*|*"$EXPECTED_PYTHON"*) return 0 ;;
    esac
    parent="$(ps -p "$pid" -o ppid= 2>/dev/null | tr -d '[:space:]' || true)"
    [ "$parent" = "$LAUNCHER_PID" ]
}

if pid_alive "$LAUNCHER_PID" && ! verify_process "$LAUNCHER_PID"; then
    echo "Launcher PID $LAUNCHER_PID does not belong to this project's virtual environment; refusing to stop it." >&2
    exit 1
fi

if pid_alive "$RECORDED_PID" && ! verify_process "$RECORDED_PID"; then
    echo "Listener PID $RECORDED_PID cannot be verified as this VideoToNo instance; refusing to stop it." >&2
    exit 1
fi

if ! pid_alive "$RECORDED_PID" && ! pid_alive "$LAUNCHER_PID"; then
    rm -f "$STATE_PATH"
    say "Removed stale VideoToNo PID state."
    exit 0
fi

if pid_alive "$RECORDED_PID"; then
    kill "$RECORDED_PID" 2>/dev/null || true
fi
if pid_alive "$LAUNCHER_PID" && [ "$LAUNCHER_PID" != "$RECORDED_PID" ]; then
    kill "$LAUNCHER_PID" 2>/dev/null || true
fi
rm -f "$STATE_PATH"
say "VideoToNo stopped (PID $RECORDED_PID)."
