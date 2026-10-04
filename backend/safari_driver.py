"""macOS Safari 的极简 WebDriver 客户端（只用标准库）。

Safari 不支持 Chrome 的调试协议（CDP），程序读不到它的 Cookie；唯一官方通道是
`/usr/bin/safaridriver` 提供的 WebDriver 接口。它只监听本机回环，一次授权
（`sudo safaridriver --enable`，或 Safari 设置 → 高级 → 勾「显示开发菜单」后
菜单栏 开发 → 允许远程自动化）之后即可免密使用。

与 Chromium 路线的两处本质差异，调用方必须知道：

1. **没有独立 profile**：safaridriver 用的是你真实的 Safari，登录态天然保留
   （对扫码登录反而是好事——已经登过就直接拿到 Cookie），但也意味着它不会像
   `--user-data-dir` 那样把会话和日常浏览隔离。
2. **授权是前提**：未授权时 session 创建会被拒绝，错误信息里带着 Apple 的原文，
   本模块会把它翻译成可操作的中文指引，而不是甩一句"未找到浏览器"。
"""
from __future__ import annotations

import json
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

SAFARIDRIVER = "safaridriver"
STARTUP_TIMEOUT = 15.0
REQUEST_TIMEOUT = 20.0
# 与 CDP（9333/9343）错开，避免和已有的 Chromium 调试端口撞在一起
WEBDRIVER_PORT_START = 9515
WEBDRIVER_PORT_COUNT = 10

# Apple 的原文（英文/本地化都可能），翻成用户能照着做的中文。
# 首次使用必然撞上这一条：「You must enable 'Allow remote automation' in the
# Developer section of Safari Settings to control Safari via WebDriver.」
AUTH_HINTS = (
    "not authorized",
    "authoriz",
    "authentication",
    "permission",
    "denied",
    "remote automation",
    "safari settings",
)


class SafariError(RuntimeError):
    """Safari 自动化链路上能明确归类的失败。"""


def safaridriver_path() -> str | None:
    """本机有没有 safaridriver；非 macOS 直接 None。"""
    import sys

    if sys.platform != "darwin":
        return None
    return shutil.which(SAFARIDRIVER)


def authorization_instructions() -> str:
    """一次授权怎么做——报错时原样带给用户。"""
    return (
        "在 Safari 设置 → 高级 → 勾选「在菜单栏中显示开发菜单」，"
        "然后菜单栏 开发 → 允许远程自动化（或在终端执行 sudo safaridriver --enable），"
        "再点一次扫码登录"
    )


def _authorization_error(detail: str) -> SafariError:
    return SafariError(
        "本机没装 Chrome/Edge，已改用 Safari，但它还没开放自动化权限："
        + authorization_instructions()
        + f"（Safari 原文：{detail[:200]}）"
    )


def free_port() -> int | None:
    for port in range(WEBDRIVER_PORT_START, WEBDRIVER_PORT_START + WEBDRIVER_PORT_COUNT):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return None


def _looks_like_auth_error(detail: str) -> bool:
    lowered = detail.lower()
    return any(hint in lowered for hint in AUTH_HINTS)


@dataclass
class SafariSession:
    """一个 WebDriver 会话：持有 safaridriver 进程与 session id。"""

    port: int
    process: subprocess.Popen[Any]
    session_id: str
    started_at: float

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def alive(self) -> bool:
        return self.process.poll() is None


