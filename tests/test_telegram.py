import io
import json
from urllib import error

from app.telegram import TelegramJobNotifier


def _make_row(job_id: int, score: int, title: str = "Java Engineer"):
    return {
        "id": job_id,
        "score": score,
        "title": title,
        "company": "Acme",
        "location": "Egypt",
        "url": f"https://www.linkedin.com/jobs/view/{job_id}",
    }


def test_send_jobs_batch_uses_single_message_with_horizontal_separator(monkeypatch) -> None:
    notifier = TelegramJobNotifier(
        bot_token="token",
        relevant_jobs_chat_id="r",
        non_relevant_jobs_chat_id="n",
    )
    rows = [_make_row(1, 10, "A"), _make_row(2, 12, "B")]

    captured = []

    def _fake_send_text(self, _chat_id, text):
        captured.append(text)
        return True

    monkeypatch.setattr(TelegramJobNotifier, "_send_text", _fake_send_text)

    ok = notifier.send_jobs_batch(rows)

    assert ok is True
    assert len(captured) == 1
    assert "\n----------------------------------------\n" in captured[0]
    assert "A" in captured[0]
    assert "B" in captured[0]


def test_send_text_retries_on_429_and_succeeds(monkeypatch) -> None:
    notifier = TelegramJobNotifier(
        bot_token="token",
        relevant_jobs_chat_id="r",
        non_relevant_jobs_chat_id="n",
        max_retries=3,
        fallback_retry_delay_seconds=1,
    )
    attempts = {"count": 0}
    sleeps = []

    class _Resp:
        status = 200

        def read(self):
            return b'{"ok": true}'

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    def _fake_urlopen(_req, timeout=15):
        attempts["count"] += 1
        if attempts["count"] == 1:
            body = json.dumps({"ok": False, "error_code": 429, "parameters": {"retry_after": 2}}).encode("utf-8")
            raise error.HTTPError("https://api.telegram.org", 429, "Too Many Requests", {}, io.BytesIO(body))
        return _Resp()

    monkeypatch.setattr("app.telegram.request.urlopen", _fake_urlopen)
    monkeypatch.setattr("app.telegram.time.sleep", lambda seconds: sleeps.append(seconds))

    ok = notifier._send_text("chat", "hello")

    assert ok is True
    assert attempts["count"] == 2
    assert sleeps == [2]


def test_send_text_fails_after_exhausted_429_retries(monkeypatch) -> None:
    notifier = TelegramJobNotifier(
        bot_token="token",
        relevant_jobs_chat_id="r",
        non_relevant_jobs_chat_id="n",
        max_retries=2,
        fallback_retry_delay_seconds=1,
    )
    attempts = {"count": 0}
    sleeps = []

    def _fake_urlopen(_req, timeout=15):
        attempts["count"] += 1
        body = json.dumps({"ok": False, "error_code": 429, "parameters": {"retry_after": 1}}).encode("utf-8")
        raise error.HTTPError("https://api.telegram.org", 429, "Too Many Requests", {}, io.BytesIO(body))

    monkeypatch.setattr("app.telegram.request.urlopen", _fake_urlopen)
    monkeypatch.setattr("app.telegram.time.sleep", lambda seconds: sleeps.append(seconds))

    ok = notifier._send_text("chat", "hello")

    assert ok is False
    assert attempts["count"] == 2
    assert sleeps == [1]