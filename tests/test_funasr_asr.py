"""SenseVoice（FunASR 原生引擎）的转写契约与时间轴处理。

重点覆盖三个真实踩过的坑：
- FunASR 的 timestamp 是**毫秒**，不除 1000 会把 60 秒音频放大成 59890 秒
- words 是纯字符串列表（无空格）：英文要拼空格、中文不能拼（"我 们 先 来 看"）
- 缩写被切成三个 token（I, ', m），拼完要收回 I'm
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from backend import funasr_asr
from backend.funasr_asr import SenseVoiceTranscriber, _strip_tags, _tidy_join


def _fake_item() -> dict[str, Any]:
    """60 秒音频的模拟返回：毫秒时间戳 + 无空格 words + 带标签 text。

    timestamp 与 words **一一对应**（FunASR 官方 README 明说），数量必须一致。
    """
    words = ["So", "anyway", "the", "course", "I", "'", "m", "going", "to", "teach"]
    # 每词约 0.8 秒，从 1.73s 开始（模拟真实首段）
    timestamp = [[1730 + i * 800, 1730 + i * 800 + 780] for i in range(len(words))]
    return {
        "text": "<|en|><|NEUTRAL|><|Speech|><|withitn|>. So anyway the course I ' m going to teach",
        "timestamp": timestamp,
        "words": words,
    }


def test_segments_use_millisecond_timestamps() -> None:
    """FunASR 的 timestamp 是毫秒，必须换算成秒——否则 60 秒音频变 59890 秒。"""
    segments = SenseVoiceTranscriber._segments_from(_fake_item(), "text")
    assert segments, "应产出段"
    # 关键是毫秒换算：1730ms → 1.73s（不是 1730s）
    assert segments[0].start == pytest.approx(1.73, abs=0.01)
    assert segments[0].end > 9.0, f"段尾应随词推进，实际 {segments[0].end}"
    assert all(s.end < 60 for s in segments), "时间轴不能超过音频长度（60s）"


def test_segments_join_english_but_not_chinese() -> None:
    """英文拼空格、中文不拼（中文按字切分，加空格会变成"我 们 先 来 看"）。"""
    en = SenseVoiceTranscriber._segments_from(_fake_item(), "So anyway the course")
    assert "course" in en[0].text and " I'm going" in en[0].text, en[0].text

    zh_words = ["我", "们", "先", "来", "看", "一", "下"]
    zh_item = {
        "text": "我们先来看一下",
        "timestamp": [[i * 140, i * 140 + 130] for i in range(len(zh_words))],
        "words": zh_words,
    }
    zh = SenseVoiceTranscriber._segments_from(zh_item, "我们先来看一下")
    assert zh and " " not in zh[0].text, f"中文不应有空格：{zh[0].text!r}"


def test_segments_drop_junk_like_pure_punctuation() -> None:
    """纯标点/倒乱段要被 segment_is_junk 滤掉（和 whisper 路径同一判据）。"""
    item = {
        "text": ".",
        "timestamp": [[1470, 1530], [1730, 9950], [9950, 18170]],
        "words": [".", "So", "anyway"],
    }
    segments = SenseVoiceTranscriber._segments_from(item, ". So anyway")
    assert all("." != s.text for s in segments), "纯标点段必须被滤掉"
    assert any("So" in s.text for s in segments)


def test_tidy_join_fixes_abbreviation_apostrophe() -> None:
    assert _tidy_join(["I", "'", "m", "going"], " ") == "I'm going"
    assert _tidy_join(["we", "are", "fine"], " ") == "we are fine"
    assert _tidy_join(["我", "们", "先"], "") == "我们先"


def test_strip_tags_removes_sensevoice_labels() -> None:
    assert _strip_tags("<|zh|><|NEUTRAL|><|Speech|><|withitn|>你好") == "你好"
    assert _strip_tags("正常文本") == "正常文本"


def test_pick_device_routes_apple_to_mps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(funasr_asr, "is_apple_silicon", lambda: True)
    t = SenseVoiceTranscriber()
    assert t._pick_device(True) == "mps", "Apple Silicon + 勾选加速 → 核显"
    assert t._pick_device(False) == "cpu", "不勾选 → CPU"
    monkeypatch.setattr(funasr_asr, "is_apple_silicon", lambda: False)
    assert t._pick_device(True) == "cpu", "非 Apple 且无 CUDA → CPU"


def test_transcribe_returns_whisper_contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """返回契约与其它引擎一致（调用方按 model_name 路由，产物要能互换）。"""
    captured: dict[str, Any] = {}

    class FakeAutoModel:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def generate(self, **kwargs):
            captured.update(generate=kwargs)
            return [_fake_item()]

    import types as _types
    fake_funasr = _types.SimpleNamespace(AutoModel=FakeAutoModel)
    fake_utils = _types.SimpleNamespace(
        postprocess_utils=_types.SimpleNamespace(
            rich_transcription_postprocess=lambda text: _strip_tags(text)
        )
    )
    monkeypatch.setitem(sys.modules, "funasr", fake_funasr)
    monkeypatch.setitem(sys.modules, "funasr.utils", _types.SimpleNamespace(utils=funasr_asr))
    monkeypatch.setitem(sys.modules, "funasr.utils.postprocess_utils", fake_utils.postprocess_utils)

    t = SenseVoiceTranscriber(tmp_path)
    result = t._transcribe_sync(Path("/tmp/whatever.wav"), use_gpu=False)

    # 契约字段
    for key in ("text", "segments", "language", "device", "model",
                "requested_model", "duration", "duration_after_vad",
                "fallback_note", "transcribe_seconds"):
        assert key in result, f"缺少契约字段 {key}"
    assert result["device"] == "cpu"
    assert result["model"] == "sensevoice"
    assert result["segments"], "要有段"
    # generate 收到的参数（时间轴、ITN、VAD）
    gen = captured["generate"]
    assert gen["output_timestamp"] is True, "必须要时间轴"
    assert gen["use_itn"] is True and gen["language"] == "auto"
    assert captured["vad_model"] == "fsmn-vad"


def test_segments_join_by_local_words_not_global_text() -> None:
    """按段判断 CJK——不能用全局 text。

    71 分钟音频的全局 text 只要含一处中文，全局判断会让**所有英文段**
    都不加空格（Soanywaythecourse…）。
    """
    item = {
        "text": "ignored",
        "timestamp": [[i * 800, i * 800 + 780] for i in range(5)],
        "words": ["So", "anyway", "the", "course", "is"],
    }
    # 全局 text 故意含中文
    segments = SenseVoiceTranscriber._segments_from(item, "这是全局中文文本")
    assert segments, "应产出段"
    assert "So anyway" in segments[0].text, f"英文段必须有空格：{segments[0].text!r}"
