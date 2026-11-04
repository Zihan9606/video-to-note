from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import shutil
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from .download import download_to_file
from .transcript import TranscriptSegment

LOGGER = logging.getLogger(__name__)

# 缓存文件完整性的下限校验：历史上中断的下载可能遗留只有几十字节的
# 损坏 model.bin（文件存在且非空，但 CTranslate2 无法加载），仅靠
# "文件存在"判断会把损坏缓存当成已缓存，且下载器会跳过不修复。
# 下限取真实体积的约 1/2 以下（最小的 tiny model.bin 也有 ~75MB）。
MIN_MODEL_FILE_BYTES = {
    "model.bin": 30 * 1024 * 1024,
    "tokenizer.json": 100 * 1024,
    "vocabulary.txt": 100 * 1024,
    "vocabulary.json": 100 * 1024,
    "preprocessor_config.json": 256,
}

# 模型 id → HF 仓库。Systran 系是官方 CT2 转换；turbo 的"官方 CT2"并不存在
# （Systran/faster-whisper-turbo 在 HF 上是 404，turbo 档自动下载从未成功过），
# 映射到社区事实标准 deepdml/faster-whisper-large-v3-turbo-ct2；
# belle-turbo-zh 是 whisper-turbo 的中文微调（int8，~0.8GB），中文优于 large-v3。
WHISPER_MODEL_REPOS = {
    "tiny": "Systran/faster-whisper-tiny",
    "base": "Systran/faster-whisper-base",
    "small": "Systran/faster-whisper-small",
    "medium": "Systran/faster-whisper-medium",
    "large-v3": "Systran/faster-whisper-large-v3",
    "turbo": "deepdml/faster-whisper-large-v3-turbo-ct2",
    "belle-turbo-zh": "wolfofbackstreet/faster-whisper-belle-whisper-large-v3-turbo-zh-ct2-int8",
}

# 必需文件清单按模型区分：Systran 官方仓用 vocabulary.txt，
# 社区 CT2 转换仓（turbo / belle-turbo-zh）用 vocabulary.json。
# preprocessor_config.json 必须带上：faster-whisper 靠它读 feature_size
# 决定 80/128 mel，缺了 turbo 系（128 mel）会直接报 shape 不匹配。
DEFAULT_MODEL_FILES = ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt")
CT2_MODEL_FILES = (
    "config.json",
    "model.bin",
    "tokenizer.json",
    "vocabulary.json",
    "preprocessor_config.json",
)
MODEL_REQUIRED_FILES = {
    "turbo": CT2_MODEL_FILES,
    "belle-turbo-zh": CT2_MODEL_FILES,
}

# ---------------------------------------------------------------------------
# Apple Silicon 的 GPU 加速（MLX）
#
# faster-whisper 背后的 CTranslate2 官方 pip wheel **不含 Metal 后端**（它只在
# 源码里支持，wheel 没编进去，实测 site-packages 里没有任何 metal 文件），
# 所以 M 系芯片想用核显只能换推理引擎——这里选 Apple 官方的 MLX。
#
# 目前只开 belle 一档。串行实测（60 秒音频、每组独立进程）：openai 系在核显上
# 并没有加速（base 10.3s vs CPU 5.2s、small 27.4s vs 15.2s、medium 60.2s vs
# 41.4s——M1 的 CPU 跑 int8 更快），质量上还丢了开头 30 秒；belle 是
# large-v3-turbo 底座，核显 14.8s vs CPU 94.3s（快 6.4 倍）才是真受益。
# openai 系的映射仍留在 MLX_MODEL_DIRS / MLX_HF_REPOS：把档位加回本名单即可启用。
MLX_MODELS = frozenset({"belle-turbo-zh"})
# 权重目录名：belle 那档是自打包的目录，openai 系直接用 HF 仓库名做目录
MLX_MODEL_DIRS = {
    "belle-turbo-zh": "belle-turbo-zh-8bit",
    **{
        name: repo.rsplit("/", 1)[-1]
        for name, repo in (
            ("tiny", "mlx-community/whisper-tiny-mlx"),
            ("base", "mlx-community/whisper-base-mlx"),
            ("small", "mlx-community/whisper-small-mlx"),
            ("medium", "mlx-community/whisper-medium-mlx"),
            ("large-v3", "mlx-community/whisper-large-v3-mlx"),
        )
    },
}
# 自托管的成品包（belle 需要：社区那份 config 是 transformers 格式，mlx-whisper 不认）
MLX_RELEASE_URLS = {
    "belle-turbo-zh": (
        "https://github.com/Zihan9606/video-to-note/releases/download/"
        "models-mlx/belle-turbo-zh-8bit-mlx.zip"
    ),
}
# openai 系：mlx-community 上就是 mlx-whisper 认的成品（11 字段 config +
# weights.safetensors），**直接下、零转换**——与上面那个"自打包"是两回事
MLX_HF_REPOS = {
    name: repo
    for name, repo in (
        ("tiny", "mlx-community/whisper-tiny-mlx"),
        ("base", "mlx-community/whisper-base-mlx"),
        ("small", "mlx-community/whisper-small-mlx"),
        ("medium", "mlx-community/whisper-medium-mlx"),
        ("large-v3", "mlx-community/whisper-large-v3-mlx"),
    )
}
MLX_MODEL_FILES = ("weights.safetensors", "config.json")
# 权重有两种合法格式，mlx_whisper 的加载器都认（load_models.py 里先找
# safetensors、没有就退回 npz）：我们自打包的 belle 是 safetensors，
# mlx-community 的 openai 系是 npz——校验只认前者会把下好的 npz 误判成"文件不齐"
MLX_WEIGHT_NAMES = ("weights.safetensors", "weights.npz")


