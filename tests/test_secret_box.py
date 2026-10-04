"""secret_box 测试：跨平台可验证的部分（信封格式、回退、写后校验、盐值文件）。

平台密钥库本身只在对应系统上存在（DPAPI 只在 Windows、钥匙串只在 macOS），Linux CI
跑不到，因此通用用例都强制走明文回退路径，把"回退路径与加密路径共用同一套信封与
校验逻辑"这件事钉住；两个平台的真实往返分别见文件末尾的 skipif 用例与
DEVELOPMENT.md 的手工烟测。
"""
from __future__ import annotations

import base64
import sys

import pytest

from backend import secret_box
from backend.secret_box import ALG_DPAPI, ALG_KEYCHAIN, ALG_PLAIN, SecretBoxError


@pytest.fixture(autouse=True)
def plaintext_backend(monkeypatch) -> None:
    """用例结论必须与运行平台无关：默认强制明文回退。"""
    monkeypatch.setattr(secret_box, "_STORAGE_BACKEND", ALG_PLAIN)


def test_protect_and_unprotect_roundtrip(tmp_path) -> None:
    envelope = secret_box.protect("sk-test-1234", entropy_file=tmp_path / "entropy.bin")

    assert envelope["alg"] == ALG_PLAIN
    assert envelope["saved_at"]
    assert secret_box.unprotect(envelope, entropy_file=tmp_path / "entropy.bin") == "sk-test-1234"


def test_plaintext_fallback_is_obviously_insecure(tmp_path) -> None:
    """回退路径不假装安全：密文就是明文本身，界面必须据此提示"本机不支持加密"。"""
    envelope = secret_box.protect("sk-leak", entropy_file=tmp_path / "entropy.bin")

    assert base64.b64decode(envelope["ciphertext"]) == b"sk-leak"


def test_empty_key_is_rejected(tmp_path) -> None:
    with pytest.raises(SecretBoxError) as error:
        secret_box.protect("   ", entropy_file=tmp_path / "entropy.bin")

    assert error.value.code == "empty_secret"


def test_unknown_algorithm_is_rejected(tmp_path) -> None:
    envelope = {"alg": "aes-but-not-really", "ciphertext": base64.b64encode(b"x").decode()}

    with pytest.raises(SecretBoxError) as error:
        secret_box.unprotect(envelope, entropy_file=tmp_path / "entropy.bin")

    assert error.value.code == "unsupported_alg"


def test_dpapi_envelope_cannot_be_read_off_windows(tmp_path) -> None:
    """跨机器/跨平台读到的 DPAPI 信封必须报"解不开"，而不是返回乱码当密钥用。"""
    envelope = {"alg": ALG_DPAPI, "ciphertext": base64.b64encode(b"not-really").decode()}

    with pytest.raises(SecretBoxError) as error:
        secret_box.unprotect(envelope, entropy_file=tmp_path / "entropy.bin")

    assert error.value.code == "undecryptable"


def test_malformed_envelope_is_rejected(tmp_path) -> None:
    entropy = tmp_path / "entropy.bin"

    with pytest.raises(SecretBoxError) as missing:
        secret_box.unprotect({"alg": ALG_PLAIN, "ciphertext": ""}, entropy_file=entropy)
    with pytest.raises(SecretBoxError) as broken:
        secret_box.unprotect({"alg": ALG_PLAIN, "ciphertext": "!!!"}, entropy_file=entropy)

    assert missing.value.code == "malformed"
    assert broken.value.code == "malformed"


def test_write_time_verification_refuses_bad_envelope(tmp_path, monkeypatch) -> None:
    """加密原语"看起来成功但回读不一致"时，绝不能拿坏信封覆盖掉旧 Key。"""
    monkeypatch.setattr(secret_box, "unprotect", lambda *_args, **_kwargs: "wrong-value")

    with pytest.raises(SecretBoxError) as error:
        secret_box.protect("sk-keep-me", entropy_file=tmp_path / "entropy.bin")

    assert error.value.code == "verify_failed"


