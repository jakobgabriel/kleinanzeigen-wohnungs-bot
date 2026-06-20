"""Notification channels: Telegram, email, and an optional Home Assistant webhook.

Each channel fails independently — a Telegram outage must not stop the email,
and vice versa.  :meth:`Notifier.notify` returns a :class:`NotifyResult` so the
caller (and run-logging) can record per-channel outcomes.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
import time
from dataclasses import dataclass
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape
from typing import Callable, List, Optional

import requests

from .config import Config
from .models import Listing

log = logging.getLogger("flatwatch.notify")

# Statuses worth retrying on the HTTP-based channels (429 = rate limited).
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def _retry_after_seconds(resp: requests.Response, attempt: int) -> float:
    """Honor a Retry-After header (seconds); else exponential backoff (2,4,8…)."""
    header = resp.headers.get("Retry-After") if resp is not None else None
    if header:
        try:
            return max(0.0, float(header))
        except ValueError:
            pass
    return 2 ** (attempt + 1)


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
    def __init__(
        self,
        cfg: Config,
        session: Optional[requests.Session] = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.cfg = cfg
        self._session = session or requests.Session()
        self._sleep = sleep

    # ----- shared retry ----------------------------------------------------- #
    def _http_send(self, do_post: Callable[[], requests.Response], label: str) -> bool:
        """POST with retry/backoff on timeouts, 5xx, and 429 (honoring Retry-After).

        Mirrors sources.http_get's transient-failure policy. Non-retryable client
        errors (4xx other than 429) fail fast without retrying.
        """
        retries = self.cfg.max_retries
        for attempt in range(retries + 1):
            try:
                resp = do_post()
            except (requests.Timeout, requests.ConnectionError) as exc:
                if attempt < retries:
                    wait = 2 ** (attempt + 1)
                    log.warning("%s transient error (attempt %d): %s; retry in %ds", label, attempt + 1, exc, wait)
                    self._sleep(wait)
                    continue
                log.error("%s failed after %d retries: %s", label, retries, exc)
                return False

            if resp.status_code in _RETRYABLE_STATUS:
                if attempt < retries:
                    wait = _retry_after_seconds(resp, attempt)
                    log.warning("%s got %d (attempt %d); retry in %.0fs", label, resp.status_code, attempt + 1, wait)
                    self._sleep(wait)
                    continue
                log.error("%s failed after %d retries: HTTP %d", label, retries, resp.status_code)
                return False

            try:
                resp.raise_for_status()
            except requests.RequestException as exc:
                log.error("%s failed (client error): %s", label, exc)  # 4xx — do not retry
                return False
            return True
        return False

    # ----- Telegram --------------------------------------------------------- #
    def _send_telegram(self, listing: Listing) -> bool:
        attrs = _fmt_attrs(listing)
        caption = (
            f"<b>{escape(listing.title)}</b>\n"
            f"{escape(attrs)}\n"
            f'<a href="{escape(listing.url, quote=True)}">{escape(listing.url)}</a>'
        )
        api = f"https://api.telegram.org/bot{self.cfg.telegram_token}"
        if listing.thumbnail:
            endpoint, data = f"{api}/sendPhoto", {
                "chat_id": self.cfg.telegram_chat_id,
                "photo": listing.thumbnail,
                "caption": caption,
                "parse_mode": "HTML",
            }
        else:
            endpoint, data = f"{api}/sendMessage", {
                "chat_id": self.cfg.telegram_chat_id,
                "text": caption,
                "parse_mode": "HTML",
                "disable_web_page_preview": "false",
            }
        return self._http_send(
            lambda: self._session.post(endpoint, data=data, timeout=self.cfg.http_timeout_s),
            label=f"Telegram[{listing.listing_id}]",
        )

    def send_telegram_text(self, text: str) -> bool:
        """Send a plain HTML text message (used for the batch summary, C2)."""
        api = f"https://api.telegram.org/bot{self.cfg.telegram_token}"
        return self._http_send(
            lambda: self._session.post(
                f"{api}/sendMessage",
                data={"chat_id": self.cfg.telegram_chat_id, "text": text, "parse_mode": "HTML"},
                timeout=self.cfg.http_timeout_s,
            ),
            label="Telegram[summary]",
        )

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

        retries = self.cfg.max_retries
        for attempt in range(retries + 1):
            try:
                with smtplib.SMTP(self.cfg.smtp_host, self.cfg.smtp_port, timeout=self.cfg.http_timeout_s) as server:
                    if self.cfg.smtp_use_tls:
                        server.starttls(context=ssl.create_default_context())
                    if self.cfg.smtp_user and self.cfg.smtp_password:
                        server.login(self.cfg.smtp_user, self.cfg.smtp_password)
                    server.send_message(msg)
                return True
            except (smtplib.SMTPException, OSError) as exc:
                if attempt < retries:
                    wait = 2 ** (attempt + 1)
                    log.warning("Email transient error for %s (attempt %d): %s; retry in %ds",
                                listing.listing_id, attempt + 1, exc, wait)
                    self._sleep(wait)
                    continue
                log.error("Email send failed for %s after %d retries: %s", listing.listing_id, retries, exc)
                return False
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
        return self._http_send(
            lambda: self._session.post(self.cfg.ha_webhook_url, json=payload, timeout=self.cfg.http_timeout_s),
            label=f"HomeAssistant[{listing.listing_id}]",
        )

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

    def send_summary(self, listings: List[Listing]) -> bool:
        """Send one summary message for a batch overflow (C2). Returns delivered."""
        if not listings:
            return True
        lines = [f"➕ {len(listings)} weitere neue Inserate:"]
        for lst in listings:
            lines.append(f"• {escape(lst.title)} — {escape(lst.url)}")
        text = "\n".join(lines)
        if self.cfg.telegram_enabled:
            return self.send_telegram_text(text)
        return False
