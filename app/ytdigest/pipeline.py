"""每日摘要主流程：采集 → 去重 → 字幕 → Gemini 总结 → 发信。"""
from __future__ import annotations

import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import (
    LOG_DIR,
    WORK_DIR,
    CST,
    load_config,
    load_env,
    now_cst,
    today_label,
)
from .gemini import Gemini
from .mailer import Mailer
from .render import render_email, render_plain
from .rss import fetch_channel_rss
from .store import already_sent, load_quota, mark_sent, prune, save_quota
from .subtitles import fetch_subtitles, subtitle_available
from .transcribe import transcribe_video
from .youtube import TokenManager, YouTube, parse_duration_seconds, parse_rfc3339


def setup_logging() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("ytdigest")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    fh = logging.FileHandler(LOG_DIR / f"digest-{today_label()}.log", encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    return logger


def fmt_duration(seconds: int) -> str:
    if seconds <= 0:
        return "未知"
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h} 小时 {m} 分"
    if m:
        return f"{m} 分 {s} 秒"
    return f"{s} 秒"


def collect_candidates(
    yt: YouTube,
    subs: list[dict[str, Any]],
    window_start: datetime,
    logger: logging.Logger,
    channel_filter: str | None = None,
    fetch_limit: int = 12,
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """返回 (video_id -> 基本信息, channel_id -> 频道名)。window_start 由调用方统一决定。"""
    channel_titles = {s["channel_id"]: s["title"] for s in subs}
    if channel_filter:
        needle = channel_filter.lower()
        subs = [s for s in subs if needle in s["title"].lower() or needle in s["channel_id"]]

    logger.info("开始采集 %d 个频道（窗口起点 %s）", len(subs), window_start.astimezone(CST).strftime("%m-%d %H:%M"))

    uploads = yt.uploads_playlists([s["channel_id"] for s in subs])

    found: dict[str, dict[str, Any]] = {}

    def handle(sub: dict[str, Any]) -> list[dict[str, Any]]:
        cid = sub["channel_id"]
        entries: list[dict[str, Any]] = []
        pid = uploads.get(cid)
        if pid:
            try:
                entries.extend(yt.playlist_recent(pid, limit=fetch_limit))
            except Exception as exc:  # noqa: BLE001
                logger.warning("playlist 采集失败 %s: %s", sub["title"], exc)
        rss_entries = fetch_channel_rss(cid)
        entries.extend(rss_entries)
        return entries

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(handle, s): s for s in subs}
        for fut in as_completed(futures):
            sub = futures[fut]
            try:
                entries = fut.result()
            except Exception as exc:  # noqa: BLE001
                logger.warning("频道采集异常 %s: %s", sub["title"], exc)
                continue
            for e in entries:
                pub = parse_rfc3339(e.get("published", ""))
                if pub and pub < window_start:
                    continue
                vid = e["video_id"]
                prev = found.get(vid)
                if prev is None:
                    found[vid] = {
                        "video_id": vid,
                        "title": e.get("title", ""),
                        "published": e.get("published", ""),
                        "channel_id": e.get("channel_id") or sub["channel_id"],
                        "channel_title": e.get("channel_title") or sub["title"],
                    }
    logger.info("候选视频 %d 条（playlist + RSS 合并）", len(found))
    return found, channel_titles


def build_items(
    yt: YouTube,
    found: dict[str, dict[str, Any]],
    cfg: dict[str, Any],
    logger: logging.Logger,
    window_start: datetime,
    ignore_window: bool = False,
    force: bool = False,
) -> list[dict[str, Any]]:
    if not found:
        return []

    details = yt.videos_detail(list(found.keys()))
    items: list[dict[str, Any]] = []
    for vid, base in found.items():
        d = details.get(vid)
        if not d:
            logger.warning("videos.list 未返回 %s（可能已删除/私有）", vid)
            continue
        sn = d.get("snippet", {})
        cd = d.get("contentDetails", {})
        live = sn.get("liveBroadcastContent", "none")
        live_details = d.get("liveStreamingDetails", {}) or {}
        is_replay = bool(live_details.get("actualEndTime")) and live != "live"

        if live == "live" and not cfg.get("include_live_now"):
            continue
        if live == "upcoming":
            continue

        pub = parse_rfc3339(sn.get("publishedAt", "")) or parse_rfc3339(base["published"])
        if pub is None:
            continue
        if not ignore_window and pub < window_start:
            continue

        duration = parse_duration_seconds(cd.get("duration", ""))
        if duration and duration < cfg["min_duration_seconds"] and not is_replay:
            continue

        if not force and already_sent(vid):
            continue

        items.append(
            {
                "video_id": vid,
                "meta": {
                    "title": sn.get("title") or base["title"],
                    "channel": sn.get("channelTitle") or base["channel_title"],
                    "duration_text": fmt_duration(duration),
                    "duration_seconds": duration,
                    "published_text": (
                        pub.astimezone(CST).strftime("%Y-%m-%d %H:%M")
                        if pub
                        else ""
                    ),
                    "published": pub.isoformat() if pub else "",
                    "url": f"https://www.youtube.com/watch?v={vid}",
                    "description": sn.get("description", ""),
                },
                "live_replay": is_replay,
                "is_short": bool(duration and duration <= 60),
                "published_dt": pub,
            }
        )

    items.sort(key=lambda x: x["published_dt"])
    logger.info("窗口内待处理 %d 条", len(items))
    return items