def test_verification_reports_unreadable_envelope(tmp_path, monkeypatch) -> None:
    def explode(*_args, **_kwargs):
        raise SecretBoxError("dpapi_failed", "boom")

    monkeypatch.setattr(secret_box, "unprotect", explode)

    with pytest.raises(SecretBoxError) as error:
        secret_box.protect("sk-keep-me", entropy_file=tmp_path / "entropy.bin")

    assert error.value.code == "verify_failed"


def test_entropy_file_is_created_once_and_reused(tmp_path) -> None:
    path = tmp_path / secret_box.ENTROPY_FILE_NAME

    first = secret_box._read_entropy(path)
    second = secret_box._read_entropy(path)

    assert len(first) == secret_box.ENTROPY_BYTES
    assert first == second == path.read_bytes()


def test_truncated_entropy_file_is_never_rotated(tmp_path) -> None:
    """静默重建盐值 = 已保存的 Key 全部作废，用户只会看到"Key 突然解不开"。"""
    path = tmp_path / secret_box.ENTROPY_FILE_NAME
    path.write_bytes(b"short")

    with pytest.raises(SecretBoxError) as error:
        secret_box._read_entropy(path)

    assert error.value.code == "entropy_invalid"


def test_detect_backend_respects_disable_switch(monkeypatch) -> None:
    """逃生口对两个平台都有效：显式要求明文时绝不启用平台密钥库。"""
    for platform_flag in ("IS_WINDOWS", "IS_MACOS"):
        monkeypatch.setattr(secret_box, platform_flag, True)
        monkeypatch.setenv(secret_box.DISABLE_DPAPI_ENV, "1")

        assert secret_box._detect_backend() == ALG_PLAIN

    monkeypatch.delenv(secret_box.DISABLE_DPAPI_ENV, raising=False)


def test_detect_backend_is_plain_on_platforms_without_keystore(monkeypatch) -> None:
    monkeypatch.setattr(secret_box, "IS_WINDOWS", False)
    monkeypatch.setattr(secret_box, "IS_MACOS", False)
    monkeypatch.delenv(secret_box.DISABLE_DPAPI_ENV, raising=False)

    assert secret_box._detect_backend() == ALG_PLAIN


def test_detect_backend_uses_keychain_on_macos(monkeypatch) -> None:
    """macOS 与 Windows 同等：有平台密钥库就必须启用，不能悄悄退回明文。"""
    probed: list[bool] = []
    monkeypatch.setattr(secret_box, "IS_WINDOWS", False)
    monkeypatch.setattr(secret_box, "IS_MACOS", True)
    monkeypatch.delenv(secret_box.DISABLE_DPAPI_ENV, raising=False)
    monkeypatch.setattr(secret_box, "_keychain_probe", lambda: probed.append(True))

    assert secret_box._detect_backend() == ALG_KEYCHAIN
    assert probed == [True]


def test_detect_backend_falls_back_when_keychain_is_unusable(monkeypatch) -> None:
    """钥匙串被锁/被策略禁用/工具缺失时退回明文，绝不能让配置页打不开。"""
    def explode() -> None:
        raise secret_box.SecretBoxError("keychain_failed", "boom")

    monkeypatch.setattr(secret_box, "IS_WINDOWS", False)
    monkeypatch.setattr(secret_box, "IS_MACOS", True)
    monkeypatch.delenv(secret_box.DISABLE_DPAPI_ENV, raising=False)
    monkeypatch.setattr(secret_box, "_keychain_probe", explode)

    assert secret_box._detect_backend() == ALG_PLAIN


def test_keychain_envelope_cannot_be_read_without_the_keystore(tmp_path) -> None:
    """钥匙串信封在没有钥匙串的平台（含被强制明文的本机）上必须报"解不开"。

    与 DPAPI 那条同构：读到自己解不开的信封时绝不能返回乱码当密钥用。
    """
    envelope = {"alg": ALG_KEYCHAIN, "ciphertext": base64.b64encode(b"not-really").decode()}

    with pytest.raises(SecretBoxError) as error:
        secret_box.unprotect(envelope, entropy_file=tmp_path / "entropy.bin")

    assert error.value.code == "undecryptable"


