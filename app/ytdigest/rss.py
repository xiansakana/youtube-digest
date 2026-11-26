"""频道 RSS 兜底采集。

YouTube RSS（feeds/videos.xml?channel_id=...）无需 API 配额，返回最近 15 条，
包括直播回放，可以补上 uploads playlist 偶发漏掉的条目。
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any

import requests

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
}

RSS_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={cid}"


def fetch_channel_rss(channel_id: str, timeout: int = 20) -> list[dict[str, Any]]:
    try:
        resp = requests.get(RSS_URL.format(cid=channel_id), timeout=timeout,
                            headers={"User-Agent": "Mozilla/5.0 ytdigest/1.0"})
        if resp.status_code != 200:
            return []
        root = ET.fromstring(resp.content)
    except Exception:
        return []

    entries: list[dict[str, Any]] = []
    for entry in root.findall("atom:entry", NS):
        vid = entry.findtext("yt:videoId", default="", namespaces=NS)
        if not vid:
            continue
        title = entry.findtext("atom:title", default="", namespaces=NS)
        published = entry.findtext("atom:published", default="", namespaces=NS)
        channel_title = entry.findtext("atom:author/atom:name", default="", namespaces=NS)
        entries.append(
            {
                "video_id": vid,
                "title": title,
                "published": published,
                "channel_id": channel_id,
                "channel_title": channel_title,
            }
        )
    return entries
