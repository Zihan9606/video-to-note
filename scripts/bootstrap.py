#!/usr/bin/env python3
"""跨平台环境准备：任意系统一条命令，把仓库变成可运行状态。

    python3 scripts/bootstrap.py

Windows / macOS / Linux 入口完全相同，平台差异全部收在这个脚本内部：
建 venv、装依赖、处理 macOS 上 python.org 安装包缺证书的问题。
它只用标准库，因此在还没装任何依赖的干净机器上也能跑。

跑完之后按提示启动即可；日常操作统一走 scripts/manage.py。
"""
from __future__ import annotations

import os
import platform
import subprocess
import sys
from pathlib import Path

MIN_PYTHON = (3, 11)
REPO_ROOT = Path(__file__).resolve().parent.parent
VENV_DIR = REPO_ROOT / ".venv"
REQUIREMENTS = REPO_ROOT / "backend" / "requirements.txt"

if os.name == "nt":
    VENV_PYTHON = VENV_DIR / "Scripts" / "python.exe"
else:
    VENV_PYTHON = VENV_DIR / "bin" / "python"


def _echo(message: str) -> None:
    print(message, flush=True)


def _fail(message: str) -> int:
    print(f"错误：{message}", file=sys.stderr, flush=True)
    return 1


def _run(command: list[str], **kwargs) -> int:
    _echo("  $ " + " ".join(command))
    return subprocess.run(command, cwd=REPO_ROOT, **kwargs).returncode


def _ensure_python() -> int:
    if sys.version_info < MIN_PYTHON:
        return _fail(
            f"需要 Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+，当前是 "
            f"{platform.python_version()}。装好后重跑本脚本。"
        )
    _echo(f"Python {platform.python_version()}（{sys.executable}）")
    return 0


def _ensure_venv() -> int:
    if VENV_PYTHON.is_file():
        _echo(f"虚拟环境已存在：{VENV_DIR}")
        return 0
    _echo("创建虚拟环境 .venv …")
    return _run([sys.executable, "-m", "venv", str(VENV_DIR)])


def _install_requirements() -> int:
    if not REQUIREMENTS.is_file():
        return _fail(f"找不到依赖清单：{REQUIREMENTS}")
    _echo("安装依赖（首次会下载 whisper / sherpa 等较大包）…")
    rc = _run([str(VENV_PYTHON), "-m", "pip", "install", "--upgrade", "pip", "-q"])
    if rc:
        return rc
    return _run(
        [str(VENV_PYTHON), "-m", "pip", "install", "-r", str(REQUIREMENTS)]
    )


def _fix_macos_certificates() -> None:
    """python.org 装的 Python 不带证书配置，yt-dlp 和模型下载会 certificate verify failed。

    只做本地文件判断，不联网探测——`ssl` 记录的 CA 文件不存在就是这个症状。
    修法优先跑官方的 Install Certificates.command，跑不了就退回 macOS 系统证书。
    """
    if sys.platform != "darwin":
        return
    try:
        probe = subprocess.run(
            [
                str(VENV_PYTHON),
                "-c",
                "import ssl; print(ssl.get_default_verify_paths().openssl_cafile or '')",
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
        cafile = probe.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return
    if cafile and os.path.exists(cafile):
        return

    installer = Path(sys.base_prefix) / "bin" / "Install Certificates.command"
    if installer.is_file():
        _echo(f"修复证书配置：{installer}")
        subprocess.run([str(VENV_PYTHON), str(installer)], cwd=REPO_ROOT, timeout=180)
        return
    if Path("/etc/ssl/cert.pem").is_file():
        _echo("本机缺 CA 证书文件；已让启停脚本自动设 SSL_CERT_FILE=/etc/ssl/cert.pem。")
        return
    print(
        "警告：Python 找不到 CA 证书，且本机没有可用的备用证书，"
        "yt-dlp 与模型下载会失败。请执行 "
        f'"{VENV_PYTHON}" "Install Certificates.command"。',
        file=sys.stderr,
        flush=True,
    )


def _install_playwright_browser() -> None:
    """页面抓取路线要一个真实 Chromium（约 95MB，已装则几秒跳过）。

    装不上也不该让 bootstrap 失败：其余功能（接口路线、转写、笔记）都不依赖它，
    只有"浏览器抓取"这一条路暂时不可用，所以只给警告和补救命令。
    """
    _echo("准备页面抓取用的 Chromium（playwright install chromium）…")
    try:
        code = _run([str(VENV_PYTHON), "-m", "playwright", "install", "chromium"])
    except OSError:
        code = 1
    if code:
        print(
            "警告：Chromium 没装上，「浏览器抓取」暂时用不了（其余功能不受影响）。"
            f"稍后手动跑：{VENV_PYTHON} -m playwright install chromium",
            file=sys.stderr,
            flush=True,
        )


def _next_steps() -> None:
    _echo("")
    _echo("环境就绪。下一步：")
    if os.name == "nt":
        _echo(r"  启动：  powershell -ExecutionPolicy Bypass -File .\start.ps1")
        _echo(r"  停止：  powershell -ExecutionPolicy Bypass -File .\stop.ps1")
    else:
        _echo("  启动：  ./start.sh")
        _echo("  停止：  ./stop.sh")
    _echo("  或者两个平台统一用：  python scripts/manage.py start / stop / test")
    _echo("  跑测试：python scripts/manage.py test")


def main() -> int:
    _echo(f"VideoToNo 环境准备（{platform.system()} {platform.machine()}）")
    _echo(f"仓库：{REPO_ROOT}")
    _echo("")
    if _ensure_python():
        return 1
    if _ensure_venv():
        return 1
    if _install_requirements():
        return 1
    _fix_macos_certificates()
    _install_playwright_browser()
    _next_steps()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
