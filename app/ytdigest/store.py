"""已发送视频的持久去重记录。"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import SENT_PATH, STATE_DIR, CST

QUOTA_PATH = STATE_DIR / "quota.json"


def load_quota() -> set[str]:
    """读取当天已确认额度耗尽/不可用的模型，避免同一天重复白发请求。"""
    if not QUOTA_PATH.exists():
        return set()
    try:
        data = json.loads(QUOTA_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return set()
    today = datetime.now(CST).strftime("%Y-%m-%d")
    if data.get("date") != today:
        return set()
    return set(data.get("disabled", []))


def save_quota(disabled: set[str]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "date": datetime.now(CST).strftime("%Y-%m-%d"),
        "disabled": sorted(disabled),
    }
    QUOTA_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def _load() -> dict[str, Any]:
    if not SENT_PATH.exists():
        return {"sent": {}}
    try:
        data = json.loads(SENT_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        # 文件损坏时保底：备份后重建，避免永久阻塞
        backup = SENT_PATH.with_suffix(".corrupt.json")
        SENT_PATH.replace(backup)
        return {"sent": {}}
    if not isinstance(data.get("sent"), dict):
        return {"sent": {}}
    return data


def _atomic_write(data: dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(STATE_DIR), prefix=".sent-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, SENT_PATH)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def already_sent(video_id: str) -> bool:
    return video_id in _load()["sent"]


def mark_sent(video_id: str, meta: dict[str, Any]) -> None:
    data = _load()
    data["sent"][video_id] = {
        "at": datetime.now(CST).isoformat(timespec="seconds"),
        **{k: meta.get(k) for k in ("channel", "title", "published") if meta.get(k)},
    }
    _atomic_write(data)


def prune(retention_days: int) -> int:
    """清理超过保留期的记录，返回删除条数。"""
    data = _load()
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    kept: dict[str, Any] = {}
    removed = 0
    for vid, info in data["sent"].items():
        at = info.get("at")
        try:
            ts = datetime.fromisoformat(at) if at else None
        except ValueError:
            ts = None
        if ts and ts.tzinfo is None:
            ts = ts.replace(tzinfo=CST)
        if ts and ts < cutoff:
            removed += 1
            continue
        kept[vid] = info
    if removed:
        data["sent"] = kept
        _atomic_write(data)
    return removed
