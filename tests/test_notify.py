"""Tests for notification channel independence and formatting (A1, C1-C3)."""

import requests

from app.models import Listing
from app.notify import Notifier, NotifyResult
from tests.conftest import make_config


class RecordingSession:
    def __init__(self, fail_urls=()):
        self.fail_urls = fail_urls
        self.posts = []

    def post(self, url, data=None, json=None, timeout=None):
        self.posts.append({"url": url, "data": data, "json": json})
        if any(f in url for f in self.fail_urls):
            raise requests.ConnectionError("boom")
        return _Ok()


class _Ok:
    status_code = 200

    def raise_for_status(self):
        return None


def _listing():
    return Listing.create(
        source="kleinanzeigen", title="Schöne Wohnung", url="https://x/ad/1",
        native_id="1", price=1200, rooms=3, sqm=80, location="Berlin",
    )


def test_telegram_html_message():
    cfg = make_config(telegram_token="t", telegram_chat_id="c")
    sess = RecordingSession()
    res = Notifier(cfg, session=sess).notify(_listing())
    assert res.telegram_ok is True
    assert res.email_ok is None  # email disabled
    post = sess.posts[0]
    assert post["data"]["parse_mode"] == "HTML"
    assert "<b>Schöne Wohnung</b>" in post["data"]["text"]


def test_telegram_sendphoto_when_thumbnail():
    cfg = make_config(telegram_token="t", telegram_chat_id="c")
    sess = RecordingSession()
    lst = _listing()
    lst.thumbnail = "https://img/x.jpg"
    Notifier(cfg, session=sess).notify(lst)
    assert sess.posts[0]["url"].endswith("/sendPhoto")


def test_channels_fail_independently():
    cfg = make_config(telegram_token="t", telegram_chat_id="c", ha_webhook_url="https://ha/hook")
    # Telegram fails, HA succeeds — both attempted, independent results.
    sess = RecordingSession(fail_urls=["api.telegram.org"])
    res = Notifier(cfg, session=sess).notify(_listing())
    assert res.telegram_ok is False
    assert res.ha_ok is True
    assert res.any_failed is True
    assert res.any_sent is True


def test_ha_skipped_when_unset():
    cfg = make_config(telegram_token="t", telegram_chat_id="c")
    sess = RecordingSession()
    res = Notifier(cfg, session=sess).notify(_listing())
    assert res.ha_ok is None


def test_email_multipart_sent(monkeypatch):
    cfg = make_config(smtp_host="smtp.example", email_from="a@x", email_to="b@y", smtp_use_tls=False)
    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            sent["host"] = host
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def starttls(self, context=None):
            sent["tls"] = True
        def login(self, u, p):
            sent["login"] = (u, p)
        def send_message(self, msg):
            sent["msg"] = msg

    import app.notify as n
    monkeypatch.setattr(n.smtplib, "SMTP", FakeSMTP)
    res = Notifier(cfg).notify(_listing())
    assert res.email_ok is True
    assert sent["host"] == "smtp.example"
    assert sent["msg"]["Subject"].startswith("[flatwatch]")
    # multipart/alternative carries both a plain and an html part.
    payloads = [p.get_content_type() for p in sent["msg"].get_payload()]
    assert "text/plain" in payloads and "text/html" in payloads


def test_summary_sent_via_telegram():
    cfg = make_config(telegram_token="t", telegram_chat_id="c")
    sess = RecordingSession()
    Notifier(cfg, session=sess).send_summary([_listing(), _listing()])
    assert sess.posts[0]["url"].endswith("/sendMessage")
    assert "weitere neue Inserate" in sess.posts[0]["data"]["text"]
