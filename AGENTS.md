# AGENTS.md

给 AI agent（以及新加入的人）的仓库说明。**单仓库、单分支，Windows 与 macOS 共用同一份代码**——
不要为某个平台新建分支，也不要用 `#ifdef` 式的分叉文件去"分别维护"。

## 快速开始（两个平台完全相同）

```bash
python3 scripts/bootstrap.py     # 首次：建 .venv、装依赖、修证书
python3 scripts/manage.py start  # 启动（默认 8000，占用时用 --port 8011）
python3 scripts/manage.py test   # 跑全量测试
```

Windows 上把 `python3` 换成 `python` 即可，其余一字不改。

`scripts/manage.py` 是**唯一**的日常操作入口，它负责把命令分发到平台脚本
（Windows → `start.ps1` / `stop.ps1` / `scripts/build_exe.ps1`，
macOS/Linux → `start.sh` / `stop.sh` / `scripts/build_app.sh`），
并做选项名映射（`--foreground` ↔ `-Foreground`）。

| 命令 | 作用 |
|---|---|
| `python scripts/manage.py start [--port N] [--foreground] [--no-browser]` | 启动服务 |
| `python scripts/manage.py stop [--all]` | 停止（`--all` 连孤儿进程一起） |
| `python scripts/manage.py restart [--port N]` | 重启 |
| `python scripts/manage.py status` | 读状态文件 + 打健康检查 |
| `python scripts/manage.py test [pytest 参数]` | 全量测试 |
| `python scripts/manage.py build` | 打包（Win 便携 exe / macOS .app） |
| `python scripts/bootstrap.py` | 环境准备 |

不要绕过 `manage.py` 去手写平台分支逻辑——那会造出第二份实现。

## 验证标准

改完代码至少跑这三条，与 CI 完全同口径：

```bash
python3 scripts/manage.py test
python3 -m compileall -q backend launcher.py
node --check frontend/script.js && node --check frontend/theme-bootstrap.js
```

当前基线：**370 passed, 2 skipped**（跳过的是 Windows DPAPI 专属用例）。

改到 B 站投稿列表（`backend/bili_space.py` / `/api/bili-space-videos`）时，除单测外还想做一次线上验证：

```bash
# 单页：应返回 count<=30；被限流时会返回结构化 message 而不是报错
curl -s -X POST http://127.0.0.1:8000/api/bili-space-videos \
  -H 'Content-Type: application/json' -d '{"uid":672328094,"mode":"all","max_pages":1}'
# 拿到 next_page 后续传
curl -s ... -d '{"uid":672328094,"mode":"all","max_pages":1,"start_page":<next_page>}'
```

注意 B 站按 IP 限流且会冷却：**连续测试本身就会把自己限流**（实测 412/-352/-403 轮着来）。
线上验证失败不代表代码有问题，先跑单测确认逻辑，再隔一段时间重试。

## 平台差异（只在这一节说明，代码里不做分叉）

| 关注点 | Windows | macOS | 其他 |
|---|---|---|---|
| 本机密钥加密 | DPAPI（`dpapi-cryptprotect-v1`） | 钥匙串（`keychain-aesgcm-v1`） | 明文回退 |
| 启停脚本 | `start.ps1` / `stop.ps1` | `start.sh` / `stop.sh` | 用 `start.sh` |
| 打包 | `scripts/build_exe.ps1` → 便携 exe | `scripts/build_app.sh` → `.app`（菜单栏托盘） | 无 |
| 浏览器扫码登录 | Edge / Chrome（CDP） | Chrome / Edge，缺了退回 Safari（safaridriver） | Chrome / Edge |
| 弹窗 | `MessageBoxW` | AppleScript 对话框（按钮值映射回 `IDYES=6`/`IDNO=7`） | 写 stderr |

改动这些行为时，**两边都要动**，并补对应平台的测试。

## 约定

- **`backend/secret_box.py` 是唯一密钥加密封装**：新增平台只需往 `protect`/`unprotect`
  加分支并纳入 `SECURE_ALGORITHMS`；不变量是「换机器解不开、换盐值解不开、写后必回读校验」。
- **前端改了 `script.js` 或 `index.html`** 要 bump `index.html` 里的 `?v=YYYYMMDD-N`，
  否则浏览器拿旧 JS 配新字段。
- **测试用 `tmp_path`**，不要往真实 `workspace/` 写数据；`workspace/` 已被 `.gitignore` 排除。
- **`tests/test_secret_box.py` 里平台专属用例**用 `skipif` 圈起来，通用用例必须在三个平台都能跑。
- 依赖清单在 `backend/requirements.txt`，测试依赖在 `backend/requirements-dev.txt`。

## 本机注意事项

- **macOS 证书**：python.org 装的 Python 没有 CA 证书配置，`yt-dlp` 和模型下载会
  `certificate verify failed`。`start.sh` 与 `bootstrap.py` 已自动兜底（设
  `SSL_CERT_FILE=/etc/ssl/cert.pem`），不需要手动处理。
- **macOS Safari 扫码登录**：首次需要在 Safari 设置 → 高级 → 勾「显示开发菜单」，
  然后 开发 → 允许远程自动化（或 `sudo safaridriver --enable`）。没开时界面会给出这段指引。
- **B 站风控**：连续请求 `api.bilibili.com` 会吃 `412/-352/-403`。任何批量拉取
  （如 UP 主视频列表）必须带页间延迟与退避重试，并且**失败时返回已拿到的部分**，不要整批丢弃。
- **两条取址路线**：接口路线（`/api/bili-space-videos`）快但易被限流；页面路线
  （`/api/bili-space-crawl`）用 Playwright 开真实 Chromium、**逐页点分页器**并拦截页面自己的
  接口响应，字段更全且实测更稳，但要开窗口。页面数据请求偶发失败时 B 站会把列表显示成
  "还没有投过视频"——**必须用页面顶部的总数校验并重开页面**，绝不能报成"UP 主没视频"。
  页面路线首次需要 `.venv/bin/python -m playwright install chromium`（`bootstrap.py` 会自动装）。

## 相关文档

- `MACOS-ADAPTATION.md` — macOS 适配的决策与验证记录
- `README.md` / `README_EN.md` — 面向用户的说明（中文 / English）
