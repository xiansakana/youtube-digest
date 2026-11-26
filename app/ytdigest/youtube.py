"""YouTube Data API v3 客户端（含 refresh token 自动续期）。

只使用 requests 直接调 REST，避免在服务器上安装整套 Google SDK。
"""
from __future__ import annotations

import email.utils
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

from .config import TOKEN_PATH

API = "https://www.googleapis.com/youtube/v3"
TOKEN_URI = "https://oauth2.googleapis.com/token"


class TokenManager:
    """读取 token.json，按需刷新 access token，并写回文件。"""

    def __init__(self, path: Path = TOKEN_PATH) -> None:
        self.path = path
        self._data: dict[str, Any] | None = None
        self._access_token: str | None = None
        self._expiry: datetime | None = None

    def _load(self) -> dict[str, Any]:
        if self._data is None:
            if not self.path.exists():
                raise RuntimeError(
                    f"未找到 token 文件 {self.path}，请先在本机完成授权并同步。"
                )
            self._data = json.loads(self.path.read_text(encoding="utf-8"))
        return self._data

    def _parse_expiry(self) -> datetime | None:
        data = self._load()
        raw = data.get("expiry")
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    def access_token(self, force: bool = False) -> str:
        data = self._load()
        if self._expiry is None:
            self._expiry = self._parse_expiry()
        if (
            not force
            and self._access_token
            and self._expiry
            and self._expiry - datetime.now(timezone.utc) > timedelta(minutes=2)
        ):
            return self._access_token

        # 若 token.json 里已有可用 access token（本机刚授权后同步上来），先直接用
        raw_access = data.get("token") or data.get("access_token")
        if not force and raw_access and self._expiry and self._expiry > datetime.now(timezone.utc):
            self._access_token = raw_access
            return raw_access

        refresh_token = data.get("refresh_token")
        client_id = data.get("client_id")
        client_secret = data.get("client_secret")
        if not refresh_token:
            raise RuntimeError("token.json 缺少 refresh_token，请重新授权。")
        if not client_id or not client_secret:
            raise RuntimeError("token.json 缺少 client_id/client_secret。")

        resp = requests.post(
            data.get("token_uri") or TOKEN_URI,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=30,
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"刷新 access token 失败 HTTP {resp.status_code}: {resp.text[:400]}"
            )
        payload = resp.json()
        self._access_token = payload["access_token"]
        expires_in = int(payload.get("expires_in", 3600))
        self._expiry = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

        # 回写文件，方便排查
        data["token"] = self._access_token
        data["expiry"] = self._expiry.replace(microsecond=0).isoformat().replace("+00:00", "Z")
        if payload.get("refresh_token"):
            data["refresh_token"] = payload["refresh_token"]
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)
        return self._access_token


class YouTube:
    def __init__(self, tokens: TokenManager) -> None:
        self.tokens = tokens
        self._session = requests.Session()

    def _get(self, path: str, params: dict[str, Any], retries: int = 3) -> dict[str, Any]:
        url = f"{API}/{path}"
        last_err = ""
        for attempt in range(retries):
            token = self.tokens.access_token(force=attempt > 0)
            headers = {"Authorization": f"Bearer {token}"}
            resp = self._session.get(url, params=params, headers=headers, timeout=45)
            if resp.status_code == 200:
                return resp.json()
            last_err = f"HTTP {resp.status_code}: {resp.text[:400]}"
            if resp.status_code in (401, 403) and attempt + 1 < retries:
                continue
            if resp.status_code >= 500 and attempt + 1 < retries:
                continue
            raise RuntimeError(f"YouTube API {path} 失败 {last_err}")
        raise RuntimeError(f"YouTube API {path} 重试耗尽 {last_err}")

    # ---------- subscriptions ----------
    def list_subscriptions(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        page = None
        while True:
            params = {"part": "snippet", "mine": "true", "maxResults": 50}
            if page:
                params["pageToken"] = page
            data = self._get("subscriptions", params)
            for item in data.get("items", []):
                sn = item["snippet"]
                out.append(
                    {
                        "channel_id": sn["resourceId"]["channelId"],
                        "title": sn.get("title", ""),
                    }
                )
            page = data.get("nextPageToken")
            if not page:
                break
        return out

    # ---------- channels -> uploads playlist ----------
    def uploads_playlists(self, channel_ids: list[str]) -> dict[str, str]:
        result: dict[str, str] = {}
        for i in range(0, len(channel_ids), 50):
            chunk = channel_ids[i : i + 50]
            data = self._get(
                "channels",
                {"part": "contentDetails", "id": ",".join(chunk), "maxResults": 50},
            )
            for item in data.get("items", []):
                try:
                    result[item["id"]] = item["contentDetails"]["relatedPlaylists"]["uploads"]
                except KeyError:
                    continue
        return result

    # ---------- uploads playlist items ----------
    def playlist_recent(self, playlist_id: str, limit: int = 10) -> list[dict[str, Any]]:
        params = {
            "part": "snippet,contentDetails",
            "playlistId": playlist_id,
            "maxResults": max(1, min(limit, 50)),
        }
        data = self._get("playlistItems", params)
        out = []
        for item in data.get("items", []):
            sn = item.get("snippet", {})
            cd = item.get("contentDetails", {})
            vid = cd.get("videoId") or sn.get("resourceId", {}).get("videoId")
            if not vid:
                continue
            out.append(
                {
                    "video_id": vid,
                    "title": sn.get("title", ""),
                    "published": cd.get("videoPublishedAt") or sn.get("publishedAt") or "",
                    "channel_id": sn.get("channelId", ""),
                    "channel_title": sn.get("channelTitle", ""),
                }
            )
        return out

    # ---------- videos detail ----------
    def videos_detail(self, video_ids: list[str]) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for i in range(0, len(video_ids), 50):
            chunk = video_ids[i : i + 50]
            data = self._get(
                "videos",
                {
                    "part": "snippet,contentDetails,statistics,liveStreamingDetails",
                    "id": ",".join(chunk),
                    "maxResults": 50,
                },
            )
            for item in data.get("items", []):
                result[item["id"]] = item
        return result


def parse_duration_seconds(iso: str) -> int:
    """把 PT1H2M3S 转成秒。"""
    if not iso or not iso.startswith("P"):
        return 0
    import re

    m = re.match(
        r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$", iso
    )
    if not m:
        return 0
    d, h, mi, s = (int(x) if x else 0 for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def parse_rfc3339(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = email.utils.parsedate_to_datetime(value)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            return None
