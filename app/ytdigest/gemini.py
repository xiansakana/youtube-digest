"""Gemini 中文深度总结。

直接走 REST，不引入 SDK；输出严格约束为 HTML 片段，供邮件模板拼装。
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Any

import requests

ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

logger = logging.getLogger("ytdigest")


class GeminiError(RuntimeError):
    """带 HTTP 状态码的 Gemini 调用错误，便于上层按额度/负载分类处理。"""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"Gemini HTTP {status}: {message[:400]}")
        self.status = status

SYSTEM_PROMPT = """你是一名资深中文财经与科技内容分析师，长期跟踪美股、港股、A 股、宏观与产业趋势。
你的任务是把一条视频（可能是投资实盘、行业分析、访谈或科技解读）整理成结构化的中文深度摘要。

硬性要求：
1. 只输出 HTML 片段，不要 <html>、<head>、<body> 标签，不要 Markdown 代码围栏，不要任何解释性开场白。
2. 所有样式使用语义标签即可，表格必须带 border="1" cellspacing="0" cellpadding="6"，不要写 style 属性。
3. 必须保留视频中出现的具体数字、点位、百分比、时间戳、股票代码、人名；不要编造视频里没有的数据。
4. 如果视频没有实盘操作内容，第二节表格中写明"本期无明确实盘操作"，但仍需给出视频提到的关键价位或数据。
5. 全文使用简体中文。

输出结构必须严格如下（顺序不可变）：

<div class="vg-meta">
  <div class="vg-title"><a href="{VIDEO_URL}">{标题}</a></div>
  <div class="vg-sub">频道：{频道名} ｜ 时长：{时长} ｜ 发布日期：{发布日期}</div>
</div>
<p class="vg-tldr"><strong>一句话总结：</strong>……</p>

<h3>一、核心观点</h3>
<ol>
  <li><strong>结论</strong>：数据与逻辑展开……</li>
  <li>……（共 3–5 条，每条以加粗结论开头）</li>
</ol>

<h3>二、实盘操作与关键点位</h3>
<table border="1" cellspacing="0" cellpadding="6">
  <thead><tr><th>类别</th><th>标的/项目</th><th>方向或数值</th><th>说明</th></tr></thead>
  <tbody>
    <tr><td>买入</td><td>……</td><td>……</td><td>……</td></tr>
  </tbody>
</table>

<h3>三、对未来市场/主题的判断</h3>
<table border="1" cellspacing="0" cellpadding="6">
  <thead><tr><th>时间维度</th><th>判断</th><th>依据</th></tr></thead>
  <tbody>
    <tr><td>短期（1–4 周）</td><td>……</td><td>……</td></tr>
    <tr><td>中期（1–2 季度）</td><td>……</td><td>……</td></tr>
    <tr><td>长期（1 年以上）</td><td>……</td><td>……</td></tr>
  </tbody>
</table>

<h3>四、重点板块/个股/嘉宾观点讨论</h3>
<ul>
  <li>……</li>
</ul>

<h3>五、客观评价与风险提示</h3>
<ul>
  <li><strong>思考框架：</strong>……</li>
  <li><strong>口径提醒：</strong>……</li>
  <li><strong>最终结论：</strong>……</li>
</ul>
"""

USER_TEMPLATE = """请按系统要求总结以下视频。

标题：{title}
频道：{channel}
时长：{duration}
发布日期：{published}
视频链接：{url}
视频简介：
{description}

字幕/转写文本（可能来自自动字幕，存在识别误差，请结合上下文理解）：
{subtitle}
"""


VIDEO_TEMPLATE = """请观看以下 YouTube 视频并按要求总结。

标题：{title}
频道：{channel}
时长：{duration}
发布日期：{published}
视频链接：{url}

