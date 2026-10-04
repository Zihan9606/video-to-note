import json
from pathlib import Path

import launcher


def test_version_tuple_accepts_release_tags() -> None:
    assert launcher.version_tuple("v1.1.4") == (1, 1, 4)
    assert launcher.version_tuple("1.2.0-rc1") == (1, 2, 0)
    assert launcher.version_tuple("invalid") == (0,)


def test_server_alive_requires_matching_version(monkeypatch) -> None:
    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"status":"ok","service":"VideoToNo","mode":"portable","version":"1.1.7"}'

    monkeypatch.setattr(launcher.urllib.request, "urlopen", lambda *args, **kwargs: FakeResponse())

    assert launcher.server_alive("http://127.0.0.1:8000", mode="portable", version="1.1.7")
    assert not launcher.server_alive("http://127.0.0.1:8000", mode="portable", version="1.1.8")


def test_configure_runtime_dirs_creates_custom_workspace(monkeypatch, tmp_path) -> None:
    """环境变量指定的 workdir 不存在时也要自动创建（打包版写日志前依赖此目录）。"""
    custom = tmp_path / "custom-ws"
    assert not custom.exists()
    monkeypatch.setenv("VIDEOTONOTES_WORKSPACE", str(custom))
    monkeypatch.setattr(launcher, "is_frozen", lambda: False)
    launcher.configure_runtime_dirs()
    assert custom.is_dir()


def test_base_dir_keeps_workspace_next_to_the_bundle_on_macos(monkeypatch, tmp_path) -> None:
    """便携工作目录要落在 .app 旁边（对应 Windows 的 exe 旁边）。

    打包后可执行文件在 VideoToNo.app/Contents/MacOS/ 下，直接取 parent 会把
    workspace 写进 bundle 内部——升级换包时整个目录会被删掉。
    """
    executable = tmp_path / "VideoToNo.app" / "Contents" / "MacOS" / "VideoToNo"
    executable.parent.mkdir(parents=True)
    monkeypatch.setattr(launcher, "is_frozen", lambda: True)
    monkeypatch.setattr(launcher.sys, "executable", str(executable))
    monkeypatch.setattr(launcher.sys, "platform", "darwin")

    assert launcher.base_dir() == tmp_path


def test_base_dir_uses_executable_dir_off_macos(monkeypatch, tmp_path) -> None:
    """Windows 便携版一直是 exe 旁边，macOS 改动不能把它带偏。"""
    executable = tmp_path / "VideoToNo.exe"
    monkeypatch.setattr(launcher, "is_frozen", lambda: True)
    monkeypatch.setattr(launcher.sys, "executable", str(executable))
    monkeypatch.setattr(launcher.sys, "platform", "win32")

    assert launcher.base_dir() == tmp_path


def test_frozen_workspace_falls_back_to_application_support_on_macos(
    monkeypatch, tmp_path
) -> None:
    """应用放在不可写目录（/Applications）时，工作目录落到用户的 Application Support。

    不靠 chmod 制造不可写目录：把 workspace 的父路径造成一个普通文件，
    mkdir 必然 ENOTDIR，因此以 root 跑测试也一样成立。
    """
    home = tmp_path / "home"
    (home / "Library" / "Application Support").mkdir(parents=True)
    blocked = tmp_path / "blocked"
    blocked.write_text("", encoding="utf-8")

    monkeypatch.setattr(launcher, "is_frozen", lambda: True)
    monkeypatch.setattr(launcher, "base_dir", lambda: blocked / "VideoToNo.app")
    monkeypatch.setattr(launcher.sys, "platform", "darwin")
    monkeypatch.setenv("HOME", str(home))  # Path.home() 在 POSIX 上读 $HOME
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.delenv("VIDEOTONOTES_WORKSPACE", raising=False)

    launcher.configure_runtime_dirs()

    expected = home / "Library" / "Application Support" / "VideoToNo" / "workspace"
    assert Path(launcher.os.environ["VIDEOTONOTES_WORKSPACE"]) == expected
    assert expected.is_dir()


def test_latest_release_info_reads_tag_and_url(monkeypatch) -> None:
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(
                {"tag_name": "v1.2.0", "html_url": "https://github.com/example/release"}
            ).encode("utf-8")

    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(launcher.urllib.request, "urlopen", fake_urlopen)
    assert launcher.latest_release_info() == {
        "tag_name": "v1.2.0",
        "html_url": "https://github.com/example/release",
    }
    assert captured == {"url": launcher.GITHUB_LATEST_RELEASE_API, "timeout": 5}


def test_check_for_updates_opens_new_release_after_confirmation(monkeypatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr(
        launcher,
        "latest_release_info",
        lambda: {"tag_name": "v9.9.9", "html_url": "https://github.com/example/release"},
    )
    monkeypatch.setattr(launcher, "show_update_message", lambda *args, **kwargs: 6)
    monkeypatch.setattr(launcher.webbrowser, "open", opened.append)

    launcher.check_for_updates()

    assert opened == ["https://github.com/example/release"]


def test_macos_update_dialog_returns_win32_button_ids(monkeypatch) -> None:
    """macOS 对话框要还原成 Windows 的 IDYES/IDNO，否则 `result == 6` 那条判断失效。"""
    answers = iter(
        [
            "button returned:是, gave up:false",
            "button returned:否, gave up:false",
            "button returned:好, gave up:false",
            "",
        ]
    )
    monkeypatch.setattr(launcher.sys, "platform", "darwin")
    monkeypatch.setattr(launcher, "_run_applescript", lambda script: next(answers))

    assert launcher.show_update_message("发现新版本", flags=0x24) == 6
    assert launcher.show_update_message("发现新版本", flags=0x24) == 7
    assert launcher.show_update_message("已是最新", flags=0x40) == 0
    # 用户按 Esc 让 osascript 报错：必须当成"没点是"，绝不能误触打开网页
    assert launcher.show_update_message("发现新版本", flags=0x24) == 7
