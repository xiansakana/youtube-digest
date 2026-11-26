#!/usr/bin/env python3
"""本机 Google OAuth 授权控制台（通用版）。

职责：
1. 完成 Google OAuth 授权（仅 youtube.readonly），拿到 refresh token 写入 data/token.json
2. 预览当前账号订阅的频道
3. 把 Gemini API Key、Gmail 应用专用密码、收件邮箱写入 data/.env 与 data/config.json
4. 可选：发送测试邮件验证 SMTP

只监听 127.0.0.1，不对外暴露。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets as pysecrets
import smtplib
import ssl
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr, formatdate
from pathlib import Path
from urllib.parse import urlencode

import requests
from flask import Flask, Response, jsonify, redirect, request

BASE = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("YTDIGEST_HOME", str(BASE.parent / "data"))).resolve()
TOKEN_PATH = Path(os.environ.get("YTDIGEST_TOKEN", str(DATA_DIR / "token.json")))
ENV_PATH = DATA_DIR / ".env"
CONFIG_PATH = DATA_DIR / "config.json"
CLIENT_PATH = DATA_DIR / "oauth_client.json"

SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
YT_API = "https://www.googleapis.com/youtube/v3"
PORT = int(os.environ.get("AUTH_PORT", "8765"))
REDIRECT_URI = f"http://localhost:{PORT}/oauth2callback"

app = Flask(__name__)
app.secret_key = pysecrets.token_hex(16)
_oauth_state: dict[str, str] = {}

CONFIG_DEFAULTS: dict = {
    "recipients": "",
    "sender_name": "YouTube 每日摘要",
    "gemini_model": "gemini-4-hydrogen",
    "gmail_user": "",
    "gemini_api_key": "",
    "gmail_app_password": "",
    "youtube_proxy": "",
}


# ------------------------------------------------------------------ 读写
def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _write_private(path: Path, text: str) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def load_settings() -> dict:
    """合并 oauth_client.json + .env + config.json + 环境变量。"""
    data = dict(CONFIG_DEFAULTS)
    client = _read_json(CLIENT_PATH)
    data["client_id"] = client.get("client_id", "")
    data["client_secret"] = client.get("client_secret", "")

    env = _read_env()
    for key in ("GEMINI_API_KEY", "GMAIL_USER", "GMAIL_APP_PASSWORD", "YOUTUBE_PROXY"):
        if env.get(key):
            data[key.lower()] = env[key]

    cfg = _read_json(CONFIG_PATH)
    if isinstance(cfg.get("recipients"), list):
        data["recipients"] = ",".join(cfg["recipients"])
    for key in ("sender_name", "gemini_model"):
        if cfg.get(key):
            data[key] = cfg[key]

    # 环境变量最高优先（容器运行时）
    for env_key, field in (
        ("RECIPIENTS", "recipients"),
        ("SENDER_NAME", "sender_name"),
        ("GEMINI_MODEL", "gemini_model"),
        ("GEMINI_API_KEY", "gemini_api_key"),
        ("GMAIL_USER", "gmail_user"),
        ("GMAIL_APP_PASSWORD", "gmail_app_password"),
        ("YOUTUBE_PROXY", "youtube_proxy"),
    ):
        if os.environ.get(env_key):
            data[field] = os.environ[env_key]
    return data


def _read_env() -> dict[str, str]:
    if not ENV_PATH.exists():
        return {}
    out: dict[str, str] = {}
    for raw in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        out[key.strip()] = val.strip().strip('"').strip("'")
    return out


def _write_env(updates: dict[str, str]) -> None:
    current = _read_env()
    current.update({k: v for k, v in updates.items() if v is not None})
    lines = ["# 由授权控制台生成；请勿提交到版本库"]
    for key, value in current.items():
        lines.append(f"{key}={value}")
    _write_private(ENV_PATH, "\n".join(lines) + "\n")


def load_token() -> dict | None:
    return _read_json(TOKEN_PATH) or None


def detect_proxy() -> str | None:
    """本机访问 Google 需要代理时自动识别系统代理（Windows）。"""
    for env in ("YOUTUBE_PROXY", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        value = os.environ.get(env)
        if value:
            return value
    if os.name == "nt":
        try:
            import winreg

            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
            )
            enabled = winreg.QueryValueEx(key, "ProxyEnable")[0]
            server = winreg.QueryValueEx(key, "ProxyServer")[0]
            if enabled and server:
                if not server.startswith("http"):
                    server = "http://" + server
                return server
        except Exception:  # noqa: BLE001
            pass
    return None


PROXY = detect_proxy()
PROXIES = {"http": PROXY, "https": PROXY} if PROXY else None


# ------------------------------------------------------------------ OAuth
def build_auth_url(client_id: str) -> str:
    verifier = base64.urlsafe_b64encode(os.urandom(48)).decode().rstrip("=")
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    state = pysecrets.token_urlsafe(16)
    _oauth_state[state] = verifier
    params = {
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPE,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
    }
    return f"{AUTH_ENDPOINT}?{urlencode(params)}"


def exchange_code(code: str, verifier: str, client_id: str, client_secret: str) -> dict:
    resp = requests.post(
        TOKEN_ENDPOINT,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "code_verifier": verifier,
            "grant_type": "authorization_code",
            "redirect_uri": REDIRECT_URI,
        },
        timeout=30,
        proxies=PROXIES,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"换取 token 失败 HTTP {resp.status_code}: {resp.text[:400]}")
    payload = resp.json()
    expiry = datetime.now(timezone.utc) + timedelta(seconds=int(payload.get("expires_in", 3600)))
    return {
        "token": payload["access_token"],
        "refresh_token": payload.get("refresh_token", ""),
        "token_uri": TOKEN_ENDPOINT,
        "client_id": client_id,
        "client_secret": client_secret,
        "scopes": payload.get("scope", SCOPE).split(),
        "universe_domain": "googleapis.com",
        "expiry": expiry.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "obtained_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }


def valid_access_token() -> str:
    token = load_token()
    if not token:
        raise RuntimeError("尚未授权")
    expiry_raw = token.get("expiry")
    if expiry_raw:
        try:
            exp = datetime.fromisoformat(expiry_raw.replace("Z", "+00:00"))
            if exp - datetime.now(timezone.utc) > timedelta(minutes=2):
                return token["token"]
        except ValueError:
            pass
    resp = requests.post(
        TOKEN_ENDPOINT,
        data={
            "client_id": token["client_id"],
            "client_secret": token["client_secret"],
            "refresh_token": token["refresh_token"],
            "grant_type": "refresh_token",
        },
        timeout=30,
        proxies=PROXIES,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"刷新 token 失败 HTTP {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    token["token"] = data["access_token"]
    exp = datetime.now(timezone.utc) + timedelta(seconds=int(data.get("expires_in", 3600)))
    token["expiry"] = exp.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    if data.get("refresh_token"):
        token["refresh_token"] = data["refresh_token"]
    _write_private(TOKEN_PATH, json.dumps(token, ensure_ascii=False, indent=2))
    return token["token"]


def fetch_subscriptions() -> list[dict]:
    token = valid_access_token()
    out: list[dict] = []
    page = None
    while True:
        params = {"part": "snippet", "mine": "true", "maxResults": 50}
        if page:
            params["pageToken"] = page
        resp = requests.get(
            f"{YT_API}/subscriptions",
            params=params,
            headers={"Authorization": f"Bearer {token}"},
            timeout=45,
            proxies=PROXIES,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"拉取订阅失败 HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        for item in data.get("items", []):
            sn = item["snippet"]
            out.append(
                {
                    "channel_id": sn["resourceId"]["channelId"],
                    "title": sn.get("title", ""),
                    "description": (sn.get("description") or "")[:120],
                    "thumbnail": sn.get("thumbnails", {}).get("default", {}).get("url", ""),
                }
            )
        page = data.get("nextPageToken")
        if not page:
            break
    out.sort(key=lambda x: x["title"].lower())
    return out


# ------------------------------------------------------------------ 路由
@app.get("/")
def index() -> Response:
    html = (BASE / "index.html").read_text(encoding="utf-8")
    html = html.replace("{{DATA_DIR}}", str(DATA_DIR)).replace(
        "{{REDIRECT_URI}}", REDIRECT_URI
    )
    return Response(html, mimetype="text/html")


@app.get("/api/status")
def api_status() -> Response:
    s = load_settings()
    token = load_token()
    return jsonify(
        {
            "authorized": bool(token and token.get("refresh_token")),
            "has_client": bool(s.get("client_id") and s.get("client_secret")),
            "recipients": s.get("recipients", ""),
            "sender_name": s.get("sender_name", ""),
            "gmail_user": s.get("gmail_user", ""),
            "gemini_model": s.get("gemini_model", ""),
            "has_gemini_key": bool(s.get("gemini_api_key")),
            "has_gmail_pass": bool(s.get("gmail_app_password")),
            "data_dir": str(DATA_DIR),
            "redirect_uri": REDIRECT_URI,
        }
    )


@app.get("/auth/start")
def auth_start() -> Response:
    client = _read_json(CLIENT_PATH)
    if not (client.get("client_id") and client.get("client_secret")):
        return redirect("/?error=missing_client")
    return redirect(build_auth_url(client["client_id"]))


@app.get("/oauth2callback")
def oauth_callback() -> Response:
    err = request.args.get("error")
    if err:
        return redirect(f"/?error={err}")
    code = request.args.get("code", "")
    state = request.args.get("state", "")
    verifier = _oauth_state.pop(state, None)
    if not code or not verifier:
        return redirect("/?error=state_mismatch")
    client = _read_json(CLIENT_PATH)
    try:
        token = exchange_code(code, verifier, client["client_id"], client["client_secret"])
    except Exception as exc:  # noqa: BLE001
        return redirect("/?error=" + str(exc)[:120].replace(" ", "_"))
    _write_private(TOKEN_PATH, json.dumps(token, ensure_ascii=False, indent=2))
    return redirect("/?authorized=1")


@app.get("/api/subscriptions")
def api_subscriptions() -> Response:
    try:
        subs = fetch_subscriptions()
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 400
    cfg = _read_json(CONFIG_PATH)
    cfg["_subs_count"] = len(subs)
    _write_private(CONFIG_PATH, json.dumps(cfg, ensure_ascii=False, indent=2))
    return jsonify({"ok": True, "count": len(subs), "items": subs})


@app.post("/api/config")
def api_config() -> Response:
    payload = request.get_json(force=True, silent=True) or {}
    env_updates = {}
    cfg = _read_json(CONFIG_PATH)
    cfg.setdefault("recipients", [])

    recipients_raw = str(payload.get("recipients", "")).strip()
    recipients = [r.strip() for r in recipients_raw.replace(";", ",").split(",") if r.strip()]
    cfg["recipients"] = recipients

    for field in ("sender_name", "gemini_model"):
        if payload.get(field):
            cfg[field] = str(payload[field]).strip()

    if payload.get("gmail_user"):
        env_updates["GMAIL_USER"] = str(payload["gmail_user"]).strip()
    if payload.get("gemini_api_key"):
        env_updates["GEMINI_API_KEY"] = str(payload["gemini_api_key"]).strip()
    if payload.get("gmail_app_password"):
        env_updates["GMAIL_APP_PASSWORD"] = str(payload["gmail_app_password"]).strip()
    if payload.get("youtube_proxy"):
        env_updates["YOUTUBE_PROXY"] = str(payload["youtube_proxy"]).strip()
    env_updates.setdefault("SMTP_HOST", _read_env().get("SMTP_HOST", "smtp.gmail.com"))
    env_updates.setdefault("SMTP_PORT", _read_env().get("SMTP_PORT", "587"))
    if recipients:
        env_updates["RECIPIENTS"] = ",".join(recipients)
    if cfg.get("sender_name"):
        env_updates["SENDER_NAME"] = cfg["sender_name"]
    if cfg.get("gemini_model"):
        env_updates["GEMINI_MODEL"] = cfg["gemini_model"]

    _write_env(env_updates)
    _write_private(CONFIG_PATH, json.dumps(cfg, ensure_ascii=False, indent=2))
    return jsonify({"ok": True, "env_path": str(ENV_PATH)})


@app.post("/api/import-client")
def api_import_client() -> Response:
    payload = request.get_json(force=True, silent=True) or {}
    raw = (payload.get("json") or "").strip()
    if not raw:
        return jsonify({"ok": False, "error": "内容为空"}), 400
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return jsonify({"ok": False, "error": f"JSON 解析失败: {exc}"}), 400
    block = data.get("installed") or data.get("web") or data
    client_id = block.get("client_id")
    client_secret = block.get("client_secret")
    if not client_id or not client_secret:
        return jsonify({"ok": False, "error": "未找到 client_id / client_secret"}), 400
    _write_private(
        CLIENT_PATH,
        json.dumps({"client_id": client_id, "client_secret": client_secret}, ensure_ascii=False, indent=2),
    )
    return jsonify({"ok": True, "client_id": client_id})


@app.post("/api/test-mail")
def api_test_mail() -> Response:
    s = load_settings()
    recipients = [r.strip() for r in s.get("recipients", "").replace(";", ",").split(",") if r.strip()]
    if not recipients:
        return jsonify({"ok": False, "error": "请先填写收件邮箱"}), 400
    if not (s.get("gmail_user") and s.get("gmail_app_password")):
        return jsonify({"ok": False, "error": "请先填写发件 Gmail 与应用专用密码"}), 400
    try:
        _send_test_mail(s["gmail_user"], s["gmail_app_password"], recipients)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "recipients": recipients})


def _send_test_mail(user: str, app_password: str, recipients: list[str]) -> None:
    msg = EmailMessage()
    msg["From"] = formataddr(("YouTube 每日摘要", user))
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = "YouTube 每日摘要 · SMTP 测试"
    msg["Date"] = formatdate(localtime=True)
    msg.set_content("这是一封测试邮件，说明 Gmail SMTP 配置可用。")
    msg.add_alternative(
        "<div style='font-family:sans-serif;font-size:14px;line-height:1.8'>"
        "<p>这是一封 <b>测试邮件</b>，说明 Gmail SMTP 配置可用。</p>"
        "<p>收到它代表每日摘要正式邮件也能发出。</p></div>",
        subtype="html",
    )
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=45) as server:
        server.starttls(context=ssl.create_default_context())
        server.login(user, app_password.replace(" ", ""))
        server.send_message(msg)


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    print("=" * 66)
    print(" YouTube 订阅每日摘要 · 本机授权控制台")
    print(f" 打开 http://localhost:{PORT}")
    print(f" 数据目录: {DATA_DIR}")
    print(f" OAuth 回调地址（Desktop 类型客户端无需登记）: {REDIRECT_URI}")
    print("=" * 66)
    app.run(host="127.0.0.1", port=PORT, debug=False, threaded=True)


if __name__ == "__main__":
    main()
