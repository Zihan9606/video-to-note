"""FunASR SenseVoice 转写引擎（官方原生实现，多语言 + 内置标点）。

与 `WhisperTranscriber` / `ParaformerTranscriber` 保持同一返回契约
（segments / text / duration / device / ...），调用方按 model_name 路由。

依赖 `funasr>=1.4.14`（会拉 PyTorch：Windows/CUDA 版 2GB+，macOS 约 150MB）。
模型由 FunASR 自己从 ModelScope/HuggingFace 下载并缓存到 cache_dir。

时间轴：FunASR 的 `generate(output_timestamp=True)` 返回词级时间戳
（`timestamp` 与 `words` 一一对应），这里聚合成段级时间戳——项目契约要 segments。
"""
from __future__ import annotations

import asyncio
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .transcript import TranscriptSegment
from .whisper_asr import (
    TranscriptionCancelledError,
    _ensure_ffmpeg_on_path,
    is_apple_silicon,
    segment_is_junk,
)

# SenseVoice 输出里的特殊标签（语言/情感/事件，如 <|zh|> <|NEUTRAL|> <|Speech|>），
# 正常由 rich_transcription_postprocess 清掉；这里兜一道，后处理不可用时不漏标签
_TAG_RE = re.compile(r"<\|[^|]*\|>")


def _strip_tags(text: str) -> str:
    return _TAG_RE.sub("", text).strip()


# 英文撇号/缩写会被切成独立 token（I, ', m），拼词后要收回：I ' m → I'm
_PUNCT_JOINED_RE = re.compile(r"\s+([.,!?;:'\")\]])")


def _tidy_join(tokens: list[str], joiner: str) -> str:
    joined = joiner.join(tokens).strip()
    if joiner:
        # I ' m → I' m → I'm：先去标点前空格，再去撇号后空格（缩写被切成三个 token）
        joined = _PUNCT_JOINED_RE.sub(r"\1", joined)
        joined = joined.replace("' ", "'")
    return joined


