"""Tiny SMTP helper. No third-party deps — stdlib smtplib + email.

Configure via env:
    SMTP_HOST       e.g. smtp.gmail.com
    SMTP_PORT       e.g. 587
    SMTP_USERNAME   account / API user (also accepted: SMTP_USER)
    SMTP_PASSWORD   app password / API key (also accepted: SMTP_PASS)
    SMTP_FROM       "From:" header, e.g. "Token Tracker <no-reply@you.com>"
    SMTP_STARTTLS   "1" (default) to use STARTTLS on port 587; "0" for implicit
                    TLS on port 465; "0" + port != 465 is plaintext (dev only).
    PUBLIC_BASE_URL absolute base URL the reset link points at,
                    e.g. https://tt.example.com

If SMTP_HOST is unset, send_reset_email() logs to stderr instead of sending.
That keeps local dev working without an SMTP account.
"""
import logging
import os
import smtplib
import ssl
from email.message import EmailMessage

log = logging.getLogger("tokentracker.mailer")


def _smtp_configured() -> bool:
    return bool(os.environ.get("SMTP_HOST"))


def public_base_url() -> str:
    return os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")


def _env_secret(*names: str) -> str:
    """Read the first non-empty env var among `names`, then normalize.

    Normalization: strip outer whitespace, strip a matched pair of surrounding
    quotes (common when people paste a quoted value from docs into .env),
    and collapse internal whitespace. The whitespace collapse matters for
    Gmail app passwords, which are displayed as four space-separated groups.
    """
    raw = ""
    for n in names:
        v = os.environ.get(n)
        if v:
            raw = v
            break
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ('"', "'"):
        raw = raw[1:-1]
    return "".join(raw.split())


def _send(msg: EmailMessage) -> None:
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = _env_secret("SMTP_USERNAME", "SMTP_USER")
    password = _env_secret("SMTP_PASSWORD", "SMTP_PASS")
    use_starttls = os.environ.get("SMTP_STARTTLS", "1") == "1"

    ctx = ssl.create_default_context()
    if use_starttls:
        with smtplib.SMTP(host, port, timeout=15) as s:
            s.starttls(context=ctx)
            if user:
                s.login(user, password or "")
            s.send_message(msg)
    elif port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=15, context=ctx) as s:
            if user:
                s.login(user, password or "")
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=15) as s:
            if user:
                s.login(user, password or "")
            s.send_message(msg)


def send_reset_email(to: str, token: str) -> None:
    """Send a password-reset link. Falls back to a stderr log when unconfigured.

    Best-effort: SMTP failures are logged and swallowed so the request handler
    can still return a generic success response (which it does regardless, to
    avoid leaking whether an email is registered).
    """
    url = f"{public_base_url()}/#/reset?token={token}"

    if not _smtp_configured():
        log.warning("SMTP not configured; reset link for %s: %s", to, url)
        return

    sender = os.environ.get("SMTP_FROM") or os.environ.get("SMTP_USERNAME", "no-reply@localhost")
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = "Reset your Token Tracker password"
    msg.set_content(
        "Someone (hopefully you) asked to reset your Token Tracker password.\n\n"
        f"Open this link to set a new one — it expires in 1 hour:\n{url}\n\n"
        "If you didn't request this, ignore this email; the link won't do "
        "anything until it's used.\n"
    )

    try:
        _send(msg)
    except Exception:
        log.exception("failed to send reset email to %s", to)
