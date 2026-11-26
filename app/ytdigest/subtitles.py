"""字幕抓取：yt-dlp 自动/人工字幕优先，失败时由上层退回标题+简介。"""
from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from .config import COOKIES_PATH

# 可选：本机反向上游代理端口（例如 WARP 的 SOCKS5 或 SSH 隧道），存在即自动使用
WARP_PORT = int(os.environ.get("WARP_PROXY_PORT", "40000"))
TUNNEL_PORT = int(os.environ.get("TUNNEL_PROXY_PORT", "18080"))


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.6):
            return True
    except OSError:
        return False


def impersonate_args(target: str = "chrome") -> list[str]:
    """只有安装 curl_cffi 时才能用 --impersonate，否则自动省略。"""
    try:
        import curl_cffi  # noqa: F401
    except ImportError:
        return []
    return ["--impersonate", target]


def _detect_proxy() -> str | None:
    """优先级：YOUTUBE_PROXY > 系统 HTTPS_PROXY > 本地 WARP/隧道端口 > 直连。"""
    for key in ("YOUTUBE_PROXY", "YTDLP_PROXY", "HTTPS_PROXY", "https_proxy"):
        value = os.environ.get(key)
        if value:
            return value
    if _port_open(WARP_PORT):
        return f"socks5h://127.0.0.1:{WARP_PORT}"
    if _port_open(TUNNEL_PORT):
        return f"http://127.0.0.1:{TUNNEL_PORT}"
    return None

_TAG_RE = re.compile(r"<[^>]+>")
_TIME_RE = re.compile(r"^\d{2}:\d{2}:\d{2}[.,]\d{3}\s*-->")


def _clean_vtt(text: str) -> str:
    lines: list[str] = []
    prev = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("WEBVTT") or line.startswith("Kind:") or line.startswith("Language:"):
            continue
        if _TIME_RE.match(line):
            continue
        if line.isdigit():
            continue
        if line.startswith("NOTE"):
            continue
        line = _TAG_RE.sub("", line).strip()
        if not line:
            continue
        if line == prev:
            continue
        # 自动字幕常见的滚动式重复：新行包含上一行
        if prev and line.startswith(prev) and len(line) - len(prev) < 8:
            lines[-1] = line
            prev = line
            continue
        lines.append(line)
        prev = line
    # 合并成段，按句子切分
    joined = " ".join(lines)
    joined = re.sub(r"\s+", " ", joined).strip()
    return joined


def _find_vtt(workdir: Path) -> Path | None:
    candidates = sorted(workdir.glob("*.vtt"))
    if not candidates:
        return None

    def score(p: Path) -> tuple[int, int]:
        name = p.name.lower()
        # 优先原始中文自动字幕，其次是其它中文，最后英文
        if ".zh-hans." in name:
            pri = 0
        elif ".zh." in name:
            pri = 1
        elif ".zh-hant." in name:
            pri = 2
        elif ".en" in name:
            pri = 3
        else:
            pri = 4
        return (pri, len(name))

    return sorted(candidates, key=score)[0]


def fetch_subtitles(video_id: str, langs: str = "zh-Hans,zh-Hant,zh,en",
                    timeout: int = 240) -> tuple[str | None, str]:
    """返回 (字幕文本, 状态)。

    状态：ok=有字幕；none=确认该视频没有字幕；error=抓取失败（网络/风控等）。
    只有 none 才适合触发本地语音转写。
    """
    url = f"https://www.youtube.com/watch?v={video_id}"
    proxy = _detect_proxy()
    attempts: list[list[str]] = [
        [],
        ["--extractor-args", "youtube:player_client=default,mweb,tv"],
        ["--extractor-args", "youtube:player_client=android,ios"],
    ]
    for extra in attempts:
        with tempfile.TemporaryDirectory(prefix="yt-sub-") as td:
            workdir = Path(td)
            cmd = [
                sys.executable,
                "-m",
                "yt_dlp",
                "--skip-download",
                "--ignore-no-formats-error",
                "--no-warnings",
                "--no-playlist",
                "--write-subs",
                "--write-auto-subs",
                "--sub-langs",
                langs,
                "--sub-format",
                "vtt",
                "--retries",
                "2",
                "--socket-timeout",
                "30",
                "-o",
                str(workdir / "%(id)s.%(ext)s"),
            ]
            if COOKIES_PATH.exists():
                cmd += ["--cookies", str(COOKIES_PATH)]
            if proxy:
                cmd += ["--proxy", proxy]
                cmd += impersonate_args()
            cmd += extra
            cmd.append(url)
            try:
                proc = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=timeout
                )
            except subprocess.TimeoutExpired:
                continue
            vtt = _find_vtt(workdir)
            if vtt is not None:
                text = _clean_vtt(vtt.read_text(encoding="utf-8", errors="ignore"))
                if len(text) > 40:
                    return text, "ok"
            if proc.returncode == 0:
                # 成功但没字幕文件，说明该视频确实没有字幕，不必再换 client
                return None, "none"
    return None, "error"


def subtitle_available() -> bool:
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "yt_dlp", "--version"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return proc.returncode == 0
    except Exception:
        return False
