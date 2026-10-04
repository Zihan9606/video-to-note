#!/usr/bin/env bash
# 构建 macOS 应用包（对应 Windows 的 scripts/build_exe.ps1）。
#
# 产物：dist/VideoToNo.app，以及带版本号与固定名的两个副本。
# 与 Windows 版保持同一套形态：onefile + 无控制台 + 托盘常驻 + workspace 落在应用旁边。
set -euo pipefail

PYTHON=""
SKIP_INSTALL=0

while [ $# -gt 0 ]; do
    case "$1" in
        --python) PYTHON="${2:-}"; shift ;;
        --skip-install) SKIP_INSTALL=1 ;;
        -h|--help)
            echo "Usage: ./build_app.sh [--python /path/to/python] [--skip-install]"
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            exit 2
            ;;
    esac
    shift
done

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

if [ -z "$PYTHON" ]; then
    PYTHON="$PROJECT_ROOT/.venv/bin/python"
fi
if [ ! -x "$PYTHON" ]; then
    echo "Python 解释器不存在: $PYTHON" >&2
    exit 1
fi

if [ "$(uname -s)" != "Darwin" ]; then
    echo "scripts/build_app.sh 只能在 macOS 上构建 .app。" >&2
    exit 1
fi

VERSION="$("$PYTHON" - "$PROJECT_ROOT/launcher.py" <<'PY'
import re
import sys
from pathlib import Path

source = Path(sys.argv[1]).read_text(encoding="utf-8")
match = re.search(r'^VERSION = "([^"]+)"', source, re.MULTILINE)
if match is None:
    raise SystemExit("无法从 launcher.py 读取 VERSION")
print(match.group(1))
PY
)"
echo "打包版本: $VERSION"

if [ "$SKIP_INSTALL" -eq 0 ]; then
    "$PYTHON" -m pip install --quiet pyinstaller
fi

# 版本图标：PyInstaller 在 macOS 上只认 .icns，仓库里只维护了 .png/.ico
ICON_PNG="$PROJECT_ROOT/sources/icon.png"
ICONSET="$PROJECT_ROOT/sources/icon.iconset"
ICNS="$PROJECT_ROOT/sources/icon.icns"
if [ ! -f "$ICNS" ]; then
    rm -rf "$ICONSET"
    mkdir -p "$ICONSET"
    for size in 16 32 128 256 512; do
        sips -z "$size" "$size" "$ICON_PNG" --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
        sips -z "$((size * 2))" "$((size * 2))" "$ICON_PNG" --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
    done
    iconutil -c icns "$ICONSET" -o "$ICNS"
    rm -rf "$ICONSET"
    echo "已生成 sources/icon.icns"
fi

# 用 onedir 而不是 onefile：macOS 的 .app 本身就是一个目录，onefile + windowed 在
# PyInstaller 里已弃用（v7.0 起会直接报错），且 onedir 免去每次启动自解压。
"$PYTHON" -m PyInstaller --noconfirm --clean --onedir --windowed \
    --name "VideoToNo" \
    --add-data "frontend:frontend" \
    --add-data "sources/icon.png:sources" \
    --add-data "sources/icon.icns:sources" \
    --collect-data faster_whisper \
    --collect-all sherpa_onnx \
    --collect-submodules mcp.server \
    --collect-data mcp \
    --icon "$ICNS" \
    --osx-bundle-identifier "ai.video-to-note" \
    --hidden-import pystray._darwin \
    --exclude-module tkinter \
    launcher.py

APP="$PROJECT_ROOT/dist/VideoToNo.app"
if [ ! -d "$APP" ]; then
    echo "PyInstaller 未产出 $APP" >&2
    exit 1
fi
# onedir 的中间目录已被 BUNDLE 收进 .app，留在外面只会让人多下一份 270MB
rm -rf "$PROJECT_ROOT/dist/VideoToNo"

# 纯托盘应用不该在 Dock 里留一个没有窗口的图标（对应 Windows 无控制台窗口），
# 同时菜单栏图标退出后进程即结束，两种系统的"常驻"形态保持一致。
"$PYTHON" - "$APP" <<'PY'
import plistlib
import sys
from pathlib import Path

plist_path = Path(sys.argv[1]) / "Contents" / "Info.plist"
with plist_path.open("rb") as handle:
    payload = plistlib.load(handle)
payload["LSUIElement"] = True
with plist_path.open("wb") as handle:
    plistlib.dump(payload, handle)
print("Info.plist: LSUIElement=true")
PY

TARGET="$PROJECT_ROOT/dist/VideoToNo-${VERSION}-macos.app"
rm -rf "$TARGET" "$PROJECT_ROOT/dist/VideoToNo-macos.app"
ditto "$APP" "$TARGET"
ditto "$APP" "$PROJECT_ROOT/dist/VideoToNo-macos.app"

SIZE="$(du -sh "$TARGET" | awk '{print $1}')"
echo ""
echo "构建完成: $TARGET"
echo "固定名（可作快捷方式/开机启动目标）: $PROJECT_ROOT/dist/VideoToNo-macos.app"
echo "大小: $SIZE"
echo "打开方式: open \"$TARGET\""
