"""HTML 邮件渲染。"""
from __future__ import annotations

import html as html_mod
import re
from datetime import datetime
from typing import Any

STYLE = """
<style>
  body{margin:0;padding:0;background:#eef1f5;
       font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
       color:#1f2937;-webkit-font-smoothing:antialiased}
  .wrap{max-width:780px;margin:0 auto;padding:26px 16px 40px}
  .hero{background:linear-gradient(135deg,#1e3a8a,#2563eb);color:#fff;border-radius:14px;
        padding:22px 24px;margin-bottom:20px}
  .hero h1{margin:0 0 6px;font-size:20px;font-weight:700;letter-spacing:.3px}
  .hero .hint{font-size:13px;opacity:.85}
  .stats{display:flex;flex-wrap:wrap;gap:10px;margin-top:14px}
  .stat{background:rgba(255,255,255,.16);border-radius:8px;padding:6px 12px;font-size:12px}
  .card{background:#fff;border-radius:12px;padding:22px 24px;margin-bottom:18px;
        box-shadow:0 1px 4px rgba(15,23,42,.08);border:1px solid #e5e9f0}
  .vg-meta{border-left:4px solid #2563eb;padding:2px 0 2px 12px;margin-bottom:12px}
  .vg-title{font-size:17px;font-weight:700;line-height:1.45}
  .vg-title a{color:#1d4ed8;text-decoration:none}
  .vg-sub{font-size:12.5px;color:#64748b;margin-top:5px}
  .vg-tldr{background:#f1f6ff;border:1px solid #dbe7ff;border-radius:8px;
           padding:10px 12px;font-size:14px;line-height:1.7;margin:0 0 6px}
  h3{font-size:15px;margin:20px 0 8px;padding-bottom:5px;border-bottom:2px solid #e8edf5;color:#0f172a}
  ol,ul{margin:8px 0 14px;padding-left:22px}
  li{font-size:13.8px;line-height:1.78;margin-bottom:6px}
  table{width:100%;border-collapse:collapse;font-size:13px;margin:8px 0 14px;
        border:1px solid #d8dee6}
  th{background:#f0f3f7;text-align:left;font-weight:600;color:#334155}
  th,td{border:1px solid #d8dee6;padding:7px 9px;vertical-align:top;line-height:1.65}
  td{background:#fff}
  .footer{text-align:center;color:#94a3b8;font-size:12px;line-height:1.9;margin-top:10px}
  .tag{display:inline-block;background:#eef2ff;color:#4338ca;border-radius:999px;
       padding:1px 9px;font-size:11.5px;margin-right:6px}
  .empty{background:#fff;border-radius:12px;padding:34px 24px;text-align:center;color:#64748b;
         border:1px solid #e5e9f0}
</style>
"""

_TABLE_STYLE = 'border="1" cellspacing="0" cellpadding="6" style="width:100%;border-collapse:collapse;font-size:13px;margin:8px 0 14px;border:1px solid #d8dee6"'


def _enhance(fragment: str) -> str:
    """给 LLM 输出的表格/标题补内联样式，提升邮件客户端兼容性。"""
    fragment = re.sub(r"<table(?![^>]*style=)[^>]*>", f"<table {_TABLE_STYLE}>", fragment)
    fragment = re.sub(
        r"<th(?![^>]*style=)([^>]*)>",
        r'<th\1 style="background:#f0f3f7;text-align:left;font-weight:600;border:1px solid #d8dee6;padding:7px 9px">',
        fragment,
    )
    fragment = re.sub(
        r"<td(?![^>]*style=)([^>]*)>",
        r'<td\1 style="border:1px solid #d8dee6;padding:7px 9px;vertical-align:top">',
        fragment,
    )
    fragment = re.sub(
        r"<h3(?![^>]*style=)([^>]*)>",
        r'<h3\1 style="font-size:15px;margin:20px 0 8px;padding-bottom:5px;border-bottom:2px solid #e8edf5;color:#0f172a">',
        fragment,
    )
    return fragment


def render_email(
    items: list[dict[str, Any]],
    date_label: str,
    generated_at: datetime,
    subtitle_missing: int = 0,
) -> str:
    blocks: list[str] = []
    for idx, item in enumerate(items, 1):
        body = _enhance(item["summary_html"])
        tag = ""
        if item.get("live_replay"):
            tag = '<span class="tag">直播回放</span>'
        elif item.get("is_short"):
            tag = '<span class="tag">短视频</span>'
        if item.get("video_read"):
            tag += '<span class="tag">视频直读</span>'
        blocks.append(
            f'<div class="card">{tag}{body}</div>'
        )

    if not items:
        body_html = (
            '<div class="empty"><div style="font-size:36px;margin-bottom:8px">☕</div>'
            "过去 24 小时订阅频道没有新视频，今天休息一下。</div>"
        )
    else:
        body_html = "\n".join(blocks)

    stats = f'<div class="stats"><span class="stat">新视频 {len(items)} 条</span>'
    if subtitle_missing:
        stats += f'<span class="stat">无字幕 {subtitle_missing} 条</span>'
    stats += f'<span class="stat">生成于 {generated_at.strftime("%H:%M")}</span></div>'

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>YouTube 每日摘要 {date_label}</title>{STYLE}</head>
<body>
<div class="wrap">
  <div class="hero">
    <h1>YouTube 订阅每日摘要</h1>
    <div class="hint">{html_mod.escape(date_label)} · 过去 24 小时更新</div>
    {stats}
  </div>
  {body_html}
  <div class="footer">
    由 YouTube Data API + yt-dlp + Gemini 自动生成<br>
    如摘要与视频原意有出入，请以原视频为准 · {html_mod.escape(date_label)}
  </div>
</div>
</body></html>"""


def render_plain(items: list[dict[str, Any]], date_label: str) -> str:
    lines = [f"YouTube 订阅每日摘要 {date_label}", "=" * 40]
    if not items:
        lines.append("过去 24 小时订阅频道没有新视频。")
    for idx, item in enumerate(items, 1):
        meta = item.get("meta", {})
        lines.append(f"\n[{idx}] {meta.get('title','')}")
        lines.append(f"频道：{meta.get('channel','')} 时长：{meta.get('duration_text','')} 发布：{meta.get('published_text','')}")
        lines.append(f"链接：{meta.get('url','')}")
        lines.append("（详细结构化摘要请查看 HTML 版本）")
    return "\n".join(lines)