要求：直接基于视频的画面与语音内容总结，保留具体数字、点位、百分比与时间戳；无法确认的信息请如实说明，不要编造。
"""


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


class Gemini:
    def __init__(
        self,
        api_key: str,
        models: list[str] | str = "gemini-3.5-flash-lite",
        disabled: set[str] | None = None,
        video_interval: float = 60.0,
    ) -> None:
        self.api_key = api_key
        if isinstance(models, str):
            models = [models]
        # 去重保序
        self.models: list[str] = []
        for m in models:
            if m and m not in self.models:
                self.models.append(m)
        self._fail_count: dict[str, int] = {m: 0 for m in self.models}
        # 当天已确认额度耗尽的模型，启动时就跳过，不再白发请求
        self._disabled: set[str] = set(disabled or ())
        # 视频直读请求的串行节流：两次请求之间至少间隔 video_interval 秒
        self._video_interval = max(0.0, float(video_interval))
        self._video_last = 0.0
        self._video_lock = threading.Lock()

    def _wait_video_slot(self) -> None:
        if self._video_interval <= 0:
            return
        with self._video_lock:
            now = time.monotonic()
            gap = self._video_interval - (now - self._video_last)
            if gap > 0:
                logger.info(
                    "    视频请求节流：等待 %.0f 秒（最小间隔 %s 秒）",
                    gap, int(self._video_interval),
                )
                time.sleep(gap)
            self._video_last = time.monotonic()

    def disabled_models(self) -> set[str]:
        return set(self._disabled)

    def _call(self, model: str, prompt: str, max_tokens: int = 8192) -> str:
        url = ENDPOINT.format(model=model)
        body = {
            "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.4,
                "topP": 0.9,
                "maxOutputTokens": max_tokens,
            },
        }
        resp = requests.post(
            url,
            params={"key": self.api_key},
            json=body,
            timeout=90,
            headers={"Content-Type": "application/json"},
        )
        if resp.status_code != 200:
            raise GeminiError(resp.status_code, resp.text)
        data = resp.json()
        candidates = data.get("candidates") or []
        if not candidates:
            raise GeminiError(0, f"无候选结果: {json.dumps(data)[:400]}")
        parts = candidates[0].get("content", {}).get("parts", [])
        # 过滤 thinking 片段（2.5 系列可能返回 thought=true）
        texts = [
            p.get("text", "")
            for p in parts
            if isinstance(p, dict) and not p.get("thought") and p.get("text")
        ]
        text = "\n".join(texts).strip()
        if not text:
            raise RuntimeError("Gemini 返回空内容")
        return _strip_fences(text)

    def _call_video(self, model: str, prompt: str, video_url: str,
                    max_tokens: int = 8192, agentic: bool = False) -> str:
        """把 YouTube 链接直接作为视频输入，让模型观看后总结。

        agentic=True 时启用官方 agentic 处理（media_processing=AGENTIC），
        需要同时提供 mime_type；免费层常不可用，调用方会降级为 static。
        """
        url = ENDPOINT.format(model=model)
        part: dict[str, Any] = {"file_data": {"file_uri": video_url}}
        if agentic:
            part["file_data"]["mime_type"] = "video/mp4"
            part["media_processing"] = "AGENTIC"
        body = {
            "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": [{"role": "user", "parts": [
                part,
                {"text": prompt},
            ]}],
            "generationConfig": {
                "temperature": 0.4,
                "topP": 0.9,
                "maxOutputTokens": max_tokens,
            },
        }
        resp = requests.post(
            url,
            params={"key": self.api_key},
            json=body,
            timeout=180,
            headers={"Content-Type": "application/json"},
        )
        if resp.status_code != 200:
            raise GeminiError(resp.status_code, resp.text)
        data = resp.json()
        candidates = data.get("candidates") or []
        if not candidates:
            raise GeminiError(0, f"无候选结果: {json.dumps(data)[:400]}")
        parts = candidates[0].get("content", {}).get("parts", [])
        texts = [
            p.get("text", "")
            for p in parts
            if isinstance(p, dict) and not p.get("thought") and p.get("text")
        ]
        text = "\n".join(texts).strip()
        if not text:
            raise GeminiError(0, "视频摘要返回空内容")
        return _strip_fences(text)

    def summarize_video(self, meta: dict[str, Any], video_url: str,
                        processing: str = "static") -> str:
        """无字幕视频优先走这里：模型直接看视频。

        processing="agentic" 时先试官方 agentic 处理，失败自动降级 static。
        """
        if str(processing).lower() == "agentic":
            try:
                return self._summarize_video_once(meta, video_url, agentic=True)
            except Exception as exc:  # noqa: BLE001
                logger.warning("    agentic 模式失败（%s），降级为 static 视频直读", exc)
        return self._summarize_video_once(meta, video_url, agentic=False)

    def _summarize_video_once(self, meta: dict[str, Any], video_url: str,
                              agentic: bool) -> str:
        prompt = VIDEO_TEMPLATE.format(
            title=meta.get("title", ""),
            channel=meta.get("channel", ""),
            duration=meta.get("duration_text", ""),
            published=meta.get("published_text", ""),
            url=video_url,
        )
        last_err: Exception | None = None
        for model in self.models:
            if model in self._disabled:
                continue
            try:
                self._wait_video_slot()
                result = self._call_video(model, prompt, video_url, agentic=agentic)
                logger.info(
                    "    Gemini 视频直读（%s）使用 %s（%d 字）",
                    "agentic" if agentic else "static", model, len(result),
                )
                return result
            except GeminiError as exc:
                last_err = exc
                if exc.status in (429, 402, 404):
                    self._disabled.add(model)
                    logger.warning(
                        "    %s 额度/权限不可用（HTTP %s），今日跳过", model, exc.status
                    )
                    continue
                if exc.status == 503:
                    self._fail_count[model] += 1
                    if self._fail_count[model] >= 2:
                        self._disabled.add(model)
                        logger.warning("    %s 连续 503，今日跳过", model)
                    else:
                        logger.warning("    %s 503，切换下一个模型", model)
                    continue
                raise
        raise RuntimeError(f"视频直读失败（已尝试 {self.models}）: {last_err}")

    def summarize(self, meta: dict[str, Any], subtitle: str | None,
                  max_chars: int = 60000) -> str:
        subtitle_text = (subtitle or "").strip()
        has_subtitle = bool(subtitle_text)
        if not has_subtitle:
            subtitle_text = "（该视频无法获取字幕，请仅依据标题与简介进行谨慎总结，并在第一节开头说明信息有限。）"
        else:
            subtitle_text = subtitle_text[:max_chars]

        prompt = USER_TEMPLATE.format(
            title=meta.get("title", ""),
            channel=meta.get("channel", ""),
            duration=meta.get("duration_text", ""),
            published=meta.get("published_text", ""),
            url=meta.get("url", ""),
            description=(meta.get("description") or "")[:3000],
            subtitle=subtitle_text,
        )

        last_err: Exception | None = None
        for model in self.models:
            if model in self._disabled:
                continue
            for attempt in range(2):
                try:
                    result = self._call(model, prompt)
                    logger.info("    Gemini 使用 %s（%d 字）", model, len(result))
                    return result
                except GeminiError as exc:
                    last_err = exc
                    # 429/402/404：额度或权限明确不可用，立即熔断，不重试、不再白发请求
                    if exc.status in (429, 402, 404):
                        self._disabled.add(model)
                        logger.warning(
                            "    %s 额度/权限不可用（HTTP %s），今日跳过", model, exc.status
                        )
                        break
                    # 503：临时高负载，连续 2 次才熔断
                    if exc.status == 503:
                        self._fail_count[model] += 1
                        if self._fail_count[model] >= 2:
                            self._disabled.add(model)
                            logger.warning(
                                "    %s 连续 %d 次 503，今日跳过",
                                model, self._fail_count[model],
                            )
                        else:
                            logger.warning("    %s 503，切换下一个模型", model)
                        break
                    # 其他错误（网络异常/5xx）：最多重试 1 次，仍失败则跳档
                    if attempt == 0:
                        time.sleep(3)
                        continue
                    break
                except Exception as exc:  # noqa: BLE001
                    last_err = exc
                    if attempt == 0:
                        time.sleep(3)
                        continue
                    break
        raise RuntimeError(f"Gemini 总结失败（已尝试 {self.models}）: {last_err}")
