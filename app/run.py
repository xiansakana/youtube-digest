#!/usr/bin/env python3
"""ytdigest 入口。

常用：
  python run.py --dry-run --days 3 --limit 3     # 本地试跑，只写预览不发信
  python run.py                                   # 正常每日任务
  python run.py --check                           # 检查 token / API / Gemini / SMTP
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ytdigest.config import load_config, load_env  # noqa: E402
from ytdigest.pipeline import run  # noqa: E402


def do_check() -> int:
    env = load_env()
    cfg = load_config()
    ok = True
    print("== 配置检查 ==")
    print(f"recipients: {cfg.get('recipients')}")
    print(f"gemini_model: {cfg.get('gemini_model')}")
    for key in ("GEMINI_API_KEY", "GMAIL_USER", "GMAIL_APP_PASSWORD"):
        has = bool(env.get(key))
        print(f"{key}: {'已配置' if has else '缺失'}")
        ok = ok and has

    print("\n== YouTube token ==")
    try:
        from ytdigest.youtube import TokenManager, YouTube

        yt = YouTube(TokenManager())
        subs = yt.list_subscriptions()
        print(f"token 可用，订阅 {len(subs)} 个频道")
        for s in subs[:10]:
            print(f"  - {s['title']} ({s['channel_id']})")
        if len(subs) > 10:
            print(f"  ... 共 {len(subs)} 个")
    except Exception as exc:  # noqa: BLE001
        print(f"token 检查失败: {exc}")
        ok = False

    print("\n== 依赖 ==")
    try:
        from ytdigest.subtitles import subtitle_available

        print(f"yt-dlp: {'可用' if subtitle_available() else '不可用'}")
    except Exception as exc:  # noqa: BLE001
        print(f"yt-dlp 检查失败: {exc}")

    print("\n== SMTP ==")
    try:
        from ytdigest.mailer import Mailer

        Mailer(
            env.get("GMAIL_USER", ""),
            env.get("GMAIL_APP_PASSWORD", ""),
            cfg.get("sender_name", ""),
            env.get("SMTP_HOST", "smtp.gmail.com"),
            int(env.get("SMTP_PORT", "587")),
        ).verify()
        print("SMTP 登录成功")
    except Exception as exc:  # noqa: BLE001
        print(f"SMTP 检查失败: {exc}")
        ok = False

    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="YouTube 订阅每日摘要")
    parser.add_argument("--dry-run", action="store_true", help="不发信、不写去重，只生成预览")
    parser.add_argument("--limit", type=int, default=None, help="最多处理多少条")
    parser.add_argument("--channel", default=None, help="只处理标题或 ID 匹配的频道")
    parser.add_argument("--days", type=int, default=None, help="回溯天数（覆盖默认 24h 窗口）")
    parser.add_argument("--ignore-window", action="store_true", help="忽略时间窗口")
    parser.add_argument("--force", action="store_true", help="忽略去重记录，重新处理")
    parser.add_argument("--no-subtitles", action="store_true", help="跳过字幕抓取")
    parser.add_argument("--check", action="store_true", help="检查配置与各依赖连通性")
    args = parser.parse_args()

    if args.check:
        return do_check()

    return run(
        dry_run=args.dry_run,
        limit=args.limit,
        channel=args.channel,
        days=args.days,
        ignore_window=args.ignore_window,
        force=args.force,
        no_subtitles=args.no_subtitles,
    )


if __name__ == "__main__":
    raise SystemExit(main())