def test_mask_secret_keeps_existing_format() -> None:
    assert secret_box.mask_secret("sk-abcdef123456") == "sk-a****"
    assert secret_box.mask_secret("") == ""


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI 只在 Windows 上存在")
def test_dpapi_roundtrip_on_windows(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(secret_box, "_STORAGE_BACKEND", ALG_DPAPI)

    envelope = secret_box.protect("sk-windows-secret", entropy_file=tmp_path / "entropy.bin")

    assert envelope["alg"] == ALG_DPAPI
    assert secret_box.unprotect(envelope, entropy_file=tmp_path / "entropy.bin") == (
        "sk-windows-secret"
    )
    assert base64.b64decode(envelope["ciphertext"]) != b"sk-windows-secret"


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI 只在 Windows 上存在")
def test_dpapi_key_does_not_survive_a_different_entropy(tmp_path, monkeypatch) -> None:
    """换掉盐值文件（等价于把配置拷到另一台机器）后必须解不开，而不是解出垃圾。"""
    monkeypatch.setattr(secret_box, "_STORAGE_BACKEND", ALG_DPAPI)
    entropy = tmp_path / "a.bin"

    envelope = secret_box.protect("sk-portable", entropy_file=entropy)
    (tmp_path / "b.bin").write_bytes(entropy.read_bytes()[::-1])

    with pytest.raises(SecretBoxError):
        secret_box.unprotect(envelope, entropy_file=tmp_path / "b.bin")


@pytest.mark.skipif(
    sys.platform != "darwin" or secret_box.storage_backend() != ALG_KEYCHAIN,
    reason="钥匙串只在 macOS 上可用（其余平台按设计回退明文）",
)
def test_keychain_roundtrip_on_macos(tmp_path, monkeypatch) -> None:
    """macOS 与 Windows 的 DPAPI 用例同构：真钥匙串往返、密文不等于明文。"""
    monkeypatch.setattr(secret_box, "_STORAGE_BACKEND", ALG_KEYCHAIN)

    envelope = secret_box.protect("sk-macos-secret", entropy_file=tmp_path / "entropy.bin")

    assert envelope["alg"] == ALG_KEYCHAIN
    assert secret_box.unprotect(envelope, entropy_file=tmp_path / "entropy.bin") == (
        "sk-macos-secret"
    )
    assert base64.b64decode(envelope["ciphertext"]) != b"sk-macos-secret"
    assert b"sk-macos-secret" not in base64.b64decode(envelope["ciphertext"])


@pytest.mark.skipif(
    sys.platform != "darwin" or secret_box.storage_backend() != ALG_KEYCHAIN,
    reason="钥匙串只在 macOS 上可用（其余平台按设计回退明文）",
)
def test_keychain_key_does_not_survive_a_different_entropy(tmp_path, monkeypatch) -> None:
    """与 DPAPI 同一条不变量：换掉盐值文件后必须解不开，而不是解出垃圾。"""
    monkeypatch.setattr(secret_box, "_STORAGE_BACKEND", ALG_KEYCHAIN)
    entropy = tmp_path / "a.bin"

    envelope = secret_box.protect("sk-portable", entropy_file=entropy)
    (tmp_path / "b.bin").write_bytes(entropy.read_bytes()[::-1])

    with pytest.raises(SecretBoxError) as error:
        secret_box.unprotect(envelope, entropy_file=tmp_path / "b.bin")

    assert error.value.code == "undecryptable"


@pytest.mark.skipif(
    sys.platform != "darwin" or secret_box.storage_backend() != ALG_KEYCHAIN,
    reason="钥匙串只在 macOS 上可用（其余平台按设计回退明文）",
)
def test_keychain_master_key_is_stored_once_and_reused(tmp_path, monkeypatch) -> None:
    """主密钥只生成一次：每次启动换一把密钥等于让旧 Key 全部作废。"""
    monkeypatch.setattr(secret_box, "_STORAGE_BACKEND", ALG_KEYCHAIN)
    monkeypatch.setattr(secret_box, "_master_key_cache", None)

    first = secret_box._keychain_master_key()
    secret_box._master_key_cache = None
    second = secret_box._keychain_master_key()

    assert first == second
    assert len(first) == secret_box.MASTER_KEY_BYTES
    monkeypatch.setattr(secret_box, "_master_key_cache", None)