def _request(url: str, payload: dict[str, Any] | None = None, method: str = "") -> dict[str, Any]:
    data = None
    headers = {"Content-Type": "application/json; charset=utf-8", "Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method=method or None)
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        if _looks_like_auth_error(detail):
            raise _authorization_error(detail) from exc
        raise SafariError(f"Safari 自动化请求失败（HTTP {exc.code}）：{detail[:300]}") from exc
    except urllib.error.URLError as exc:
        raise SafariError(f"连不上 safaridriver：{exc.reason}") from exc
    try:
        body = json.loads(raw) if raw else {}
    except ValueError:
        body = {}
    # W3C WebDriver 把错误放在 value.error 里，HTTP 状态仍是 200
    value = body.get("value")
    if isinstance(value, dict) and value.get("error") and not value.get("sessionId"):
        message = str(value.get("message") or "")
        detail = f"{value.get('error')}: {message}"
        if _looks_like_auth_error(detail):
            raise _authorization_error(detail)
        raise SafariError(f"Safari 自动化请求失败：{detail[:400]}")
    return body


def _kill_stale_drivers() -> int:
    """杀掉还挂着的 safaridriver 进程，返回杀掉的个数。

    服务重启时会话对象随进程消失，但 safaridriver 进程还在，Safari 因此仍"配对"着
    一个已经不存在的会话，下次登录会直接撞 `already paired`。这种情况只可能来自
    上一次没关干净的自己人，所以按进程名清理是安全的。
    """
    import os
    import signal

    try:
        result = subprocess.run(
            ["pgrep", "-f", SAFARIDRIVER],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return 0
    killed = 0
    for line in result.stdout.split():
        try:
            os.kill(int(line.strip()), signal.SIGTERM)
            killed += 1
        except (OSError, ValueError):
            continue
    return killed


def start_session(_retry_stale: bool = True) -> SafariSession:
    """启动 safaridriver 并创建一个会话；失败时抛出带指引的 SafariError。"""
    binary = safaridriver_path()
    if not binary:
        raise SafariError("本机没有 safaridriver（仅 macOS 提供）")
    port = free_port()
    if port is None:
        raise SafariError("没有可用的 WebDriver 端口，请稍后重试")

    process = subprocess.Popen(
        [binary, "-p", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + STARTUP_TIMEOUT
    ready = False
    while time.monotonic() < deadline:
        if process.poll() is not None:
            break
        try:
            status = _request(f"http://127.0.0.1:{port}/status")
        except SafariError:
            time.sleep(0.25)
            continue
        if status.get("value", {}).get("ready", True):
            ready = True
            break
        time.sleep(0.25)
    if not ready:
        exited = process.poll() is not None
        if exited:
            raise SafariError(
                "safaridriver 启动失败。" + authorization_instructions()
            )
        process.kill()
        raise SafariError("safaridriver 在超时内未就绪，请稍后重试")

    try:
        created = _request(
            f"http://127.0.0.1:{port}/session",
            {"capabilities": {"alwaysMatch": {"browserName": "Safari"}}},
        )
    except SafariError as exc:
        if process.poll() is None:
            process.kill()
        if _retry_stale and "already paired" in str(exc).lower():
            # 上一次会话没关干净：清掉残留的 safaridriver 再试，只重试一次
            if _kill_stale_drivers():
                time.sleep(1.5)
                return start_session(_retry_stale=False)
        raise
    session_id = str((created.get("value") or {}).get("sessionId") or "")
    if not session_id:
        process.kill()
        raise SafariError("Safari 会话创建失败：未返回 sessionId")

    return SafariSession(
        port=port, process=process, session_id=session_id, started_at=time.time()
    )


def execute_script(session: SafariSession, script: str, *args: Any) -> Any:
    """在当前页面执行一段 JS 并返回结果（W3C WebDriver 的 execute/sync）。

    这是页面抓取路线的基础：space 页面是 SPA 空壳，视频列表只存在于渲染后的 DOM 里，
    不执行 JS 就一个地址也拿不到。
    """
    body = _request(
        f"{session.base_url}/session/{session.session_id}/execute/sync",
        {"script": script, "args": list(args)},
        method="POST",
    )
    return body.get("value")


def navigate(session: SafariSession, url: str) -> None:
    _request(
        f"{session.base_url}/session/{session.session_id}/url",
        {"url": url},
        method="POST",
    )


def cookies(session: SafariSession) -> list[dict[str, Any]]:
    """当前浏览上下文的全部 Cookie（含 httpOnly）。"""
    body = _request(
        f"{session.base_url}/session/{session.session_id}/cookie", method="GET"
    )
    value = body.get("value")
    return list(value) if isinstance(value, list) else []


def close_session(session: SafariSession) -> None:
    """关掉会话（会连带关掉它开的 Safari 窗口）并停掉 safaridriver。"""
    try:
        _request(
            f"{session.base_url}/session/{session.session_id}", method="DELETE"
        )
    except SafariError:
        pass
    if session.process.poll() is None:
        session.process.terminate()
        try:
            session.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            session.process.kill()
