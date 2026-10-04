"""本机密钥的静态加密：Windows 用 DPAPI、macOS 用钥匙串，其余平台回退明文。

只加密密钥本身，不加密整份配置文档：provider / base_url / model 不是秘密
（它们本来就会写进任务日志），这样换机器导致密钥解不开时，用户看到的仍然是
有名字的接口列表，只有 Key 变红，而不是整页空白。

威胁模型（诚实版）：平台密钥库（DPAPI / 钥匙串）只把主密钥留在本机本账户，落盘的
只有密文；再叠一个只存在于工作目录的随机盐值文件，把威胁门槛从"以同一用户身份运行的
任意进程"抬高到"能同时读取 workspace 目录和平台密钥库的进程"。
它**不**防御：同用户下同时读到两个文件的恶意进程，以及你把整个 workspace 连同本机
账户一起交给别人。需要真正隔离时，请不要保存 Key，改用页面临时输入。

两个平台的实现刻意同构，好让"换机器就解不开、换盐值就解不开、写后必回读校验"
这套行为在 Windows 和 macOS 上完全一致：

- Windows：`CryptProtectData`（DPAPI）直接吃明文 + 盐值，密文写进配置文件。
- macOS：钥匙串里只存一把随机主密钥；密文 = AES-256-GCM(HKDF(主密钥, salt=盐值), 明文)
  写进配置文件。主密钥不出钥匙串，配置文件被拷走也解不开。
- 其他平台（开发机 / CI）没有这两个密钥库，回退明文保存并通过 `is_secure_storage()`
  如实上报，由界面和文档标注"本机不支持加密"。
"""
from __future__ import annotations

import base64
import ctypes
import logging
import os
import secrets
import shlex
import shutil
import subprocess
import sys
from ctypes import wintypes
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)

ALG_DPAPI = "dpapi-cryptprotect-v1"
ALG_KEYCHAIN = "keychain-aesgcm-v1"
ALG_PLAIN = "plain-v1"
# 真正做了本机加密的后端：界面据此决定要不要提示"本机不支持加密"。
SECURE_ALGORITHMS = frozenset({ALG_DPAPI, ALG_KEYCHAIN})
ENTROPY_FILE_NAME = ".profile_entropy.bin"
ENTROPY_MIN_BYTES = 16
ENTROPY_BYTES = 32
# 杀软/企业策略干扰平台密钥库时的逃生口；命名对齐仓库既有的 VIDEOTONOTES_* 变量。
DISABLE_DPAPI_ENV = "VIDEOTONOTES_DISABLE_DPAPI"
IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"
TRUTHY = {"1", "true", "yes", "on"}

# macOS 钥匙串：主密钥与自检项都用 /usr/bin/security 存取。
KEYCHAIN_BINARY = "security"
KEYCHAIN_SERVICE = "ai.video-to-note.secretbox"
KEYCHAIN_MASTER_ACCOUNT = "master-key"
KEYCHAIN_PROBE_ACCOUNT = "probe"
KEYCHAIN_TIMEOUT = 10.0
MASTER_KEY_BYTES = 32
AES_NONCE_BYTES = 12
AES_TAG_BYTES = 16
# HKDF 的 info 与 GCM 的 AAD 用同一段常量：换用途就换标签，避免两个密钥互相冒用。
KEY_CONTEXT = b"videotono-secretbox-v1"
_master_key_cache: bytes | None = None


