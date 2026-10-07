from sitewatch.notify import build_message, render
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
