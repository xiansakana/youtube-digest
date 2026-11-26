"""Gmail SMTP 发信（应用专用密码，不需要 Gmail 受限 OAuth 权限）。"""
from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, formatdate
from typing import Any


class Mailer:
    def __init__(
        self,
        user: str,
        app_password: str,
        sender_name: str = "YouTube 每日摘要",
        host: str = "smtp.gmail.com",
        port: int = 587,
    ) -> None:
        if not user or not app_password:
            raise RuntimeError("缺少 GMAIL_USER 或 GMAIL_APP_PASSWORD")
        self.user = user
        # 应用专用密码常带空格，统一去掉
        self.password = app_password.replace(" ", "")
        self.sender_name = sender_name
        self.host = host
        self.port = port

    def send(self, recipients: list[str], subject: str, html: str, plain: str) -> None:
        if not recipients:
            raise RuntimeError("收件人为空")
        msg = EmailMessage()
        msg["From"] = formataddr((self.sender_name, self.user))
        msg["To"] = ", ".join(recipients)
        msg["Subject"] = subject
        msg["Date"] = formatdate(localtime=True)
        msg.set_content(plain)
        msg.add_alternative(html, subtype="html")

        context = ssl.create_default_context()
        with smtplib.SMTP(self.host, self.port, timeout=60) as server:
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
            server.login(self.user, self.password)
            server.send_message(msg)

    def verify(self) -> None:
        """仅验证登录，不发信。"""
        context = ssl.create_default_context()
        with smtplib.SMTP(self.host, self.port, timeout=45) as server:
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
            server.login(self.user, self.password)