def run(
    dry_run: bool = False,
    limit: int | None = None,
    channel: str | None = None,
    days: int | None = None,
    ignore_window: bool = False,
    force: bool = False,
    no_subtitles: bool = False,
) -> int:
    logger = setup_logging()
    cfg = load_config()
    env = load_env()
    # 把 .env 里的可选配置（如 YTDIGEST_YTDLP_PROXY）注入进程环境，供字幕模块读取
    for key, value in env.items():
        os.environ.setdefault(key, value)

    if not env.get("GEMINI_API_KEY"):
        logger.error("缺少 GEMINI_API_KEY（环境变量或 data/.env）")
        return 2

    started = now_cst()
    logger.info("=" * 62)
    logger.info("YouTube 每日摘要开始 | dry_run=%s limit=%s days=%s", dry_run, limit, days)

    tokens = TokenManager()
    yt = YouTube(tokens)
    subs = yt.list_subscriptions()
    logger.info("账号订阅频道 %d 个", len(subs))

    now = datetime.now(timezone.utc)
    if days:
        window_start = now - timedelta(days=days)
    else:
        window_start = now - timedelta(
            hours=cfg["lookback_hours"], minutes=cfg["lookback_grace_minutes"]
        )
    # 窗口越长，需要从每个频道多取一些条目（playlist 上限 50）
    fetch_limit = 25 if (days and days > 1) or cfg["lookback_hours"] > 24 else 12

    found, _ = collect_candidates(
        yt, subs, window_start, logger, channel, fetch_limit=fetch_limit
    )
    items = build_items(
        yt, found, cfg, logger, window_start, ignore_window=ignore_window, force=force
    )

    if limit:
        items = items[:limit]
    max_n = cfg["max_videos_per_day"]
    if len(items) > max_n:
        logger.warning("超出单日上限 %d 条，截断为 %d 条", max_n, max_n)
        items = items[:max_n]

    if not items:
        logger.info("没有需要处理的新视频")
        if cfg.get("send_when_empty") and not dry_run:
            html = render_email([], today_label(), started)
            plain = render_plain([], today_label())
            _send(env, cfg, html, plain, 0, logger, dry_run)
        return 0

    gemini_models = [cfg["gemini_model"]] + [
        m for m in cfg.get("gemini_fallback_models", []) if m and m != cfg["gemini_model"]
    ]
    quota_disabled = load_quota()
    gemini = Gemini(
        env["GEMINI_API_KEY"],
        gemini_models,
        disabled=quota_disabled,
        video_interval=cfg.get("video_request_interval_seconds", 60),
    )
    if quota_disabled:
        logger.info("今日已耗尽额度的模型（直接跳过）: %s", ", ".join(sorted(quota_disabled)))
    logger.info("Gemini 模型链: %s", " -> ".join(gemini_models))
    have_ytdlp = subtitle_available() and not no_subtitles
    if not have_ytdlp:
        logger.warning("yt-dlp 不可用或已禁用，将直接使用标题+简介")

    processed: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    missing_subs = 0
    try:
        for idx, item in enumerate(items, 1):
            meta = item["meta"]
            logger.info("[%d/%d] 处理 %s | %s", idx, len(items), meta["channel"], meta["title"][:60])
            subtitle = None
            transcribed = False
            sub_status = "disabled"
            if have_ytdlp:
                subtitle, sub_status = fetch_subtitles(item["video_id"], cfg["subtitle_langs"])
            if subtitle:
                logger.info("    字幕 %d 字", len(subtitle))

            # 无字幕：优先让 Gemini 直接看视频（快，省去下载与本地转写）
            if not subtitle and sub_status == "none" and cfg.get("enable_video_summary", True):
                limit_sec = int(cfg.get("video_summary_max_minutes", 120)) * 60
                if meta["duration_seconds"] <= limit_sec:
                    logger.info("    确认无字幕，尝试 Gemini 视频直读…")
                    try:
                        summary_html = gemini.summarize_video(
                            meta, meta["url"], processing=cfg.get("video_processing", "static")
                        )
                        processed.append(
                            {**item, "summary_html": summary_html, "video_read": True}
                        )
                        continue
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("    视频直读失败：%s，改用 whisper 兜底", exc)
                else:
                    logger.info("    视频较长（%s），跳过直读", meta["duration_text"])

            # whisper 兜底
            if not subtitle and sub_status == "none" and cfg.get("enable_transcribe"):
                logger.info("    启动本地语音转写（%s）…", cfg.get("transcribe_model", "small"))
                try:
                    subtitle = transcribe_video(
                        item["video_id"],
                        model_size=cfg.get("transcribe_model", "small"),
                        language=cfg.get("transcribe_language", "zh"),
                        max_minutes=cfg.get("transcribe_max_minutes", 90),
                        max_chars=cfg.get("subtitle_max_chars", 60000),
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("    转写失败: %s", exc)
                    subtitle = None
                if subtitle:
                    transcribed = True
                    logger.info("    转写完成 %d 字", len(subtitle))
                else:
                    missing_subs += 1
                    logger.info("    转写未产出，改用标题+简介")
            elif not subtitle:
                missing_subs += 1
                if sub_status == "error":
                    logger.info("    字幕抓取失败，改用标题+简介")
                elif sub_status != "disabled":
                    logger.info("    无字幕，改用标题+简介")

            try:
                summary_html = gemini.summarize(meta, subtitle, cfg["subtitle_max_chars"])
            except Exception as exc:  # noqa: BLE001
                # 不生成占位、不标记已发送，留待下一次重试，确保不漏
                logger.error("    总结失败，保留待下次重试: %s", exc)
                failed.append(item)
                continue

            processed.append({**item, "summary_html": summary_html, "transcribed": transcribed})
            try:
                WORK_DIR.mkdir(parents=True, exist_ok=True)
                (WORK_DIR / f"{today_label()}-{item['video_id']}.html").write_text(
                    summary_html, encoding="utf-8"
                )
            except Exception:
                pass
    finally:
        # 无论成功失败都记录今日已耗尽的模型，重跑时不再白发请求
        save_quota(gemini.disabled_models())

    if not processed:
        logger.error("全部 %d 条总结失败，本次不发信，已保留待重试", len(failed))
        return 1

    if failed:
        logger.warning(
            "有 %d 条总结失败，已保留待下次重试: %s",
            len(failed),
            "; ".join(i["meta"]["title"][:30] for i in failed),
        )

    batch_size = max(1, int(cfg.get("batch_size", 15)))
    batches = [processed[i : i + batch_size] for i in range(0, len(processed), batch_size)]

    if dry_run:
        html = render_email(processed, today_label(), started, missing_subs)
        out = WORK_DIR / f"preview-{today_label()}.html"
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        out.write_text(html, encoding="utf-8")
        logger.info(
            "[dry-run] 预览已写入 %s（%d 条 / %d 批），未发信、未记录去重",
            out, len(processed), len(batches),
        )
        for item in processed:
            logger.info("    ✔ %s | %s", item["meta"]["channel"], item["meta"]["title"][:60])
        return 0

    for b_idx, batch in enumerate(batches, 1):
        html = render_email(batch, today_label(), started, missing_subs)
        plain = render_plain(batch, today_label())
        suffix = f"（{b_idx}/{len(batches)}）" if len(batches) > 1 else ""
        _send(env, cfg, html, plain, len(batch), logger, dry_run, suffix)
        for item in batch:
            mark_sent(item["video_id"], item["meta"])
        logger.info("第 %d/%d 批已发送并记录去重 %d 条", b_idx, len(batches), len(batch))

    logger.info("已记录去重 %d 条", len(processed))

    removed = prune(cfg["state_retention_days"])
    if removed:
        logger.info("清理过期去重记录 %d 条", removed)

    logger.info("完成，用时 %.1f 秒", (now_cst() - started).total_seconds())
    return 0


def _send(env, cfg, html, plain, count, logger, dry_run, suffix: str = "") -> None:
    recipients = cfg.get("recipients") or []
    if not recipients:
        raise RuntimeError("config.json 未配置 recipients")
    mailer = Mailer(
        env["GMAIL_USER"],
        env["GMAIL_APP_PASSWORD"],
        cfg.get("sender_name", "YouTube 每日摘要"),
        env.get("SMTP_HOST", "smtp.gmail.com"),
        int(env.get("SMTP_PORT", "587")),
    )
    subject = f"YouTube 每日摘要 {today_label()}（{count} 条新视频）{suffix}".strip()
    mailer.send(recipients, subject, html, plain)
    logger.info("邮件已发送至 %s%s", ", ".join(recipients), suffix and f" {suffix}")
