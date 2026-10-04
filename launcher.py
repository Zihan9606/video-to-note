"""VideoToNo 启动器。

开发模式（`python launcher.py`）：
  - 控制台窗口显示地址，Ctrl+C 退出

打包模式（PyInstaller onefile，--noconsole）：
  - 无控制台窗口，服务启动后驻留系统托盘
  - 托盘菜单：打开界面 / 查看日志 / 退出
  - 日志写入工作目录的 _app.log
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any

VERSION = "1.4.2"
DEFAULT_PORT = 8000
PORT_SCAN_RANGE = 20
START_TIMEOUT_SECONDS = 60
APP_NAME = f"VideoToNo v{VERSION}"
GITHUB_LATEST_RELEASE_API = "https://api.github.com/repos/like-attract/video-to-note/releases/latest"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def base_dir() -> Path:
    if is_frozen():
        executable_dir = Path(sys.executable).resolve().parent
        # macOS 的 .app 里可执行文件在 Contents/MacOS 下，便携工作目录要落在 .app 旁边
        # （对应 Windows 的 exe 旁边），不能塞进 bundle 内部——升级换包时会被整个删掉。
        parts = executable_dir.parts
        if sys.platform == "darwin" and len(parts) >= 4 and parts[-1] == "MacOS" and parts[-2] == "Contents":
            return executable_dir.parents[2]
        return executable_dir
    return Path(__file__).resolve().parent


def configure_runtime_dirs() -> None:
    """设置工作目录：打包模式默认放在应用旁边（便携），允许环境变量覆盖。"""
    if is_frozen():
        exe_dir = base_dir()
        default_workspace = exe_dir / "workspace"
        try:
            default_workspace.mkdir(parents=True, exist_ok=True)
            probe = default_workspace / ".write_test"
            probe.write_text("", encoding="utf-8")
            probe.unlink()
        except OSError:
            # 应用所在目录不可写时（如 /Applications、Program Files）退回用户目录
            local_app_data = os.environ.get("LOCALAPPDATA")
            if local_app_data:
                fallback_root = Path(local_app_data)
            elif sys.platform == "darwin":
                # 对应 Windows 的 %LOCALAPPDATA%（AppData\Local）的 macOS 惯例位置
                fallback_root = Path.home() / "Library" / "Application Support"
            else:
                fallback_root = Path.home() / ".videotono"
            default_workspace = fallback_root / "VideoToNo" / "workspace"
            default_workspace.mkdir(parents=True, exist_ok=True)
    else:
        default_workspace = base_dir() / "workspace"
        default_workspace.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("VIDEOTONOTES_WORKSPACE", str(default_workspace))
    # 环境变量覆盖的路径也要确保存在：打包版写日志文件前依赖此目录，缺失会启动崩溃
    Path(os.environ["VIDEOTONOTES_WORKSPACE"]).mkdir(parents=True, exist_ok=True)


RUN_MODE = "portable" if is_frozen() else "dev"


def server_alive(
    url: str,
    mode: str | None = None,
    version: str | None = None,
) -> bool:
    """健康检查；可同时隔离运行模式和应用版本。

    新旧便携版不能只按 ``portable`` 复用，否则旧版服务会继续提供旧前端，
    造成页面脚本、HTML 和后端版本混用。
    """
    try:
        with urllib.request.urlopen(f"{url}/api/health", timeout=1.5) as response:
            if response.status != 200:
                return False
            payload = json.loads(response.read().decode("utf-8"))
            if not (payload.get("status") == "ok" and payload.get("service") == "VideoToNo"):
                return False
            if mode is not None and payload.get("mode") != mode:
                return False
            return version is None or payload.get("version") == version
    except Exception:
        return False


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def find_available_port() -> int:
    for port in range(DEFAULT_PORT, DEFAULT_PORT + PORT_SCAN_RANGE):
        url = f"http://127.0.0.1:{port}"
        if server_alive(url, mode=RUN_MODE, version=VERSION):
            return port  # 同模式实例已在运行，直接复用
        if port_free(port):
            return port
    raise RuntimeError(f"端口 {DEFAULT_PORT}-{DEFAULT_PORT + PORT_SCAN_RANGE - 1} 均不可用，请稍后重试")


# --------------------------------------------------------------------------
# 服务启动（两种模式共用）
# --------------------------------------------------------------------------

def start_server(port: int) -> tuple[Any, threading.Thread]:
    """在线程中启动 uvicorn，返回 (server, thread)。"""
    import uvicorn

    # 让挂载的 MCP 端点（/mcp/sse）自调命中实际端口
    os.environ.setdefault("VIDEOTONOTES_BACKEND_URL", f"http://127.0.0.1:{port}")
    from backend.main import app

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="info",
        access_log=False,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    return server, thread


def wait_healthy(
    url: str,
    timeout: float = START_TIMEOUT_SECONDS,
    mode: str | None = None,
    version: str | None = None,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if server_alive(url, mode=mode, version=version):
            return True
        time.sleep(0.2)
    return False


# --------------------------------------------------------------------------
# 开发模式（控制台）
# --------------------------------------------------------------------------

def print_banner(url: str, workspace: Path) -> None:
    width = 58
    print("=" * width)
    print("  VideoToNo v" + VERSION + "（开发模式）")
    print(f"  界面地址: {url}")
    print(f"  工作目录: {workspace}")
    print("  关闭本窗口或按 Ctrl+C 退出。")
    print("=" * width, flush=True)


def run_console(url: str, workspace: Path, port: int) -> int:
    print_banner(url, workspace)
    print("  正在启动服务，请稍候…", flush=True)
    try:
        server, _ = start_server(port)
        if not wait_healthy(url, mode=RUN_MODE, version=VERSION):
            print(f"警告: {START_TIMEOUT_SECONDS} 秒内未就绪，请手动打开 {url}", flush=True)
        else:
            webbrowser.open(url)
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n已退出。再见！")
        server.should_exit = True
    except Exception as error:
        print(f"\n启动失败：{error}")
        input("按回车键退出…")
        return 1
    return 0


# --------------------------------------------------------------------------
# 打包模式（系统托盘）
# --------------------------------------------------------------------------

def setup_file_logging(workspace: Path) -> Path:
    """noconsole 模式下把 stdout/stderr 重定向到日志文件。"""
    log_path = workspace / "_app.log"
    log_file = open(log_path, "a", encoding="utf-8", buffering=1)
    sys.stdout = log_file
    sys.stderr = log_file
    return log_path


def _applescript_string(value: str) -> str:
    """转义成 AppleScript 字符串字面量；换行保留原样（AppleScript 允许字符串跨行）。"""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _run_applescript(script: str) -> str:
    """执行一段 AppleScript 并返回 stdout；命令失败或工具缺失返回空串。"""
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=300,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def show_error(message: str) -> None:
    """启动失败提示：Windows 弹消息框、macOS 弹系统警告，其余平台写 stderr。"""
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, f"{APP_NAME} 启动失败", 0x10)
        return
    if sys.platform == "darwin":
        # 打包版没有控制台，写 stderr 等于没人看得见
        _run_applescript(
            f'display alert "{_applescript_string(APP_NAME)} 启动失败" '
            f'message "{_applescript_string(message)}" as critical'
        )
        return
    print(message, file=sys.stderr, flush=True)


def open_in_shell(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def version_tuple(value: str) -> tuple[int, ...]:
    """把 v1.2.3 或 1.2.3 转成可比较的版本元组。"""
    text = str(value or "").strip().lstrip("vV")
    parts: list[int] = []
    for part in text.split("."):
        digits = ""
        for character in part:
            if not character.isdigit():
                break
            digits += character
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts or [0])


def latest_release_info() -> dict[str, str]:
    """读取 GitHub 最新 Release；只在用户点击托盘菜单时调用。"""
    request = urllib.request.Request(
        GITHUB_LATEST_RELEASE_API,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"VideoToNo/{VERSION}",
        },
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        payload = json.loads(response.read().decode("utf-8"))
    tag = str(payload.get("tag_name") or "").strip()
    url = str(payload.get("html_url") or "https://github.com/like-attract/video-to-note/releases/latest")
    if not tag:
        raise RuntimeError("GitHub 未返回有效版本号")
    return {"tag_name": tag, "html_url": url}


def show_update_message(message: str, title: str = APP_NAME, flags: int = 0x40) -> int:
    """显示更新提示；Windows 用消息框，macOS 用系统对话框，其余平台退回日志便于测试。

    返回值沿用 Windows 的 IDYES(6) / IDNO(7)，调用方 `result == 6` 这条判断因此在
    两个平台上语义一致。
    """
    if sys.platform == "win32":
        import ctypes

        return int(ctypes.windll.user32.MessageBoxW(None, message, title, flags))
    if sys.platform == "darwin":
        if flags & 0x4:  # MB_YESNO：问"要不要打开 Release 页面"
            returned = _run_applescript(
                f'display dialog "{_applescript_string(message)}" '
                f'with title "{_applescript_string(title)}" '
                f'buttons {{"否", "是"}} default button "是"'
            )
            return 6 if "button returned:是" in returned else 7
        _run_applescript(
            f'display dialog "{_applescript_string(message)}" '
            f'with title "{_applescript_string(title)}" '
            f'buttons {{"好"}} default button "好"'
        )
        return 0
    print(f"{title}: {message}", file=sys.stderr, flush=True)
    return 0


def check_for_updates(_icon: Any = None, _item: Any = None) -> None:
    """手动检查 GitHub Release，不阻塞启动，也不在启动时联网。"""
    try:
        release = latest_release_info()
        if version_tuple(release["tag_name"]) <= version_tuple(VERSION):
            show_update_message(f"当前已是最新版本（v{VERSION}）。", flags=0x40)
            return
        message = (
            f"检测到新版本 {release['tag_name']}（当前 v{VERSION}）。\n\n"
            "是否打开 GitHub Release 页面查看并更新？"
        )
        result = show_update_message(message, title=f"{APP_NAME} 更新", flags=0x24)
        if result == 6:  # IDYES
            webbrowser.open(release["html_url"])
    except Exception as error:
        show_update_message(f"检查更新失败：{error}\n请稍后重试。", title=f"{APP_NAME} 更新", flags=0x10)


def tray_icon_image() -> Any:
    from PIL import Image

    if is_frozen():
        resource = Path(getattr(sys, "_MEIPASS", "")) / "sources" / "icon.png"
    else:
        resource = base_dir() / "sources" / "icon.png"
    if resource.is_file():
        return Image.open(resource).resize((64, 64))
    # 兜底：生成一个简单占位图标
    image = Image.new("RGB", (64, 64), (69, 200, 144))
    return image


def run_tray(url: str, workspace: Path, server: Any) -> int:
    import pystray

    log_path = workspace / "_app.log"
    print(f"VideoToNo v{VERSION} 已启动: {url}（托盘图标可退出）", flush=True)

    def on_open(_icon: Any, _item: Any) -> None:
        webbrowser.open(url)

    def on_logs(_icon: Any, _item: Any) -> None:
        if log_path.is_file():
            open_in_shell(log_path)

    def on_quit(_icon: Any, _item: Any) -> None:
        server.should_exit = True
        _icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("打开界面", on_open, default=True),
        pystray.MenuItem("查看日志", on_logs),
        pystray.MenuItem("检查更新", check_for_updates),
        pystray.MenuItem("退出", on_quit),
    )
    icon = pystray.Icon("videotono", tray_icon_image(), f"{APP_NAME} · {url}", menu)
    # 任务结果通知：把 backend 的完成/失败事件接到托盘气泡（Windows 通知）
    try:
        from backend import main as backend_main

        backend_main.register_task_notify(
            lambda title, message: icon.notify(message, f"VideoToNo · {title}")
        )
    except Exception:
        pass  # 通知注册失败不影响服务
    icon.run()
    return 0


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------

def main() -> int:
    configure_runtime_dirs()
    workspace = Path(os.environ["VIDEOTONOTES_WORKSPACE"])
    no_browser = os.environ.get("VIDEOTONOTES_NO_BROWSER") == "1"

    try:
        port = find_available_port()
    except RuntimeError as error:
        show_error(str(error))
        return 1

    url = f"http://127.0.0.1:{port}"

    if server_alive(url, mode=RUN_MODE, version=VERSION):
        # 同模式服务已在运行，直接打开界面
        if not no_browser:
            webbrowser.open(url)
        return 0

    if not is_frozen():
        return run_console(url, workspace, port)

    # ---- 打包模式 ----
    log_path = setup_file_logging(workspace)
    try:
        server, _ = start_server(port)
        if not wait_healthy(url, mode=RUN_MODE, version=VERSION):
            server.should_exit = True
            show_error(f"服务启动超时，请查看日志：{log_path}")
            return 1
        if not no_browser:
            webbrowser.open(url)
        return run_tray(url, workspace, server)
    except Exception as error:
        show_error(f"启动失败：{error}\n日志：{log_path}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