def mlx_model_ready(directory: Path) -> bool:
    """MLX 权重目录是否就位：config 齐 + 任一格式的权重在。"""
    return (directory / "config.json").is_file() and any(
        (directory / name).is_file() for name in MLX_WEIGHT_NAMES
    )


def is_apple_silicon() -> bool:
    """MLX 只在 Apple Silicon + macOS 上可用（Intel Mac 走 CPU）。"""
    return platform.system() == "Darwin" and platform.machine() in {"arm64", "aarch64"}


def mlx_supported(model_name: str, *, apple_silicon: bool | None = None) -> bool:
    """某档位在这台机器上能否走 M 系加速。界面与后端校验共用这一个判据，
    免得"界面能选、后端却报错"或反过来。"""
    if apple_silicon is None:
        apple_silicon = is_apple_silicon()
    return bool(apple_silicon) and model_name in MLX_MODELS


def looks_like_hallucination(text: str) -> bool:
    """段落文本是否是幻觉输出（纯标点、单字符、超高重复）。

    mlx-whisper 没有 VAD，片头音乐/长静音会被强行解码成 `! ! !`、`.ALE.A.A.`
    这类复读——实测同一段音频，未加抑制时 1036 段几乎全是 `!`，加抑制后 225 段
    且正文完整。参数能挡掉大头，这里再兜一道，也给"已生成的乱码转录"留个清理口径。

    判据刻意保守：只要含足够多的不同字符就放行，避免把 "Yes"、"嗯" 这类
    真实短答误杀。
    """
    stripped = (text or "").strip()
    if not stripped:
        return True
    if not any(char.isalnum() for char in stripped):
        return True  # ". "、"!!!"、"……"
    if len(stripped) >= 16 and len(set(stripped)) <= 6:
        return True  # ".ALE.ALE.A.A.A…" 这种字符集极窄的复读
    return False


