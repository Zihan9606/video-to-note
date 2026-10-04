#!/usr/bin/env bash
# 重启 VideoToNo 开发服务：等价于 restart.ps1（stop -Quiet 后原样透传参数给 start）。
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
"$PROJECT_ROOT/stop.sh" --quiet
exec "$PROJECT_ROOT/start.sh" "$@"
