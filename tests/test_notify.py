import smtplib
import ssl

import pytest

from sitewatch.notify import MailError, build_message, render, send_email
from tests.helpers import make_analysis, make_change, make_settings


def test_render_with_analysis_escapes_html():
    change = make_change()
    change.analysis = make_analysis()
    subject, text, html_body = render([change], [])
    assert subject.startswith("[Мониторинг]")
    assert "[ВЫСОКАЯ]" in text and "до 01.07.2026" in text and "обновить интеграцию" in text
    assert "&lt;b&gt;20&lt;/b&gt;" in html_body and "<b>20</b>" not in html_body


def test_render_without_analysis_falls_back_to_excerpt_or_diff():
    _, text, _ = render([make_change(text="x" * 5000)], [])
    assert "Автоматический анализ недоступен" in text and "…" in text

    _, text, _ = render([make_change(kind="updated", diff="-старое\n+новое")], [])
    assert "+новое" in text


def test_render_alert_only_and_truncation_note():
    subject, text, html_body = render([], ["Источник X недоступен"])
    assert "Сбой" in subject and "Источник X недоступен" in html_body

    change = make_change()
    change.truncated = True
    assert "не целиком" in render([change], [])[1]


def test_changes_are_listed_in_given_order_with_count_in_subject():
    a, b = make_change(title="Первый"), make_change(title="Второй")
    subject, text, _ = render([a, b], [])
    assert "Обновлений: 2" in subject and text.index("Первый") < text.index("Второй")


def test_build_message_is_multipart_utf8():
    settings = make_settings(mail_to=("a@x.ru", "b@x.ru"))
    msg = build_message(settings, "Тема", "Текст", "<p>Текст</p>")
    assert msg["To"] == "a@x.ru, b@x.ru"
    assert [p.get_content_type() for p in msg.iter_parts()] == ["text/plain", "text/html"]
    assert msg.get_body(("plain",)).get_content().strip() == "Текст"


# --- понятные ошибки почты ---


class FakeServer:
    """Подмена smtplib-сервера: падает на нужном шаге."""

    def __init__(self, fail_login=None, fail_send=None):
        self.fail_login, self.fail_send = fail_login, fail_send
        self.sent = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        pass

    def login(self, user, password):
        if self.fail_login:
            raise self.fail_login

    def send_message(self, msg):
        if self.fail_send:
            raise self.fail_send
        self.sent.append(msg)


def patch_smtp(monkeypatch, *, connect_error=None, **server_kwargs):
    server = FakeServer(**server_kwargs)

    def factory(*args, **kwargs):
        if connect_error:
            raise connect_error
        return server

    monkeypatch.setattr(smtplib, "SMTP_SSL", factory)
    monkeypatch.setattr(smtplib, "SMTP", factory)
    return server


def mail_settings(**kw):
    return make_settings(smtp_user="bot@example.com", smtp_password="pw", smtp_port=465, **kw)


def test_send_email_success_logs_in_and_sends(monkeypatch):
    server = patch_smtp(monkeypatch)
    send_email(mail_settings(), "Тема", "Текст", "<p>Текст</p>")
    assert len(server.sent) == 1 and server.sent[0]["Subject"] == "Тема"


@pytest.mark.parametrize(
    "patch_kwargs, expected",
    [
        ({"connect_error": ConnectionRefusedError("refused")}, "не удалось подключиться к smtp.example.com:465"),
        ({"connect_error": TimeoutError()}, "таймаут"),
        ({"connect_error": ssl.SSLError("wrong version number")}, "SMTP_SECURITY=ssl"),
        ({"fail_login": smtplib.SMTPAuthenticationError(535, b"bad credentials")}, "пароль приложения"),
        ({"fail_login": smtplib.SMTPNotSupportedError("no AUTH")}, "не поддерживает авторизацию"),
        ({"fail_send": smtplib.SMTPRecipientsRefused({"a@x.ru": (550, b"no")})}, "a@x.ru"),
        ({"fail_send": smtplib.SMTPSenderRefused(550, b"no", "from@x.ru")}, "MAIL_FROM"),
        ({"fail_send": smtplib.SMTPServerDisconnected("closed")}, "ошибка почтового сервера"),
    ],
)
def test_mail_errors_are_explained_in_plain_russian(monkeypatch, patch_kwargs, expected):
    patch_smtp(monkeypatch, **patch_kwargs)
    with pytest.raises(MailError, match=expected) as info:
        send_email(mail_settings(), "Тема", "Текст", "<p>x</p>")
    assert info.value.__cause__ is not None  # исходная причина сохранена для журнала


def test_missing_mail_settings_are_named():
    with pytest.raises(MailError, match="SMTP_HOST"):
        send_email(make_settings(smtp_host="", mail_to=()), "Тема", "Текст", "<p>x</p>")