class SecretBoxError(RuntimeError):
    """加密层能明确归类的失败，`code` 供上层映射成用户可读提示。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def storage_backend() -> str:
    return _STORAGE_BACKEND


def is_secure_storage() -> bool:
    return _STORAGE_BACKEND in SECURE_ALGORITHMS


def mask_secret(value: str) -> str:
    """只暴露前 4 个字符，与既有的 `/api/llm-config` 掩码格式保持一致。"""
    if not value:
        return ""
    return f"{value[:4]}****"


def protect(plaintext: str, *, entropy_file: Path) -> dict[str, Any]:
    if not plaintext.strip():
        raise SecretBoxError("empty_secret", "API Key 为空，未写入本机")
    data = plaintext.encode("utf-8")
    backend = storage_backend()
    if backend == ALG_PLAIN:
        sealed = data
    elif backend in (ALG_DPAPI, ALG_KEYCHAIN):
        # 两个加密后端都要先拿到本机盐值：换掉盐值必须等于"这台机器解不开"。
        entropy = _read_entropy(entropy_file)
        sealed = (
            _dpapi_protect(data, entropy)
            if backend == ALG_DPAPI
            else _aesgcm_seal(data, _derive_key(entropy))
        )
    else:
        raise SecretBoxError("unsupported_alg", f"未知的本机加密方式：{backend}")
    envelope: dict[str, Any] = {
        "alg": backend,
        "ciphertext": base64.b64encode(sealed).decode("ascii"),
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }
    # 写后即读：密文损坏或平台异常在保存当场就暴露，绝不拿一个解不开的信封覆盖旧 Key。
    try:
        verified = unprotect(envelope, entropy_file=entropy_file)
    except SecretBoxError as exc:
        raise SecretBoxError("verify_failed", f"本机加密结果无法回读（{exc}），已保留原有 Key") from exc
    if verified != plaintext:
        raise SecretBoxError("verify_failed", "本机加密结果校验不一致，已保留原有 Key")
    return envelope


def unprotect(envelope: dict[str, Any], *, entropy_file: Path) -> str:
    algorithm = str(envelope.get("alg") or "")
    raw = str(envelope.get("ciphertext") or "")
    if not raw:
        raise SecretBoxError("malformed", "密钥记录缺少密文")
    try:
        data = base64.b64decode(raw, validate=True)
    except Exception as exc:  # binascii.Error 等多种子类型，统一归类
        raise SecretBoxError("malformed", "密钥记录格式无法解析") from exc
    if algorithm == ALG_PLAIN:
        return _decode(data)
    if algorithm == ALG_DPAPI:
        if storage_backend() != ALG_DPAPI:
            raise SecretBoxError(
                "undecryptable",
                "该 Key 由 Windows 机器加密，当前系统无法解密，请重新填写",
            )
        return _decode(_dpapi_unprotect(data, _read_entropy(entropy_file)))
    if algorithm == ALG_KEYCHAIN:
        if storage_backend() != ALG_KEYCHAIN:
            raise SecretBoxError(
                "undecryptable",
                "该 Key 由 macOS 钥匙串加密，当前系统无法解密，请重新填写",
            )
        return _decode(_aesgcm_open(data, _derive_key(_read_entropy(entropy_file))))
    raise SecretBoxError("unsupported_alg", f"未知的密钥加密方式：{algorithm}")


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SecretBoxError(
            "undecryptable", "密钥无法解密（可能来自其他机器或另一个系统账户）"
        ) from exc


def _read_entropy(path: Path) -> bytes:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        data = b""
    except OSError as exc:
        raise SecretBoxError("entropy_unavailable", f"无法读取本机加密盐值：{exc}") from exc
    if data:
        if len(data) < ENTROPY_MIN_BYTES:
            # 盐值被截断时绝不静默重新生成：那会让已保存的密钥全部作废，
            # 而用户看到的只是"Key 突然解不开"。
            raise SecretBoxError("entropy_invalid", "本机加密盐值文件已损坏，请删除后重新保存 Key")
        return data
    data = secrets.token_bytes(ENTROPY_BYTES)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_bytes(data)
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        temporary.replace(path)
    except OSError as exc:
        raise SecretBoxError("entropy_unavailable", f"无法创建本机加密盐值：{exc}") from exc
    return data


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


_CRYPTPROTECT_UI_FORBIDDEN = 0x01
# 刻意不加 CRYPTPROTECT_LOCAL_MACHINE：机器级作用域会让同机任意用户（含 SYSTEM
# 上的服务）都能解密，共享工作站上等于没有保护。代价是 Key 不随 workspace 搬家。
_CRYPTPROTECT_FLAGS = wintypes.DWORD(_CRYPTPROTECT_UI_FORBIDDEN)


def _make_blob(data: bytes) -> tuple[_DataBlob, Any]:
    buffer = ctypes.create_string_buffer(data, len(data))
    blob = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    # 必须同时返回 buffer：ctypes 的缓冲区一旦被回收，blob 里的指针就悬空。
    return blob, buffer


def _dpapi() -> Any:
    crypt32 = ctypes.windll.crypt32  # type: ignore[attr-defined]
    # 不显式声明 argtypes/restype 时，ctypes 会按 int 推断，x64 上 DWORD 长度会被
    # 静默截断——这是 DPAPI-via-ctypes 最经典的坑。
    pointer_signature = [
        ctypes.POINTER(_DataBlob),
        ctypes.c_wchar_p,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptProtectData.argtypes = pointer_signature
    crypt32.CryptProtectData.restype = wintypes.BOOL
    crypt32.CryptUnprotectData.argtypes = pointer_signature
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    return crypt32


def _dpapi_call(function: Any, data: bytes, entropy: bytes) -> bytes:
    blob, _input_guard = _make_blob(data)
    secret, _entropy_guard = _make_blob(entropy)
    output = _DataBlob()
    ok = function(ctypes.byref(blob), "VideoToNo", ctypes.byref(secret), None, None, _CRYPTPROTECT_FLAGS, ctypes.byref(output))
    if not ok:
        raise SecretBoxError("dpapi_failed", f"Windows 加密调用失败（错误码 {ctypes.GetLastError()}）")
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        # 输出缓冲区由系统分配，必须用 LocalFree 归还；用错分配器会破坏堆。
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        kernel32.LocalFree(output.pbData)


def _dpapi_protect(data: bytes, entropy: bytes) -> bytes:
    return _dpapi_call(_dpapi().CryptProtectData, data, entropy)


def _dpapi_unprotect(data: bytes, entropy: bytes) -> bytes:
    return _dpapi_call(_dpapi().CryptUnprotectData, data, entropy)


# --------------------------------------------------------------------------
# macOS 钥匙串：只存一把随机主密钥，密文落盘
# --------------------------------------------------------------------------

_KEYCHAIN_CORRUPT_MESSAGE = (
    "macOS 钥匙串里的 VideoToNo 主密钥不可用（条目可能被删改或来自另一台机器），"
    "请在「钥匙串访问」中删除 ai.video-to-note 条目后重新保存 Key"
)


def _security_binary() -> str | None:
    """钥匙串工具路径；非 macOS 或工具缺失时返回 None。"""
    if not IS_MACOS:
        return None
    return shutil.which(KEYCHAIN_BINARY)


def _run_security(argv: list[str], *, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    binary = _security_binary()
    if not binary:
        raise SecretBoxError("keychain_unavailable", "本机没有 macOS 钥匙串工具")
    try:
        return subprocess.run(
            [binary, *argv],
            input=stdin,
            capture_output=True,
            text=True,
            timeout=KEYCHAIN_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SecretBoxError("keychain_failed", f"调用 macOS 钥匙串失败：{exc}") from exc


def _keychain_write(account: str, secret: str) -> None:
    """写入/更新一条通用密码。

    命令走 stdin 而不是 argv：argv 对同用户的任何进程都通过 `ps` 可见，
    密钥从那里过一遍等于没加密。这里要写的值只有 base64 主密钥与十六进制自检令牌，
    shlex.quote 对它们是空操作，留着只为兜住将来引入的特殊字符。
    """
    command = " ".join(
        [
            "add-generic-password",
            "-U",
            "-s",
            shlex.quote(KEYCHAIN_SERVICE),
            "-a",
            shlex.quote(account),
            "-w",
            shlex.quote(secret),
        ]
    )
    result = _run_security(["-i"], stdin=command + "\n")
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "未知错误"
        raise SecretBoxError("keychain_failed", f"写入 macOS 钥匙串失败：{detail}")


def _keychain_read(account: str) -> str | None:
    """读取一条通用密码；条目不存在返回 None。"""
    result = _run_security(
        ["find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", account, "-w"]
    )
    if result.returncode != 0:
        return None
    # `security -w` 会在密钥后面补一个换行，密钥本身不会含换行（base64 / 十六进制）。
    return result.stdout.rstrip("\n")


def _keychain_delete(account: str) -> None:
    try:
        _run_security(["delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", account])
    except SecretBoxError:
        pass  # 删不掉只留下一条无害残留，不能因此让保存或自检失败


def _keychain_master_key() -> bytes:
    """钥匙串里那把主密钥；没有就生成并写后回读。"""
    global _master_key_cache
    if _master_key_cache is not None:
        return _master_key_cache
    stored = _keychain_read(KEYCHAIN_MASTER_ACCOUNT)
    if stored:
        try:
            key = base64.b64decode(stored, validate=True)
        except Exception as exc:
            raise SecretBoxError("keychain_corrupt", _KEYCHAIN_CORRUPT_MESSAGE) from exc
        if len(key) != MASTER_KEY_BYTES:
            raise SecretBoxError("keychain_corrupt", _KEYCHAIN_CORRUPT_MESSAGE)
        _master_key_cache = key
        return key
    key = secrets.token_bytes(MASTER_KEY_BYTES)
    encoded = base64.b64encode(key).decode("ascii")
    _keychain_write(KEYCHAIN_MASTER_ACCOUNT, encoded)
    # 写后即读：写失败却被当成"已保存"会让每次启动换一把密钥，旧 Key 全部作废。
    if _keychain_read(KEYCHAIN_MASTER_ACCOUNT) != encoded:
        raise SecretBoxError("keychain_failed", "macOS 钥匙串主密钥回读不一致，未保存")
    _master_key_cache = key
    return key


def _keychain_probe() -> None:
    """真跑一次写→读→删，确认钥匙串在本机可用（等价 DPAPI 的 canary 往返）。"""
    token = secrets.token_hex(16)
    _keychain_write(KEYCHAIN_PROBE_ACCOUNT, token)
    if _keychain_read(KEYCHAIN_PROBE_ACCOUNT) != token:
        raise SecretBoxError("keychain_failed", "macOS 钥匙串自检未通过")
    _keychain_delete(KEYCHAIN_PROBE_ACCOUNT)


def _derive_key(entropy: bytes) -> bytes:
    """从主密钥 + 本机盐值派生数据密钥。

    salt 用工作目录里的盐值，行为才能与 DPAPI 对齐：换掉盐值文件（等价于把
    workspace 拷到另一台机器）就必须解不开，而不是解出一段垃圾当密钥用。
    """
    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    except ImportError as exc:
        raise SecretBoxError("crypto_unavailable", f"缺少 cryptography 依赖：{exc}") from exc
    return HKDF(
        algorithm=hashes.SHA256(),
        length=MASTER_KEY_BYTES,
        salt=entropy,
        info=KEY_CONTEXT,
    ).derive(_keychain_master_key())


def _aesgcm_seal(data: bytes, key: bytes) -> bytes:
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:
        raise SecretBoxError("crypto_unavailable", f"缺少 cryptography 依赖：{exc}") from exc
    nonce = secrets.token_bytes(AES_NONCE_BYTES)
    return nonce + AESGCM(key).encrypt(nonce, data, KEY_CONTEXT)


def _aesgcm_open(blob: bytes, key: bytes) -> bytes:
    if len(blob) < AES_NONCE_BYTES + AES_TAG_BYTES:
        raise SecretBoxError("malformed", "钥匙串加密的密文长度不合法")
    try:
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:
        raise SecretBoxError("crypto_unavailable", f"缺少 cryptography 依赖：{exc}") from exc
    nonce, sealed = blob[:AES_NONCE_BYTES], blob[AES_NONCE_BYTES:]
    try:
        return AESGCM(key).decrypt(nonce, sealed, KEY_CONTEXT)
    except InvalidTag as exc:
        raise SecretBoxError(
            "undecryptable",
            "密钥无法解密（可能来自其他机器、另一个系统账户，或盐值已更换）",
        ) from exc


def _detect_backend() -> str:
    if os.environ.get(DISABLE_DPAPI_ENV, "").strip().lower() in TRUTHY:
        LOGGER.warning("已通过 %s 关闭本机加密，API Key 将以明文保存", DISABLE_DPAPI_ENV)
        return ALG_PLAIN
    if IS_WINDOWS:
        try:
            canary = "videotonotes-probe"
            entropy = secrets.token_bytes(ENTROPY_BYTES)
            sealed = _dpapi_protect(canary.encode("utf-8"), entropy)
            if _dpapi_unprotect(sealed, entropy).decode("utf-8") != canary:
                raise SecretBoxError("verify_failed", "DPAPI 自检未通过")
        except Exception as exc:  # 任何平台异常都退回明文，不能让配置页打不开
            LOGGER.warning("DPAPI 不可用，本机 API Key 将以明文保存：%s", exc)
            return ALG_PLAIN
        return ALG_DPAPI
    if IS_MACOS:
        try:
            _keychain_probe()
        except Exception as exc:  # 钥匙串被锁、被策略禁用、工具缺失都退回明文
            LOGGER.warning("macOS 钥匙串不可用，本机 API Key 将以明文保存：%s", exc)
            return ALG_PLAIN
        return ALG_KEYCHAIN
    return ALG_PLAIN


# 进程内探测一次：避免一半记录加密、一半明文。
_STORAGE_BACKEND = _detect_backend()