class SenseVoiceTranscriber:
    """FunASR 的 SenseVoiceSmall。"""

    def __init__(self, cache_dir: Path | None = None) -> None:
        # FunASR 自己下载模型，缓存到 <cache_dir>/sensevoice（与 whisper 的 _model_cache 平级）
        self.cache_dir = (cache_dir / "sensevoice") if cache_dir else None
        self._model: Any = None
        self._device = ""
        self._model_lock = threading.Lock()

    def cache_status(self) -> str:
        if not self.cache_dir or not self.cache_dir.is_dir():
            return "missing"
        # FunASR 缓存目录里有模型权重即视为已缓存
        if any(self.cache_dir.rglob("*.pt")) or any(self.cache_dir.rglob("*.onnx")):
            return "cached"
        return "missing"

    async def transcribe(
        self,
        media_path: Path,
        model_name: str = "sensevoice",
        use_gpu: bool = False,
        initial_prompt: str | None = None,
        cancel_event: threading.Event | None = None,
        progress_callback: Callable[[float, float], None] | None = None,
    ) -> dict:
        """转写（与其它引擎同签名，initial_prompt 对 SenseVoice 无效——它不用 prompt）。"""
        try:
            return await asyncio.to_thread(
                self._transcribe_sync, media_path, use_gpu, cancel_event, progress_callback
            )
        except TranscriptionCancelledError as exc:
            raise asyncio.CancelledError("转写已被用户取消") from exc

    def _pick_device(self, use_gpu: bool) -> str:
        """use_gpu → Apple Silicon 用 MPS（核显），有 CUDA 用 CUDA，否则 CPU。"""
        if not use_gpu:
            return "cpu"
        if is_apple_silicon():
            return "mps"
        try:
            import torch

            if torch.cuda.is_available():
                return "cuda:0"
        except Exception:
            pass
        return "cpu"

    def _transcribe_sync(
        self,
        media_path: Path,
        use_gpu: bool,
        cancel_event: threading.Event | None = None,
        progress_callback: Callable[[float, float], None] | None = None,
    ) -> dict:
        try:
            from funasr import AutoModel
            from funasr.utils.postprocess_utils import rich_transcription_postprocess
        except ImportError as exc:
            raise RuntimeError(
                "SenseVoice 需要 funasr：`pip install \"funasr>=1.4.14\"`"
                "（会拉 PyTorch：Windows/CUDA 版 2GB+，macOS 约 150MB）"
            ) from exc

        if cancel_event is not None and cancel_event.is_set():
            raise TranscriptionCancelledError()

        # FunASR 对长音频会调 ffmpeg 解码（短音频走 torchaudio 回退，长的没有回退），
        # 服务进程的 PATH 里通常没有它——复用 MLX 路径那套临时软链
        _ensure_ffmpeg_on_path()

        device = self._pick_device(use_gpu)
        started = time.monotonic()
        with self._model_lock:
            if self._model is None or self._device != device:
                kwargs: dict[str, Any] = {
                    "model": "iic/SenseVoiceSmall",
                    "device": device,
                    "disable_update": True,  # 不联网检查更新（离线转写）
                }
                if self.cache_dir:
                    kwargs["cache_dir"] = str(self.cache_dir)
                # FSMN-VAD 切长音频（FunASR 官方推荐路径），单段上限 30s
                kwargs.update(
                    vad_model="fsmn-vad",
                    vad_kwargs={"max_single_segment_time": 30000},
                )
                self._model = AutoModel(**kwargs)
                self._device = device

        if cancel_event is not None and cancel_event.is_set():
            raise TranscriptionCancelledError()
        if progress_callback is not None:
            # 起点只能报 (0, 0)——报 (0, 1) 会把总时长渲染成 "00:01"
            # （前端 fmt(position)/fmt(duration)），误导用户"音频只有 1 秒"
            progress_callback(0.0, 0.0)

        res = self._model.generate(
            input=str(media_path),
            language="auto",
            use_itn=True,               # 逆文本归一化 + 标点（SenseVoice 内置）
            batch_size_s=60,
            merge_vad=True,
            merge_length_s=15,
            output_timestamp=True,      # 要时间轴（否则只有纯文本）
        )
        if cancel_event is not None and cancel_event.is_set():
            raise TranscriptionCancelledError()

        item = res[0] if res else {}
        raw_text = str(item.get("text") or "")
        try:
            text = rich_transcription_postprocess(raw_text)
        except Exception:
            text = _strip_tags(raw_text)
        text = _strip_tags(text)

        segments = self._segments_from(item, text)
        duration = segments[-1].end if segments else 0.0
        if progress_callback is not None:
            progress_callback(duration, duration)

        return {
            "text": text,
            "segments": segments,
            "language": str(item.get("language") or "auto"),
            "language_probability": 1.0,
            "device": device,
            "model": "sensevoice",
            "requested_model": "sensevoice",
            "duration": duration,
            "duration_after_vad": duration,
            "fallback_note": (
                "SenseVoice（FunASR 原生）：转写期间不显示逐段进度、不能中途打断，"
                "取消会在本次调用结束后生效。"
            ),
            "model_load_seconds": 0.0,
            "transcribe_seconds": round(time.monotonic() - started, 1),
        }

    @staticmethod
    def _segments_from(item: dict, text: str) -> list[TranscriptSegment]:
        """把 FunASR 的词级时间戳聚合成段级（项目契约要 segments[start,end,text]）。

        `output_timestamp=True` 时 item["timestamp"] 是 [[start_s, end_s], ...]，
        与 item["words"] 一一对应；按句读标点或 8 秒窗口聚合。
        拿不到时间戳时退化为单段（start/end 为 0），调用方仍能用。
        """
        pairs = item.get("timestamp") or []
        words = item.get("words") or []
        segments: list[TranscriptSegment] = []
        if pairs and words and len(pairs) == len(words):
            # FunASR 的 timestamp 是**毫秒** [start_ms, end_ms]（官方 README 写明），
            # 不除 1000 会把 60 秒音频的时间轴放大成 59890 秒
            to_sec = lambda value: float(value) / 1000.0
            # words 是纯字符串列表（无空格）：英文加空格拼词，中文不加
            # （中文按字/词切分，加空格会变成"我 们 先 来 看"）。
            # 注意必须**按段**判断 CJK——71 分钟的全局 text 只要含一处中文，
            # 全局判断会让所有英文段都不加空格（Soanywaythecourse…）
            buf_start: float | None = None
            buf_end = 0.0
            buf: list[str] = []
            for (start, end), word in zip(pairs, words):
                token = word[0] if isinstance(word, (list, tuple)) else str(word)
                if buf_start is None:
                    buf_start = to_sec(start)
                buf_end = max(buf_end, to_sec(end))
                buf.append(str(token))
                if re.search(r"[。！？.!?]$", str(token)) or (buf_end - buf_start) >= 8.0:
                    has_cjk = any("\u4e00" <= ch <= "\u9fff" for ch in "".join(buf))
                    joined = _tidy_join(buf, "" if has_cjk else " ")
                    if not segment_is_junk(buf_start, buf_end, joined):
                        segments.append(TranscriptSegment(buf_start, buf_end, joined))
                    buf_start, buf_end, buf = None, 0.0, []
            if buf_start is not None and buf:
                has_cjk = any("\u4e00" <= ch <= "\u9fff" for ch in "".join(buf))
                joined = _tidy_join(buf, "" if has_cjk else " ")
                if not segment_is_junk(buf_start, buf_end, joined):
                    segments.append(TranscriptSegment(buf_start, buf_end, joined))
        elif text:
            segments.append(TranscriptSegment(0.0, 0.0, text))
        return segments