def _ensure_ffmpeg_on_path() -> None:
    """mlx_whisper 解码音频要调 ffmpeg 二进制（faster-whisper 走 PyAV，不需要）。

    系统没装 ffmpeg 时用 imageio-ffmpeg 自带的静态版。注意它的文件名带平台
    后缀（`ffmpeg-macos-aarch64-v7.1`），光把目录加进 PATH 没用——mlx_whisper
    调的是裸命令 `ffmpeg`，所以要在临时目录建一个名字对得上的符号链接。
    只影响本进程，不动用户环境。
    """
    if shutil.which("ffmpeg"):
        return
    try:
        import imageio_ffmpeg

        source = Path(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception:
        return
    if not source.is_file():
        return
    link_dir = Path(tempfile.gettempdir()) / "video-to-note-ffmpeg"
    link_dir.mkdir(parents=True, exist_ok=True)
    link = link_dir / "ffmpeg"
    if link.is_symlink() or link.exists():
        try:
            if link.resolve() != source.resolve():
                link.unlink()
                link.symlink_to(source)
        except OSError:
            pass
    else:
        try:
            link.symlink_to(source)
            link.chmod(0o755)
        except OSError:
            return
    os.environ["PATH"] = str(link_dir) + os.pathsep + os.environ.get("PATH", "")


# 词表文件名不能按"官方仓 .txt / 社区仓 .json"区分：同一个 Systran 官方仓里
# tiny/base/small/medium 是 vocabulary.txt，large-v3 却是 vocabulary.json
# （按 .txt 去下 large-v3 会 404，用户照页面下载 .json 手动导入又被判不完整）。
# CTranslate2 两个名字都认（缺词表会直接 "Cannot load the vocabulary" 加载失败），
# 所以清单里写哪个都只表示"需要词表"：校验时任一存在即算齐备，下载时按顺序试到命中。
VOCABULARY_NAMES = ("vocabulary.txt", "vocabulary.json")

# CTranslate2 加载损坏模型时的典型报错特征（小写匹配）。
CORRUPT_MODEL_ERROR_MARKERS = ("is incomplete", "failed to read a buffer")

GPU_UNAVAILABLE_NOTE = (
    "本机 GPU 不可用，已改用 CPU 转写；后续任务不再重复尝试 GPU。"
    "要用 GPU 请补装 CUDA 12 运行库（cuBLAS/cuDNN）后重启程序。"
)

# 转写心跳间隔，与生成笔记的 LLM 心跳同节奏。belle/whisper 在 CPU 上转写
# 8 分钟音频要约 3 分钟，期间没有任何反馈时「语音转写」阶段看起来像卡死。
TRANSCRIBE_HEARTBEAT_SECONDS = 30.0


class TranscriptionCancelledError(Exception):
    """转写过程中检测到用户取消请求（协作式中止信号）。

    由 async transcribe() 转换为 asyncio.CancelledError，走任务编排里
    统一的取消处理路径（任务标记为已取消）。
    """


class WhisperTranscriber:
    """Lazy faster-whisper wrapper with CPU-safe defaults."""

    def __init__(
        self, download_root: Path | None = None, mlx_root: Path | None = None
    ) -> None:
        self._models: dict[tuple[str, str], Any] = {}
        self._actual_models: dict[tuple[str, str], str] = {}
        self._last_download_error: Exception | None = None
        # 驱动正常但缺 cuBLAS/cuDNN 运行库的机器上，模型能在 cuda 上构造成功、
        # 一到推理才抛错。记下来，本进程后续任务直接走 CPU，不再重复付这次失败。
        self._gpu_unusable = False
        self._model_lock = threading.Lock()
        self.download_root = download_root.resolve() if download_root else None
        if self.download_root:
            self.download_root.mkdir(parents=True, exist_ok=True)
        # MLX 权重单独放（与 CT2 的 download_root 分开：两边文件布局完全不同，
        # 混在一起会让"缺哪些文件"的判断互相干扰）
        self.mlx_root = mlx_root.resolve() if mlx_root else None
        if self.mlx_root:
            self.mlx_root.mkdir(parents=True, exist_ok=True)

    async def transcribe(
        self,
        media_path: Path,
        model_name: str = "base",
        use_gpu: bool = False,
        initial_prompt: str | None = None,
        cancel_event: threading.Event | None = None,
        progress_callback: Callable[[float, float], None] | None = None,
    ) -> dict:
        """转写音视频文件。

        cancel_event：每任务取消事件；转写过程中在段与段之间检查，检测到
        已设置时抛 TranscriptionCancelledError 并转为 asyncio.CancelledError。
        传入 None（如冒烟脚本）时不检查取消。

        progress_callback(已转写到的秒数, 音频总秒数)：转写线程每
        TRANSCRIBE_HEARTBEAT_SECONDS 调一次，供任务层回报进度心跳；
        回调在工作线程执行，任务层自行保证线程安全。
        """
        try:
            return await asyncio.to_thread(
                self._transcribe_sync,
                media_path,
                model_name,
                use_gpu,
                initial_prompt,
                cancel_event,
                progress_callback,
            )
        except TranscriptionCancelledError as exc:
            raise asyncio.CancelledError("转写已被用户取消") from exc

    def _transcribe_sync(
        self,
        media_path: Path,
        model_name: str,
        use_gpu: bool,
        initial_prompt: str | None,
        cancel_event: threading.Event | None = None,
        progress_callback: Callable[[float, float], None] | None = None,
    ) -> dict:
        # Hugging Face 直连在国内网络常超时，默认走 hf-mirror.com 镜像；
        # 可通过环境变量 HF_ENDPOINT 覆盖（切回官方源或自建镜像）。
        os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
        os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
        os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

        # Apple Silicon 勾了加速：换 MLX 路径。CTranslate2 的 pip wheel 没编
        # Metal，faster-whisper 在这台机器上永远只能是 CPU。
        if use_gpu and is_apple_silicon():
            if model_name not in MLX_MODELS:
                # 不静默降级：界面已经挡过一次，这里再拦是防 API 直接调用绕过。
                # 降成 CPU 会让用户以为在用 GPU，跑完才发现慢了三倍。
                raise RuntimeError(
                    f"「{model_name}」没有可用的 M 系加速权重（当前支持："
                    f"{'、'.join(sorted(MLX_MODELS))}）。请取消勾选加速，或换用支持的档位。"
                )
            return self._transcribe_mlx(
                media_path, model_name, initial_prompt, cancel_event, progress_callback
            )

        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise RuntimeError(
                "faster-whisper is not installed. Run pip install -r backend/requirements.txt"
            ) from exc

        if cancel_event is not None and cancel_event.is_set():
            raise TranscriptionCancelledError()
        model_to_load = self._preferred_model(model_name)
        gpu_known_bad = self._gpu_unusable
        load_started = time.monotonic()
        cache_key, device = self._ensure_model(
            WhisperModel, model_name, model_to_load, use_gpu, cancel_event
        )
        load_seconds = round(time.monotonic() - load_started, 1)
        if cancel_event is not None and cancel_event.is_set():
            raise TranscriptionCancelledError()

        fallback_note: str | None = None
        if use_gpu and not gpu_known_bad and device == "cpu":
            # 构造阶段就退回 CPU：这台机器的 GPU 用不了，本进程已经记下了
            fallback_note = GPU_UNAVAILABLE_NOTE
        transcribe_started = time.monotonic()
        try:
            raw_segments, info = self._run_model(
                self._models[cache_key], media_path, initial_prompt, device
            )
        except Exception as error:
            if device != "cuda":
                raise
            # 驱动在、但缺 cuBLAS/cuDNN 运行库的机器上，模型能在 cuda 上构造成功，
            # 一到真正计算才抛 "Library cublas64_12.dll is not found"。这类错误不在
            # 构造阶段，这里不兜住就是整个任务失败，而且坏模型会留在缓存里反复撞。
            with self._model_lock:
                self._models.pop(cache_key, None)
                self._actual_models.pop(cache_key, None)
                self._gpu_unusable = True
            fallback_note = (
                f"GPU 转写不可用（{error}）：已改用 CPU，本机后续任务不再尝试 GPU。"
                "要用 GPU 请补装 CUDA 12 运行库（cuBLAS/cuDNN）后重启程序。"
            )
            cache_key, device = self._ensure_model(
                WhisperModel, model_name, model_to_load, False, cancel_event
            )
            raw_segments, info = self._run_model(
                self._models[cache_key], media_path, initial_prompt, device
            )
        # 惰性生成器逐段消费：每取一段前检查取消事件，用户取消时在段与段
        # 之间停下，而不是等整段音频转写完。单段 C 层解码中途无法打断。
        segments: list[TranscriptSegment] = []
        last_beat = time.monotonic()
        for item in raw_segments:
            if cancel_event is not None and cancel_event.is_set():
                raise TranscriptionCancelledError()
            if (
                progress_callback is not None
                and time.monotonic() - last_beat >= TRANSCRIBE_HEARTBEAT_SECONDS
            ):
                last_beat = time.monotonic()
                progress_callback(float(item.end), float(info.duration or 0))
            if item.text.strip():
                segments.append(
                    TranscriptSegment(
                        float(item.start), float(item.end), item.text.strip()
                    )
                )
        return {
            "text": " ".join(item.text for item in segments),
            "segments": segments,
            "language": info.language,
            "language_probability": info.language_probability,
            "device": device,
            "model": self._actual_models.get(cache_key, model_name),
            "requested_model": model_name,
            "duration": float(info.duration or 0),
            "duration_after_vad": float(info.duration_after_vad or 0),
            "fallback_note": fallback_note,
            "model_load_seconds": load_seconds,
            "transcribe_seconds": round(time.monotonic() - transcribe_started, 1),
        }

    @staticmethod
    def _run_model(model: Any, media_path: Path, initial_prompt: str | None, device: str):
        """一次转写调用。参数集中在这里，GPU 降级重试时不会和首次跑偏。"""
        return model.transcribe(
            str(media_path),
            # CPU 上 beam=5 的时间代价远大于中文收益：CPU 档用 1，GPU 保持 5
            beam_size=5 if device == "cuda" else 1,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
            repetition_penalty=1.1,
            no_repeat_ngram_size=3,
            hallucination_silence_threshold=2.0,
            initial_prompt=initial_prompt,
            word_timestamps=False,
        )

    # ------------------------------------------------------------------
    # Apple Silicon GPU 路径（MLX）
    # ------------------------------------------------------------------

    def _ensure_mlx_model(
        self, model_name: str, cancel_event: threading.Event | None = None
    ) -> Path:
        """确保 MLX 权重在本地就位，返回模型目录。

        本地已就位就直接用（零请求），否则按档位取成品：
        - belle：本仓库 Release 的自打包 zip（社区那份 mlx-whisper 不认，我们打好的）
        - openai 系：mlx-community 直接拉（那边本来就是 mlx-whisper 认的成品格式，
          **零转换**——不是之前删掉的那套"下载 + 重写 config"）
        """
        if not self.mlx_root:
            raise RuntimeError("未配置 MLX 模型目录，无法使用 M 系加速")
        directory = self.mlx_root / MLX_MODEL_DIRS[model_name]
        if mlx_model_ready(directory):
            return directory

        release_url = MLX_RELEASE_URLS.get(model_name)
        try:
            if release_url:
                self._download_mlx_release(release_url, directory, cancel_event)
            else:
                self._download_mlx_from_hf(model_name, directory, cancel_event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            source = release_url or MLX_HF_REPOS.get(model_name, "")
            raise RuntimeError(
                f"MLX 模型 {model_name} 下载失败：{exc}\n"
                f"可以手动下载 {source} 放到 {directory}"
            ) from exc
        if not mlx_model_ready(directory):
            raise RuntimeError(
                "MLX 模型 "
                f"{model_name} 下载完成但文件不齐（需要 config.json + weights 任一格式），"
                f"请重新下载或手动放置到 {directory}"
            )
        return directory

    def _download_mlx_from_hf(
        self, model_name: str, directory: Path, cancel_event: threading.Event | None
    ) -> None:
        """从 mlx-community 直接拉成品权重（openai 系，无需任何转换）。"""
        try:
            from huggingface_hub import snapshot_download
        except ImportError as exc:
            raise RuntimeError(
                "下载 MLX 模型需要 huggingface-hub（随 mlx-whisper 安装）"
            ) from exc

        os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
        os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
        directory.mkdir(parents=True, exist_ok=True)
        # 注意 snapshot_download 没有 cancel_event 参数（那是别的下载入口的），
        # 传了会 TypeError；这里的下载是一次性的，取消交给进程退出处理。
        snapshot_download(
            repo_id=MLX_HF_REPOS[model_name],
            local_dir=str(directory),
        )

    def _download_mlx_release(
        self, url: str, directory: Path, cancel_event: threading.Event | None
    ) -> None:
        """下载 Release 的成品 zip 并解压到模型目录。

        zip 里包了一层同名目录（打包时 `zip -r ... <目录>/`），解压到 mlx_root
        就正好落在 directory，无需再搬一次。
        """
        directory.parent.mkdir(parents=True, exist_ok=True)
        archive = directory.parent / f"{directory.name}.zip"
        ok, error = download_to_file(
            url,
            archive,
            headers={"User-Agent": "video-to-note"},
            cancel_event=cancel_event,
            attempts=3,
            timeout=120,
        )
        if not ok:
            raise RuntimeError(f"Release 下载失败：{error}")
        import zipfile

        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(directory.parent)
        archive.unlink(missing_ok=True)
        if not all((directory / name).is_file() for name in MLX_MODEL_FILES):
            raise RuntimeError("Release 包里没有可用的模型文件")

    def _transcribe_mlx(
        self,
        media_path: Path,
        model_name: str,
        initial_prompt: str | None,
        cancel_event: threading.Event | None = None,
        progress_callback: Callable[[float, float], None] | None = None,
    ) -> dict:
        try:
            import mlx_whisper
        except ImportError as exc:
            raise RuntimeError(
                "M 系加速需要 mlx-whisper：`pip install mlx-whisper`"
            ) from exc

        _ensure_ffmpeg_on_path()
        model_dir = self._ensure_mlx_model(model_name, cancel_event)
        if cancel_event is not None and cancel_event.is_set():
            raise TranscriptionCancelledError()

        started = time.monotonic()
        # mlx-whisper 没有逐段回调，转写期间既给不了心跳，也**不能中途取消**
        # ——这是它的能力边界，如实写进 fallback_note，不假装能取消。
        #
        # 幻觉抑制（与 faster-whisper 路径的 vad/repetition 手段对齐，用 mlx 支持的
        # 等价参数，A/B 实测见下面这组数字）：
        #   未加抑制：0-400s 转出 1036 段、几乎全是 "! ! !"，正文被淹没
        #   加抑制后：225 段、正文完整，initial_prompt（视频标题）也不再被复读
        result = mlx_whisper.transcribe(
            str(media_path),
            path_or_hf_repo=str(model_dir),
            initial_prompt=initial_prompt,
            verbose=False,
            # 断开上下文传递：片头静音产出的幻觉会顺着 previous text 一路复读下去
            condition_on_previous_text=False,
            # 0.6 = openai/whisper 官方默认。0.9 会把开头 30 秒也判成静音跳掉——
            # 串行实测 mlx/base 与 mlx/large-v3 都因此丢了第一句
            no_speech_threshold=0.6,
            hallucination_silence_threshold=2.0,
            # 重复文本更容易触发压缩比超标；超标后按温度序列重解
            compression_ratio_threshold=2.0,
            temperature=(0.0, 0.2, 0.4, 0.6, 0.8, 1.0),
        )
        if cancel_event is not None and cancel_event.is_set():
            raise TranscriptionCancelledError()

        segments = [
            TranscriptSegment(
                float(item["start"]), float(item["end"]), str(item.get("text") or "").strip()
            )
            for item in (result.get("segments") or [])
            if str(item.get("text") or "").strip()
            and not looks_like_hallucination(str(item.get("text") or ""))
        ]
        duration = float(result.get("duration") or 0)
        if progress_callback is not None and duration:
            progress_callback(duration, duration)
        return {
            "text": " ".join(item.text for item in segments),
            "segments": segments,
            "language": result.get("language") or "unknown",
            "language_probability": float(result.get("language_probability") or 0.0),
            "device": "metal",
            "model": model_name,
            "requested_model": model_name,
            "duration": duration,
            "duration_after_vad": duration,
            "fallback_note": (
                "M 系加速（Metal）：转写期间不显示逐段进度，也不能中途打断"
                "（mlx-whisper 的能力边界）；取消会在这一段转写结束后立即生效。"
            ),
            "model_load_seconds": 0.0,
            "transcribe_seconds": round(time.monotonic() - started, 1),
        }

    def _ensure_model(
        self,
        model_class: Any,
        model_name: str,
        model_to_load: str,
        use_gpu: bool,
        cancel_event: threading.Event | None = None,
    ) -> tuple[tuple[str, str], str]:
        """Load each shared model once, including first-download and GPU fallback."""
        use_gpu = use_gpu and not self._gpu_unusable
        device = "cuda" if use_gpu else "cpu"
        compute_type = "float16" if use_gpu else "int8"
        cache_key = (model_name, device)
        with self._model_lock:
            if cache_key in self._models:
                return cache_key, device
            try:
                self._models[cache_key] = self._load_model(
                    model_class, model_to_load, device, compute_type, cancel_event
                )
                self._actual_models[cache_key] = model_to_load
            except Exception as gpu_error:
                if not use_gpu:
                    self._load_cached_base_or_raise(
                        model_class, cache_key, model_name, device, gpu_error, cancel_event
                    )
                else:
                    # 记下来：本进程后续任务直接走 CPU，不再重付一次注定失败的 cuda 初始化
                    self._gpu_unusable = True
                    device = "cpu"
                    cache_key = (model_name, device)
                    if cache_key not in self._models:
                        try:
                            self._models[cache_key] = self._load_model(
                                model_class, model_to_load, device, "int8", cancel_event
                            )
                            self._actual_models[cache_key] = model_to_load
                        except Exception as cpu_error:
                            self._load_cached_base_or_raise(
                                model_class, cache_key, model_name, device, cpu_error, cancel_event
                            )
        return cache_key, device

    def _load_model(
        self,
        model_class: Any,
        model_name: str,
        device: str,
        compute_type: str,
        cancel_event: threading.Event | None = None,
    ) -> Any:
        cached_path = self._cached_model_path(model_name)
        if not cached_path and self.download_root:
            # 自建下载（默认 hf-mirror 镜像），避免 huggingface_hub 对镜像的兼容问题
            cached_path = self._download_model_files(model_name, cancel_event)
            # 不要在自建下载失败后再让 huggingface_hub 重复请求同一个地址；
            # 那会把真正的网络/证书错误包装成“找不到 snapshot”。
            if not cached_path and self._last_download_error:
                raise self._last_download_error
        model_reference = str(cached_path) if cached_path else model_name
        options: dict[str, Any] = {
            "device": device,
            "compute_type": compute_type,
        }
        if self.download_root and not cached_path:
            options["download_root"] = str(self.download_root)
        try:
            return model_class(model_reference, **options)
        except Exception as exc:
            # 缓存里的模型文件可能已损坏（历史中断下载遗留，体积校验也可能
            # 漏掉的截断）。识别典型报错后删除损坏文件并自动重下一次，
            # 避免“已缓存却永远加载失败”的死循环。
            if (
                cached_path
                and self._looks_like_corrupt_model_error(exc)
                and self._is_managed_model_dir(Path(cached_path))
            ):
                self._discard_corrupt_model(
                    Path(cached_path), self._required_files(model_name)
                )
                refreshed = (
                    self._download_model_files(model_name, cancel_event)
                    if self.download_root
                    else None
                )
                if refreshed:
                    return model_class(str(refreshed), **options)
            raise

    @staticmethod
    def _model_repo(model_name: str) -> str:
        return WHISPER_MODEL_REPOS.get(
            model_name, f"Systran/faster-whisper-{model_name}"
        )

    @staticmethod
    def _required_files(model_name: str) -> tuple[str, ...]:
        return MODEL_REQUIRED_FILES.get(model_name, DEFAULT_MODEL_FILES)

    @staticmethod
    def _slot_candidates(filename: str) -> tuple[str, ...]:
        """清单里的词表项展开成两个可接受的文件名（清单写的优先），其余文件原样。"""
        if filename not in VOCABULARY_NAMES:
            return (filename,)
        return (filename,) + tuple(name for name in VOCABULARY_NAMES if name != filename)

    @classmethod
    def manual_import_files(cls, model_name: str) -> list[str]:
        """手动导入引导用的文件清单：词表两个名字都收，直接告诉用户任选其一即可。"""
        return [
            " 或 ".join(cls._slot_candidates(filename))
            if filename in VOCABULARY_NAMES
            else filename
            for filename in cls._required_files(model_name)
        ]

    def _download_model_files(
        self, model_name: str, cancel_event: threading.Event | None = None
    ) -> Path | None:
        """下载 Whisper 模型到标准缓存目录结构。

        新版 huggingface_hub 校验重定向响应的 X-Repo-Commit 头，hf-mirror
        等镜像不返回该头导致原生下载失败，因此这里自己实现下载：
        走 HF_ENDPOINT（默认 hf-mirror.com），支持断点续传与重试。
        """
        self._last_download_error = None
        configured_endpoint = os.environ.get("HF_ENDPOINT", "https://hf-mirror.com")
        endpoint = configured_endpoint.rstrip("/")
        endpoints = [endpoint]
        # 镜像证书或代理链路异常时，官方源通常仍可用。仅对默认镜像追加，
        # 不改变用户通过 HF_ENDPOINT 指定的其他自建 endpoint。
        if endpoint == "https://hf-mirror.com":
            endpoints.append("https://huggingface.co")

        for candidate in endpoints:
            try:
                snapshot = self._download_model_files_from_endpoint(
                    model_name, candidate, cancel_event
                )
                if snapshot:
                    return snapshot
            except Exception as exc:
                self._last_download_error = exc
        return None

    def _download_model_files_from_endpoint(
        self, model_name: str, endpoint: str, cancel_event: threading.Event | None = None
    ) -> Path | None:
        """从单个 Hugging Face endpoint 下载完整快照。"""
        repo = self._model_repo(model_name)
        base_url = f"{endpoint}/{repo}/resolve/main"
        headers = {"User-Agent": "VideoToNo/1.0", "Accept-Encoding": "identity"}

        # 从 307 重定向响应头取 commit hash（目录命名用）
        commit = "main"
        try:
            request = urllib.request.Request(
                f"{base_url}/config.json", headers=headers, method="HEAD"
            )
            with urllib.request.urlopen(request, timeout=20) as response:
                commit = response.headers.get("X-Repo-Commit") or commit
        except Exception:
            pass

        snapshot = (
            self.download_root
            / f"models--{repo.replace('/', '--')}"
            / "snapshots"
            / commit
        )
        snapshot.mkdir(parents=True, exist_ok=True)
        required_files = self._required_files(model_name)
        for filename in required_files:
            candidates = self._slot_candidates(filename)
            # 槽位已有文件必须通过完整性下限校验；损坏/截断的遗留文件会被重新下载
            if any(self._file_complete(snapshot / name) for name in candidates):
                continue
            downloaded = False
            for name in candidates:
                if cancel_event is not None and cancel_event.is_set():
                    raise asyncio.CancelledError("任务已取消")
                downloaded = self._download_one_file(
                    f"{base_url}/{name}", snapshot / name, headers, cancel_event
                )
                if downloaded:
                    # 换名成功：别把上一个候选的 404 留下冒充真正的失败原因
                    self._last_download_error = None
                    break
                # 404 说明这个仓库用的不是这个名字（large-v3 只有 vocabulary.json），
                # 换候选重试才有意义；其他错误换个文件名也一样过不去。
                if not self._is_missing_on_remote():
                    break
            if not downloaded:
                if self._last_download_error:
                    raise self._last_download_error
                return None
        if not self._snapshot_complete(snapshot, required_files):
            return None
        return snapshot

    def _download_one_file(
        self,
        url: str,
        target: Path,
        headers: dict[str, str],
        cancel_event: threading.Event | None = None,
    ) -> bool:
        """单文件下载：断点续传（Range）+ 重试三次 + Content-Length 完整性校验。"""
        ok, error = download_to_file(url, target, headers, cancel_event)
        if error is not None:
            self._last_download_error = error
        return ok

    def _load_cached_base_or_raise(
        self,
        model_class: Any,
        cache_key: tuple[str, str],
        requested_model: str,
        device: str,
        original_error: Exception,
        cancel_event: threading.Event | None = None,
    ) -> None:
        if requested_model == "base" or not self._cached_model_path("base"):
            raise self._model_load_error(requested_model, original_error) from original_error
        self._models[cache_key] = self._load_model(
            model_class, "base", device, "int8", cancel_event
        )
        self._actual_models[cache_key] = "base"

    def _preferred_model(self, requested_model: str) -> str:
        # 让用户选择的模型先真正尝试加载/下载；只有 _ensure_model 捕获到
        # 下载或加载异常时，才由 _load_cached_base_or_raise 降级到 base。
        return requested_model

    def _snapshot_dirs(self, model_name: str) -> list[Path]:
        """该模型可能存放文件的所有目录：手动导入目录优先，其次各 revision 快照。"""
        dirs: list[Path] = []
        repo_name = f"models--{self._model_repo(model_name).replace('/', '--')}"
        roots: list[Path] = []
        if self.download_root:
            # 手动导入约定目录：manual/{model}/ 或 manual/faster-whisper-{model}/
            for alias in (model_name, f"faster-whisper-{model_name}"):
                dirs.append(self.download_root / "manual" / alias)
            roots.append(self.download_root)
        try:
            from huggingface_hub.constants import HF_HUB_CACHE

            roots.append(Path(HF_HUB_CACHE))
        except ImportError:
            pass
        for root in roots:
            snapshots_dir = root / repo_name / "snapshots"
            if not snapshots_dir.is_dir():
                continue
            dirs.extend(
                sorted(
                    (path for path in snapshots_dir.iterdir() if path.is_dir()),
                    key=lambda path: path.stat().st_mtime,
                    reverse=True,
                )
            )
        return dirs

    def _cached_model_path(self, model_name: str) -> Path | None:
        direct_path = Path(model_name)
        if direct_path.is_dir():
            return direct_path.resolve()

        required_files = self._required_files(model_name)
        for snapshot in self._snapshot_dirs(model_name):
            if self._snapshot_complete(snapshot, required_files):
                return snapshot.resolve()
        return None

    def mlx_cache_status(self, model_name: str) -> str:
        """MLX 权重的本地状态，口径与 `_model_cache_status` 一致。

        多一个 unsupported：这台机器不支持加速、或该档位没有 MLX 权重时，
        界面要能明确区分"没下过"和"根本不支持"。
        """
        if not mlx_supported(model_name) or not self.mlx_root:
            return "unsupported"
        directory = self.mlx_root / MLX_MODEL_DIRS[model_name]
        if mlx_model_ready(directory):
            return "cached"
        if directory.is_dir() and any(directory.iterdir()):
            return "incomplete"
        return "missing"

    def _model_cache_status(self, model_name: str) -> str:
        """模型缓存状态：cached（完整）/ incomplete（部分或损坏文件）/ missing（无）。"""
        if self._cached_model_path(model_name):
            return "cached"
        for snapshot in self._snapshot_dirs(model_name):
            if snapshot.is_dir() and any(
                (snapshot / name).is_file()
                for filename in self._required_files(model_name)
                for name in self._slot_candidates(filename)
            ):
                return "incomplete"
        return "missing"

    @classmethod
    def _snapshot_complete(cls, snapshot: Path, files: tuple[str, ...]) -> bool:
        """槽位齐备：词表两个文件名都算数，其余文件名按清单原样要求。"""
        return all(
            any(cls._file_complete(snapshot / name) for name in cls._slot_candidates(filename))
            for filename in files
        )

    @staticmethod
    def _file_complete(path: Path) -> bool:
        """文件存在且通过体积下限校验（拦截损坏/截断的遗留文件）。"""
        if not path.is_file():
            return False
        minimum = MIN_MODEL_FILE_BYTES.get(path.name, 1)
        return path.stat().st_size >= minimum

    def _is_missing_on_remote(self) -> bool:
        """最近一次下载失败是否是"仓库里没有这个文件"（404，确定性结果）。"""
        error = self._last_download_error
        return isinstance(error, urllib.error.HTTPError) and error.code == 404

    @staticmethod
    def _looks_like_corrupt_model_error(error: Exception) -> bool:
        message = str(error).lower()
        return any(marker in message for marker in CORRUPT_MODEL_ERROR_MARKERS)

    def _is_managed_model_dir(self, path: Path) -> bool:
        """目录是否由本工具的缓存管理；用户手填的外部路径绝不自动删改。"""
        if self.download_root:
            try:
                path.relative_to(self.download_root)
                return True
            except ValueError:
                pass
        try:
            from huggingface_hub.constants import HF_HUB_CACHE

            path.relative_to(Path(HF_HUB_CACHE))
            return True
        except (ImportError, ValueError):
            return False

    def _discard_corrupt_model(self, snapshot: Path, files: tuple[str, ...]) -> None:
        """删除损坏的模型文件（加载报错的 model.bin 及未完成分片、体积异常的小文件）。"""
        (snapshot / "model.bin").unlink(missing_ok=True)
        (snapshot / "model.bin.part").unlink(missing_ok=True)
        for filename in files:
            for name in self._slot_candidates(filename):
                path = snapshot / name
                minimum = MIN_MODEL_FILE_BYTES.get(name, 1)
                if path.is_file() and path.stat().st_size < minimum:
                    path.unlink(missing_ok=True)

    def _model_load_error(self, model_name: str, error: Exception) -> RuntimeError:
        message = str(error).strip() or error.__class__.__name__
        cache_hint = f"，缓存目录：{self.download_root}" if self.download_root else ""
        manual_hint = (
            f"也可手动下载所需模型文件放入缓存目录的 manual/{model_name} 子文件夹"
            "（界面「手动导入模型」按钮可直达）后重试。"
            if self.download_root
            else ""
        )
        return RuntimeError(
            f"Whisper 模型 {model_name} 下载或加载失败{cache_hint}。{manual_hint}"
            "下载默认走 hf-mirror.com 镜像；若仍失败，请检查网络/代理，"
            "或通过 HF_ENDPOINT 环境变量指定可用镜像后重试。"
            f"原始错误：{message}"
        )
