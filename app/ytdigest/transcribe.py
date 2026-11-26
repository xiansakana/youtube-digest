"""无字幕视频的本地语音转写（yt-dlp 下载音频 + faster-whisper）。

只有在「确认视频确实没有字幕」时才会被调用，有字幕的视频不会走这里。
模型文件放在挂载卷里（默认 /data/models/whisper），首次使用时自动下载，不打进镜像。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .config import COOKIES_PATH, WORK_DIR
from .subtitles import _detect_proxy, impersonate_args

MODEL_DIR = Path(os.environ.get("WHISPER_MODEL_DIR", "/data/models/whisper"))
_MODEL_CACHE: dict[str, object] = {}


def _download_audio(video_id: str, out_dir: Path, max_minutes: int,
                    timeout: int = 600) -> Path | None:
    """下载并转成 16k 单声道 wav；超长视频直接放弃。"""
    out_tmpl = str(out_dir / f"{video_id}.%(ext)s")
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "-f", "bestaudio/best",
        "-x", "--audio-format", "wav",
        "--postprocessor-args", "-ar 16000 -ac 1",
        "--no-playlist", "--no-warnings",
        "--match-filter", f"duration <= {max_minutes * 60}",
        "--socket-timeout", "30",
        "--retries", "2",
        "-o", out_tmpl,
    ]
    if COOKIES_PATH.exists():
        cmd += ["--cookies", str(COOKIES_PATH)]
    proxy = _detect_proxy()
    if proxy:
        cmd += ["--proxy", proxy]
        cmd += impersonate_args()
    cmd.append(f"https://www.youtube.com/watch?v={video_id}")
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None
    wavs = list(out_dir.glob(f"{video_id}*.wav"))
    return wavs[0] if wavs else None


def _get_model(model_size: str):
    if model_size in _MODEL_CACHE:
        return _MODEL_CACHE[model_size]
    from faster_whisper import WhisperModel  # 延迟导入，避免未启用时加载

    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    model = WhisperModel(
        model_size,
        device="cpu",
        compute_type="int8",
        download_root=str(MODEL_DIR),
        cpu_threads=max(1, (os.cpu_count() or 4)),
    )
    _MODEL_CACHE[model_size] = model
    return model


def transcribe_video(
    video_id: str,
    model_size: str = "small",
    language: str = "zh",
    max_minutes: int = 90,
    max_chars: int = 60000,
) -> str | None:
    """返回转写文本；失败或超长返回 None。"""
    started = time.time()
    try:
        model = _get_model(model_size)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"加载 faster-whisper 模型失败: {exc}") from exc

    with tempfile.TemporaryDirectory(prefix="yt-audio-") as tmp:
        wav = _download_audio(video_id, Path(tmp), max_minutes)
        if wav is None:
            return None
        try:
            segments, info = model.transcribe(
                str(wav),
                language=language or None,
                vad_filter=True,
                beam_size=1,
                condition_on_previous_text=False,
            )
            pieces = []
            length = 0
            for seg in segments:
                text = (seg.text or "").strip()
                if not text:
                    continue
                pieces.append(text)
                length += len(text)
                if length >= max_chars:
                    break
            result = "".join(pieces) if language and language.startswith("zh") else " ".join(pieces)
            result = result[:max_chars].strip()
            if len(result) < 40:
                return None
            # 落盘一份转写原文，便于排查与人工核对
            try:
                WORK_DIR.mkdir(parents=True, exist_ok=True)
                (WORK_DIR / f"transcript-{video_id}.txt").write_text(result, encoding="utf-8")
            except OSError:
                pass
            duration = getattr(info, "duration", 0) or 0
            print(
                f"[transcribe] {video_id} 用时 {time.time() - started:.1f}s，"
                f"音频 {duration:.0f}s，文本 {len(result)} 字",
                flush=True,
            )
            return result
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
