"""容器内调度器：每天在指定时区的固定时刻执行一次摘要任务。

环境变量：
  TIMEZONE        默认 Asia/Shanghai
  SEND_HOUR       默认 12
  SEND_MINUTE     默认 0
  RUN_ON_START    默认 false（true 时容器启动先跑一次，便于验证）
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ytdigest.pipeline import run  # noqa: E402

logger = logging.getLogger("ytdigest.scheduler")


def _setup() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def main() -> int:
    _setup()
    tz_name = os.environ.get("TIMEZONE", "Asia/Shanghai")
    tz = ZoneInfo(tz_name)
    hour = int(os.environ.get("SEND_HOUR", "12"))
    minute = int(os.environ.get("SEND_MINUTE", "0"))
    run_on_start = os.environ.get("RUN_ON_START", "false").strip().lower() in (
        "1", "true", "yes", "on",
    )

    stop = {"flag": False}

    def _handle(signum, _frame):  # noqa: ANN001
        logger.info("收到退出信号 %s，准备停止", signum)
        stop["flag"] = True

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)

    logger.info(
        "调度已启动：每天 %02d:%02d（%s）执行；RUN_ON_START=%s",
        hour, minute, tz_name, run_on_start,
    )

    if run_on_start:
        logger.info("RUN_ON_START=true，先执行一次")
        try:
            run()
        except Exception as exc:  # noqa: BLE001
            logger.exception("首次执行失败: %s", exc)

    while not stop["flag"]:
        now = datetime.now(tz)
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        wait = (target - now).total_seconds()
        logger.info(
            "下一次执行：%s（约 %.1f 分钟后）",
            target.strftime("%Y-%m-%d %H:%M %Z"),
            wait / 60,
        )
        # 分段休眠，便于及时响应退出信号
        while wait > 0 and not stop["flag"]:
            chunk = min(wait, 60)
            time.sleep(chunk)
            wait -= chunk
        if stop["flag"]:
            break
        try:
            run()
        except Exception as exc:  # noqa: BLE001
            logger.exception("执行失败: %s", exc)

    logger.info("调度已停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
