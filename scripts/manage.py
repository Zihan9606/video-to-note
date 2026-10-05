#!/usr/bin/env python3
"""跨平台统一操作入口——Windows 和 macOS 用同一条命令。

    python scripts/bootstrap.py     # 首次：建虚拟环境、装依赖
    python scripts/manage.py start  # 启动
    python scripts/manage.py test   # 跑测试
    python scripts/manage.py build  # 打包

为什么需要它：仓库里启停脚本是平台各一份（start.ps1 / start.sh），
调用方（尤其是 agent）不该也不用记住两套命令。本脚本只做**分发与选项映射**，
真正的逻辑仍然只在对应平台的脚本里，不产生第二份实现。

子命令：
    start [--port N] [--foreground] [--restart] [--no-browser] [--bind-host H]
    stop  [--all] [--quiet]
    restart [start 的选项]
    test [pytest 的额外参数]
    build
    status
    bootstrap
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
IS_WINDOWS = os.name == "nt"
if IS_WINDOWS:
    VENV_PYTHON = REPO_ROOT / ".venv" / "Scripts" / "python.exe"
    START_SCRIPT = REPO_ROOT / "start.ps1"
    STOP_SCRIPT = REPO_ROOT / "stop.ps1"
    BUILD_SCRIPT = REPO_ROOT / "scripts" / "build_exe.ps1"
else:
    VENV_PYTHON = REPO_ROOT / ".venv" / "bin" / "python"
    START_SCRIPT = REPO_ROOT / "start.sh"
    STOP_SCRIPT = REPO_ROOT / "stop.sh"
    BUILD_SCRIPT = REPO_ROOT / "scripts" / "build_app.sh"

STATE_FILE = REPO_ROOT / ".runtime" / "server.json"

USAGE = __doc__ or ""

# 子命令行的长选项 -> 平台脚本的选项
# Windows 用 PowerShell 的 -Foreground 风格，POSIX 脚本用 --foreground 风格
FLAG_MAP = {
    "foreground": ("-Foreground", "--foreground"),
    "restart": ("-Restart", "--restart"),
    "no-browser": ("-NoBrowser", "--no-browser"),
    "all": ("-All", "--all"),
    "quiet": ("-Quiet", "--quiet"),
}
VALUE_MAP = {
    "port": ("-Port", "--port"),
    "bind-host": ("-BindHost", "--bind-host"),
}


def _platform_script(script: Path) -> list[str]:
    if IS_WINDOWS:
        return [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
        ]
    return ["bash", str(script)]


def _map_args(args: list[str]) -> list[str]:
    """把 manage.py 的选项翻译成平台脚本认识的写法。"""
    mapped: list[str] = []
    index = 0
    while index < len(args):
        token = args[index]
        raw = token.lstrip("-")
        if token.startswith("--") and raw in FLAG_MAP:
            mapped.append(FLAG_MAP[raw][0 if IS_WINDOWS else 1])
        elif token.startswith("--") and raw in VALUE_MAP:
            if index + 1 >= len(args):
                raise SystemExit(f"选项 {token} 需要一个值")
            mapped.append(VALUE_MAP[raw][0 if IS_WINDOWS else 1])
            mapped.append(args[index + 1])
            index += 1
        else:
            raise SystemExit(f"不认识的选项：{token}（见 python scripts/manage.py）")
        index += 1
    return mapped


def _python_for_project() -> str:
    """测试要跑在虚拟环境里；没有 venv 时退回当前解释器（CI 就是这个形态）。"""
    return str(VENV_PYTHON) if VENV_PYTHON.is_file() else sys.executable


def _require_venv(action: str) -> None:
    if VENV_PYTHON.is_file():
        return
    raise SystemExit(
        f"还没建虚拟环境，无法 {action}。先跑一次：\n"
        f"    {sys.executable} scripts/bootstrap.py"
    )


def _dispatch(script: Path, args: list[str]) -> int:
    command = _platform_script(script) + _map_args(args)
    print("$ " + " ".join(command), flush=True)
    return subprocess.run(command, cwd=REPO_ROOT).returncode


def _read_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}


def cmd_status(_args: list[str]) -> int:
    state = _read_state()
    if not state:
        print("未在运行（没有 .runtime/server.json）。")
        return 3
    url = str(state.get("url") or "")
    print(f"状态文件：PID {state.get('pid')} · {url}")
    print(f"启动于 {state.get('started_at')}")
    try:
        with urllib.request.urlopen(f"{url}/api/health", timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"健康检查失败：{exc}")
        return 1
    print(f"健康：{payload.get('status')} · 版本 {payload.get('version')} · 模式 {payload.get('mode')}")
    print(f"本机密钥存储：{payload.get('llm_key_storage')}")
    return 0


def cmd_start(args: list[str]) -> int:
    _require_venv("start")
    return _dispatch(START_SCRIPT, args)


def cmd_stop(args: list[str]) -> int:
    return _dispatch(STOP_SCRIPT, args)


def cmd_restart(args: list[str]) -> int:
    _require_venv("restart")
    code = _dispatch(STOP_SCRIPT, ["--quiet"])
    if code:
        return code
    return _dispatch(START_SCRIPT, args)


def cmd_test(args: list[str]) -> int:
    command = [_python_for_project(), "-m", "pytest", *args]
    print("$ " + " ".join(command), flush=True)
    return subprocess.run(command, cwd=REPO_ROOT).returncode


def cmd_build(args: list[str]) -> int:
    if not BUILD_SCRIPT.exists():
        raise SystemExit(f"本机没有对应的构建脚本：{BUILD_SCRIPT}")
    if IS_WINDOWS:
        command = _platform_script(BUILD_SCRIPT) + _map_args(args)
    else:
        command = ["bash", str(BUILD_SCRIPT), *args]
    print("$ " + " ".join(command), flush=True)
    return subprocess.run(command, cwd=REPO_ROOT).returncode


def cmd_bootstrap(args: list[str]) -> int:
    command = [sys.executable, str(REPO_ROOT / "scripts" / "bootstrap.py"), *args]
    return subprocess.run(command, cwd=REPO_ROOT).returncode


COMMANDS = {
    "start": cmd_start,
    "stop": cmd_stop,
    "restart": cmd_restart,
    "test": cmd_test,
    "build": cmd_build,
    "status": cmd_status,
    "bootstrap": cmd_bootstrap,
}


def main(argv: list[str]) -> int:
    if not argv or argv[0] in {"-h", "--help", "help"}:
        print(USAGE)
        return 0
    name = argv[0]
    handler = COMMANDS.get(name)
    if handler is None:
        print(f"不认识的命令：{name}\n", file=sys.stderr)
        print(USAGE, file=sys.stderr)
        return 2
    return handler(argv[1:])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
