"""SMTP transport for the digest email.

Provider-agnostic — set SMTP_HOST/SMTP_PORT/SMTP_USERNAME/SMTP_PASSWORD to point
at Gmail (smtp.gmail.com:587), SendGrid (smtp.sendgrid.net:587, user='apikey'),
SES, etc. STARTTLS is on by default; set SMTP_STARTTLS=false for an SMTPS
listener (port 465) — implicit TLS will be used instead.
"""

from __future__ import annotations

import smtplib
from email.message import EmailMessage


def send_email(
    *,
    host: str,
    port: int,
    username: str | None,
    password: str | None,
    sender: str,
    recipients: list[str],
    subject: str,
    html: str,
    starttls: bool = True,
    timeout: float = 30.0,
) -> None:
    """Send a single HTML email. Raises on transport / auth failure.

    `recipients` may have multiple addresses; they all get To-addressed.
    """
    if not recipients:
        raise ValueError("send_email: recipients list is empty")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.set_content("This email requires an HTML-capable client.")
    msg.add_alternative(html, subtype="html")

    if starttls:
        with smtplib.SMTP(host, port, timeout=timeout) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()
            if username and password:
                smtp.login(username, password)
            smtp.send_message(msg, from_addr=sender, to_addrs=recipients)
    else:
        with smtplib.SMTP_SSL(host, port, timeout=timeout) as smtp:
            if username and password:
                smtp.login(username, password)
            smtp.send_message(msg, from_addr=sender, to_addrs=recipients)
