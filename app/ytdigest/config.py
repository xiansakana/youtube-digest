"""配置：环境变量优先，其次 data/config.json，最后内置默认值。

容器部署时只需要提供环境变量；本机运行时可以用 data/config.json 覆盖。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

BASE_DIR = Path(os.environ.get("YTDIGEST_HOME", "/data")).resolve()
CONFIG_PATH = BASE_DIR / "config.json"
TOKEN_PATH = Path(os.environ.get("YTDIGEST_TOKEN", str(BASE_DIR / "token.json")))
ENV_PATH = BASE_DIR / ".env"
STATE_DIR = BASE_DIR / "state"
SENT_PATH = STATE_DIR / "sent.json"
LOG_DIR = BASE_DIR / "logs"
WORK_DIR = BASE_DIR / "work"
COOKIES_PATH = Path(os.environ.get("YOUTUBE_COOKIES", str(BASE_DIR / "cookies.txt")))

CST = timezone(timedelta(hours=8))

DEFAULTS: dict[str, Any] = {
    # 收件邮箱，可多个
    "recipients": [],
    # 发件人显示名
    "sender_name": "YouTube 每日摘要",
    # 采集时间窗口（小时）与宽限
    "lookback_hours": 24,
    "lookback_grace_minutes": 120,
    # 单次任务最多处理多少条
    "max_videos_per_day": 100,
    # 每封邮件包含多少条，超出分批
    "batch_size": 15,
    # 过滤过短的视频（秒）
    "min_duration_seconds": 120,
    # 字幕语言优先级
    "subtitle_langs": "zh-Hans,zh-Hant,zh,en",
    # 字幕文本上限（字符）
    "subtitle_max_chars": 60000,
    # Gemini 模型链（按顺序回退）
    "gemini_model": "gemini-4-hydrogen",
    "gemini_fallback_models": ["gemini-3-flash-preview", "gemini-3.5-flash-lite"],
    # 无新视频时是否仍发信
    "send_when_empty": False,
    # 去重记录保留天数
    "state_retention_days": 120,
    # 是否包含正在直播的视频
    "include_live_now": False,
    # 无字幕时是否本地语音转写（faster-whisper）
    "enable_transcribe": True,
    "transcribe_model": "small",
    "transcribe_language": "zh",
    "transcribe_max_minutes": 90,
    # 无字幕时优先让 Gemini 直接读视频（快，但 token 消耗大）
    "enable_video_summary": True,
    "video_summary_max_minutes": 120,
}

SECRET_KEYS = (
    "GEMINI_API_KEY",
    "GMAIL_USER",
    "GMAIL_APP_PASSWORD",
    "SMTP_HOST",
    "SMTP_PORT",
    "YOUTUBE_PROXY",
)


def _as_bool(value: str) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _as_list(value: str) -> list[str]:
    return [item.strip() for item in str(value).replace(";", ",").split(",") if item.strip()]


ENV_MAP: list[tuple[str, str, Any]] = [
    ("RECIPIENTS", "recipients", _as_list),
    ("SENDER_NAME", "sender_name", str),
    ("LOOKBACK_HOURS", "lookback_hours", int),
    ("LOOKBACK_GRACE_MINUTES", "lookback_grace_minutes", int),
    ("MAX_VIDEOS_PER_DAY", "max_videos_per_day", int),
    ("BATCH_SIZE", "batch_size", int),
    ("MIN_DURATION_SECONDS", "min_duration_seconds", int),
    ("SUBTITLE_LANGS", "subtitle_langs", str),
    ("SUBTITLE_MAX_CHARS", "subtitle_max_chars", int),
    ("GEMINI_MODEL", "gemini_model", str),
    ("GEMINI_FALLBACK_MODELS", "gemini_fallback_models", _as_list),
    ("SEND_WHEN_EMPTY", "send_when_empty", _as_bool),
    ("STATE_RETENTION_DAYS", "state_retention_days", int),
    ("INCLUDE_LIVE_NOW", "include_live_now", _as_bool),
    ("ENABLE_TRANSCRIBE", "enable_transcribe", _as_bool),
    ("TRANSCRIBE_MODEL", "transcribe_model", str),
    ("TRANSCRIBE_LANGUAGE", "transcribe_language", str),
    ("TRANSCRIBE_MAX_MINUTES", "transcribe_max_minutes", int),
    ("ENABLE_VIDEO_SUMMARY", "enable_video_summary", _as_bool),
    ("VIDEO_SUMMARY_MAX_MINUTES", "video_summary_max_minutes", int),
]


def ensure_dirs() -> None:
    for path in (BASE_DIR, STATE_DIR, LOG_DIR, WORK_DIR):
        path.mkdir(parents=True, exist_ok=True)


def load_config() -> dict[str, Any]:
    ensure_dirs()
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    for env_key, cfg_key, convert in ENV_MAP:
        raw = os.environ.get(env_key)
        if raw is None or raw == "":
            continue
        try:
            cfg[cfg_key] = convert(raw)
        except (TypeError, ValueError):
            continue
    return cfg


def load_env() -> dict[str, str]:
    """密钥来源：进程环境变量优先，其次 data/.env。"""
    values: dict[str, str] = {}
    if ENV_PATH.exists():
        for raw in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            values[key.strip()] = val.strip().strip('"').strip("'")
    for key in SECRET_KEYS:
        if os.environ.get(key):
            values[key] = os.environ[key]
    return values


def now_cst() -> datetime:
    return datetime.now(CST)


def today_label() -> str:
    return now_cst().strftime("%Y-%m-%d")
