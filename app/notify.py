"""Notification channels: Telegram, email, and an optional Home Assistant webhook.

Each channel fails independently — a Telegram outage must not stop the email,
and vice versa.  :meth:`Notifier.notify` returns a :class:`NotifyResult` so the
caller (and run-logging) can record per-channel outcomes.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from dataclasses import dataclass
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape
from typing import List, Optional

import requests

from .config import Config
from .models import Listing

log = logging.getLogger("flatwatch.notify")


@dataclass
class NotifyResult:
    listing_id: str
    telegram_ok: Optional[bool] = None  # None = channel disabled
    email_ok: Optional[bool] = None
    ha_ok: Optional[bool] = None

    @property
    def any_failed(self) -> bool:
        return False in (self.telegram_ok, self.email_ok, self.ha_ok)

    @property
    def any_sent(self) -> bool:
        return True in (self.telegram_ok, self.email_ok, self.ha_ok)


def _fmt_attrs(listing: Listing) -> str:
    bits = []
    if listing.price is not None:
        bits.append(f"{listing.price:.0f} €")
    if listing.rooms is not None:
        bits.append(f"{listing.rooms:g} Zi")
    if listing.sqm is not None:
        bits.append(f"{listing.sqm:g} m²")
    if listing.location:
        bits.append(listing.location)
    return " · ".join(bits)


class Notifier:
    def __init__(self, cfg: Config, session: Optional[requests.Session] = None):
        self.cfg = cfg
        self._session = session or requests.Session()

    # ----- Telegram --------------------------------------------------------- #
    def _send_telegram(self, listing: Listing) -> bool:
        attrs = _fmt_attrs(listing)
        caption = (
            f"<b>{escape(listing.title)}</b>\n"
            f"{escape(attrs)}\n"
            f'<a href="{escape(listing.url, quote=True)}">{escape(listing.url)}</a>'
        )
        api = f"https://api.telegram.org/bot{self.cfg.telegram_token}"
        try:
            if listing.thumbnail:
                resp = self._session.post(
                    f"{api}/sendPhoto",
                    data={
                        "chat_id": self.cfg.telegram_chat_id,
                        "photo": listing.thumbnail,
                        "caption": caption,
                        "parse_mode": "HTML",
                    },
                    timeout=self.cfg.http_timeout_s,
                )
            else:
                resp = self._session.post(
                    f"{api}/sendMessage",
                    data={
                        "chat_id": self.cfg.telegram_chat_id,
                        "text": caption,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": "false",
                    },
                    timeout=self.cfg.http_timeout_s,
                )
            resp.raise_for_status()
            return True
        except requests.RequestException as exc:
            log.error("Telegram send failed for %s: %s", listing.listing_id, exc)
            return False

    def send_telegram_text(self, text: str) -> bool:
        """Send a plain HTML text message (used for the batch summary, C2)."""
        api = f"https://api.telegram.org/bot{self.cfg.telegram_token}"
        try:
            resp = self._session.post(
                f"{api}/sendMessage",
                data={"chat_id": self.cfg.telegram_chat_id, "text": text, "parse_mode": "HTML"},
                timeout=self.cfg.http_timeout_s,
            )
            resp.raise_for_status()
            return True
        except requests.RequestException as exc:
            log.error("Telegram summary send failed: %s", exc)
            return False

    # ----- Email ------------------------------------------------------------ #
    def _send_email(self, listing: Listing) -> bool:
        attrs = _fmt_attrs(listing)
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"[flatwatch] {listing.title}"
        msg["From"] = self.cfg.email_from
        msg["To"] = self.cfg.email_to

        plain = f"{listing.title}\n{attrs}\n{listing.url}"
        img_html = f'<img src="{escape(listing.thumbnail, quote=True)}" alt=""><br>' if listing.thumbnail else ""
        html = (
            f"<html><body><h3>{escape(listing.title)}</h3>"
            f"<p>{escape(attrs)}</p>{img_html}"
            f'<p><a href="{escape(listing.url, quote=True)}">{escape(listing.url)}</a></p>'
            f"</body></html>"
        )
        msg.attach(MIMEText(plain, "plain", "utf-8"))
        msg.attach(MIMEText(html, "html", "utf-8"))

        try:
            with smtplib.SMTP(self.cfg.smtp_host, self.cfg.smtp_port, timeout=self.cfg.http_timeout_s) as server:
                if self.cfg.smtp_use_tls:
                    server.starttls(context=ssl.create_default_context())
                if self.cfg.smtp_user and self.cfg.smtp_password:
                    server.login(self.cfg.smtp_user, self.cfg.smtp_password)
                server.send_message(msg)
            return True
        except (smtplib.SMTPException, OSError) as exc:
            log.error("Email send failed for %s: %s", listing.listing_id, exc)
            return False

    # ----- Home Assistant (C3) --------------------------------------------- #
    def _send_ha(self, listing: Listing) -> bool:
        payload = {
            "listing_id": listing.listing_id,
            "title": listing.title,
            "url": listing.url,
            "price": listing.price,
            "rooms": listing.rooms,
            "sqm": listing.sqm,
            "location": listing.location,
            "source": listing.source,
        }
        try:
            resp = self._session.post(self.cfg.ha_webhook_url, json=payload, timeout=self.cfg.http_timeout_s)
            resp.raise_for_status()
            return True
        except requests.RequestException as exc:
            log.error("Home Assistant webhook failed for %s: %s", listing.listing_id, exc)
            return False

    # ----- Public ----------------------------------------------------------- #
    def notify(self, listing: Listing) -> NotifyResult:
        """Notify all enabled channels for one listing; channels fail independently."""
        result = NotifyResult(listing_id=listing.listing_id)
        if self.cfg.telegram_enabled:
            result.telegram_ok = self._send_telegram(listing)
        if self.cfg.email_enabled:
            result.email_ok = self._send_email(listing)
        if self.cfg.ha_webhook_url:
            result.ha_ok = self._send_ha(listing)
        if not (self.cfg.telegram_enabled or self.cfg.email_enabled or self.cfg.ha_webhook_url):
            log.info("NEW (no channels configured): %s %s", listing.title, listing.url)
        return result

    def send_summary(self, listings: List[Listing]) -> None:
        """Send one summary message for a batch overflow (C2)."""
        if not listings:
            return
        lines = [f"➕ {len(listings)} weitere neue Inserate:"]
        for lst in listings:
            lines.append(f"• {escape(lst.title)} — {escape(lst.url)}")
        text = "\n".join(lines)
        if self.cfg.telegram_enabled:
            self.send_telegram_text(text)
