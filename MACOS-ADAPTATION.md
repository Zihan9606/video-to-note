# macOS 适配记录

本文记录把 video-to-note 从「Windows 专用」适配到「macOS 可用且与 Windows 功能一致」的排查结论、
设计决策与验证结果。改动全部在本仓库内，Windows 侧行为未变。

## 一、排查：真正卡住 macOS 的地方

后端主体（FastAPI、yt-dlp、faster-whisper、sherpa-onnx、MCP、前端）本来就是跨平台的，
`bili_login.py` / `douyin_login.py` 甚至已经带了 macOS 浏览器路径与 `ps` 杀进程分支。
基线测试在 macOS 上跑：**307 通过 / 2 跳过**（跳过的正是 Windows 专属的 DPAPI 用例）。

真正缺的是这几块：

| # | Windows 有、macOS 没有 | 影响 |
|---|---|---|
| 1 | `start.ps1` / `stop.ps1` / `restart.ps1` | 没有等价启停能力，只能手动敲 uvicorn |
| 2 | DPAPI 加密落盘 | API Key 在 macOS 上是 **base64 明文**写进 `workspace/llm_keys.json`，`/api/health` 报 `plain-v1`、界面显示「本机不支持加密」 |
| 3 | 打包版托盘常驻 + 弹窗 + 任务通知 | 只有 Windows 便携 exe，macOS 无双击可用的形态 |
| 4 | `scripts/build_exe.ps1` | 无 macOS 构建入口 |
| 5 | CI / Release 只跑 ubuntu + windows | macOS 改动没有回归保护，也不产出 mac 资产 |

## 二、决策

### 1. 密钥加密：macOS 用系统钥匙串，与 DPAPI 同构

`backend/secret_box.py` 新增后端 `keychain-aesgcm-v1`，与 `dpapi-cryptprotect-v1` 保持同一套不变量：

- 钥匙串里**只存一把随机主密钥**（service `ai.video-to-note.secretbox`，account `master-key`）；
- 落盘密文 = `AES-256-GCM(HKDF(主密钥, salt=工作目录盐值), 明文)`，配置文件被拷走也解不开；
- 换盐值 / 换机器 / 换系统账户 → 解不开（与 DPAPI 行为一致）；
- 写后回读校验，失败绝不覆盖旧 Key；
- 启动时做一次真「写→读→删」自检，钥匙串被锁或被策略禁用时退回明文并如实上报
  （`/api/health` 的 `llm_key_storage`、界面的「本机不支持加密」提示沿用原逻辑）；
- 逃生口沿用 `VIDEOTONOTES_DISABLE_DPAPI`。

实现细节：命令走 `security -i` 的 **stdin** 而不是 argv——argv 对同用户任何进程 `ps` 可见，
密钥从那里过一遍等于没加密。`cryptography` 显式写进 `backend/requirements.txt`。

### 2. 启停脚本：bash 版与 PowerShell 版逐条对齐

`start.sh` / `stop.sh` / `restart.sh` 与 `*.ps1` 共用同一套参数、同一份 `.env` 解析、
同一个 `.runtime/server.json` 状态文件，以及同一条底线：校验不出 PID 属于本项目这一份就不动它。

两处 macOS 特有的坑：

- `ps -o command=` 对 venv 启动的进程显示的是**框架 Python 路径**，项目路径根本不在里面，
  所以路径校验只能放在状态文件那一侧；进程归属改用与 `stop.ps1` 等价的三条判据
  （命令行是这个服务 + 是启动它的那个进程 / 是它的子进程 / 命令行带着本项目路径）。
- `stop.sh --all` 找孤儿进程改用**工作目录**（`lsof -d cwd`）判归属，且要把 lsof 输出里的
  `\xe4\xb8\xb4` 这类转义还原——项目路径含中文时，不还原就永远比不相等。

### 3. 打包形态：`.app` + 菜单栏托盘，对应 Windows 的无控制台 exe

`scripts/build_app.sh`：

- PyInstaller `--onedir --windowed`（`--onefile` + windowed 在 macOS 上已弃用，v7.0 起报错）；
- `--hidden-import pystray._darwin`（对应 Windows 的 `pystray._win32`）；
- 从 `sources/icon.png` 生成 `.icns`（`sips` + `iconutil`）；
- `Info.plist` 写入 `LSUIElement=true`：纯托盘应用不在 Dock 留一个没有窗口的图标，
  对应 Windows 便携版没有控制台窗口。

`launcher.py` 的对应改动：

- `show_error` / `show_update_message` 在 macOS 走 AppleScript 对话框，且把按钮结果映射回
  Windows 的 `IDYES(6) / IDNO(7)`——调用方 `result == 6` 这条判断在两个平台语义一致；
- `base_dir()`：`.app` 内的可执行文件在 `Contents/MacOS` 下，工作目录要落在 **.app 旁边**
  （对应 Windows 的 exe 旁边），否则写进 bundle 内部、升级换包即丢；
- 目录不可写时（如 `/Applications`）回退到 `~/Library/Application Support/VideoToNo/workspace/`，
  这是 `%LOCALAPPDATA%` 的 macOS 对应位置（Windows 分支未动）；
- 任务完成/失败通知复用 pystray 的 `icon.notify`，darwin 后端本身就是 `osascript display notification`。

### 4. CI / Release

- `ci.yml` 测试矩阵加入 `macos-latest`；
- `release.yml` 新增 `build-macos` 任务，产出 `VideoToNo-<版本>-macos.zip`，
  `publish` 同时依赖 Windows 与 macOS 构建。

### 5. 其余

- B 站 / 抖音扫码登录的浏览器候选路径补上用户级 `~/Applications`；
- 前端两处「Windows 账户 / Windows DPAPI」文案改为平台中立，`script.js` 缓存版本 bump 到 `20261003-1`；
- `.gitignore` 排除 `sources/icon.icns`、`sources/icon.iconset/`；`.env.example` 补 macOS 路径示例；
- README / README_EN 补 macOS 下载、启停、打包、钥匙串与证书说明。

## 三、验证记录

本机：macOS 15.2（arm64），Python 3.12.9 虚拟环境。

| 项目 | 结果 |
|---|---|
| 全量 pytest | **317 通过 / 2 跳过**（基线 307/2；新增 10 条，跳过的仍是 Windows DPAPI 专属） |
| `compileall` + `node --check` | 通过（与 CI 的 syntax job 同口径） |
| `start.sh` 全路径 | 启动 / 重复启动识别 / `stop.sh` / `stop.sh --all` / `restart.sh` / 端口占用拒绝 全部符合预期 |
| 开发模式烟测 | `/api/health` 报 `keychain-aesgcm-v1`、`storage.secure=true`；Key 落盘为密文，明文不出现 |
| 升级路径 | 改动前保存的 `plain-v1` 明文信封仍能正常解密（老用户不掉 Key） |
| 打包 | `build_app.sh` 产出 271 MB `.app`，`LSUIElement` 生效 |
| 打包版烟测 | 双击形态启动 → `mode=portable`、前端 200、MCP SSE 200、托盘日志出现、Key 以 `keychain-aesgcm-v1` 落盘 |
| **端到端转写** | 上传本机音频 → whisper base 模型下载 → 离线转写 → 取回带时间轴转录：`Hello this, is a test of the video to note transcription pipeline on macOS.`（`E2E_STATUS=completed`） |
| pystray 菜单栏 | 独立进程验证 `icon.run()` 正常起停 |
| AppleScript | 通知 `osascript -e 'display notification'` 返回 0；阻塞式对话框行为符合预期 |

## 四、已知差异与注意事项

- **只验证了 Apple Silicon**：`release.yml` 用 `macos-latest`（arm64），Intel Mac 需自行用
  `./scripts/build_app.sh` 构建。
- **本机未装 Chrome/Edge**，扫码登录流程只验证到「找不到浏览器时按原样报错并给出可操作提示」，
  没有跑通真实扫码；浏览器启动与进程清理分支本身是跨平台代码。
- **Gatekeeper**：从网上下载的 `.app` 首次打开会被拦，需在「系统设置 → 隐私与安全性」点「仍要打开」，
  或执行 `xattr -dr com.apple.quarantine VideoToNo.app`。
- **python.org 安装的 Python 缺证书**：`certificate verify failed` 会让 yt-dlp 和模型下载全部失败，
  需 `./.venv/bin/python "Install Certificates.command"` 或 `export SSL_CERT_FILE=/etc/ssl/cert.pem`。
  README 已写明。
- **钥匙串主密钥不随 workspace 迁移**：与 DPAPI「换机器要重填 Key」是同一语义，不是缺陷。
  主密钥条目可在「钥匙串访问」里按 `ai.video-to-note` 检索删除。

## 五、后续扩展：Safari 扫码登录

原实现只认 Edge/Chrome，没装的 Mac 点「扫码登录导入」只得到一句"未找到 Edge 或 Chrome"。

**为什么不能直接读 Safari 的 Cookie**：Safari 不支持 CDP（Chrome DevTools Protocol）。
官方唯一通道是 `safaridriver` 提供的 WebDriver 接口，代价是需要一次性授权
（Safari 设置 → 高级 → 显示开发菜单 → 开发 → 允许远程自动化）。

实现（`backend/safari_driver.py`，纯标准库，无新依赖）：

- 极简 W3C WebDriver 客户端：起 `safaridriver -p <port>` → 建会话 → 导航 → 取 Cookie → 关会话；
- 端口与 CDP 的 9333/9343 错开（9515–9524），避免撞端口；
- **未授权时把 Apple 的原文翻成可操作指引**：实测 Apple 返回
  `You must enable 'Allow remote automation' in the Developer section of Safari Settings…`，
  现在界面直接告诉你去哪点一下，而不是甩一句找不到浏览器；
- `bili_login.py` / `douyin_login.py` 各加一条 Safari 分支：找不到 Chromium 时自动退回，
  找得到则**优先用 Chromium**（有独立 profile，不碰你的日常浏览器）。

与 Chromium 路线的差异（已写进代码注释与文档）：

1. **没有独立 profile**——safaridriver 用的是你真实的 Safari。对扫码登录反而是好事
   （已登过就直接拿到 Cookie，走的是原有的"已从上次登录恢复"分支），但意味着它不会
   和日常浏览隔离；
2. **取 Cookie 的字段筛选逻辑两条路线共用** `pick_bili_cookies()` /
   `pick_douyin_cookies()`，保证 Safari 和 Chrome 拿到的字段完全一致。

**验证状态**：`safaridriver` 存在性、会话创建失败路径（未授权）→ 已实测并给出正确指引；
**授权后的完整扫码流程尚未联调**——需要在 Safari 里做一次授权（见 README FAQ）。

