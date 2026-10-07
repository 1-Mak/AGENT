"""Формирование и отправка письма-дайджеста."""

from __future__ import annotations

import html
import smtplib
import ssl
from datetime import datetime
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from .config import Settings
from .models import Change

KIND_LABEL = {"new": "новая страница", "updated": "страница обновлена"}
IMPORTANCE_ORDER = {"высокая": 0, "средняя": 1, "низкая": 2}
IMPORTANCE_COLOR = {"высокая": "#c62828", "средняя": "#ef6c00", "низкая": "#546e7a"}
EXCERPT_CHARS = 1200
EXCERPT_DIFF_LINES = 60


def importance_rank(change: Change) -> int:
    if change.analysis is None:
        return 3
    return IMPORTANCE_ORDER.get(change.analysis.importance, 3)


def _fallback_excerpt(change: Change) -> str:
    if change.kind == "updated" and change.diff:
        lines = change.diff.splitlines()
        excerpt = "\n".join(lines[:EXCERPT_DIFF_LINES])
        return excerpt + ("\n…" if len(lines) > EXCERPT_DIFF_LINES else "")
    text = change.record.text
    return text[:EXCERPT_CHARS] + ("…" if len(text) > EXCERPT_CHARS else "")


def _subject(changes: list[Change], alerts: list[str]) -> str:
    if len(changes) == 1:
        return f"[Мониторинг] {changes[0].record.title[:120]}"
    if changes:
        names = ", ".join(dict.fromkeys(c.source.name for c in changes))
        return f"[Мониторинг] Обновлений: {len(changes)} — {names}"[:200]
    return "[Мониторинг] Сбой: источник недоступен"


def render(changes: list[Change], alerts: list[str], now: datetime | None = None) -> tuple[str, str, str]:
    """Возвращает (тема, текстовая версия, HTML-версия)."""
    date = (now or datetime.now()).strftime("%d.%m.%Y")
    header = f"Мониторинг документации — {date}"
    text: list[str] = [header, ""]
    body: list[str] = [f"<h2 style='margin:0 0 4px'>{html.escape(header)}</h2>"]

    if changes:
        text += [f"Найдено обновлений: {len(changes)}", ""]
        body.append(f"<p style='color:#555;margin:0 0 16px'>Найдено обновлений: {len(changes)}</p>")

    for c in changes:
        a = c.analysis
        label = KIND_LABEL.get(c.kind, c.kind)
        badge = a.importance if a else "без анализа"
        color = IMPORTANCE_COLOR.get(badge, "#546e7a")
        text += [f"[{badge.upper()}] {c.record.title} ({label})", f"Источник: {c.source.name}", c.record.url, ""]
        body.append(
            "<div style='border-top:1px solid #ddd;padding:12px 0'>"
            f"<span style='background:{color};color:#fff;padding:2px 8px;border-radius:10px;font-size:12px'>"
            f"{html.escape(badge)}</span> "
            f"<span style='color:#777;font-size:12px'>{html.escape(label)} · {html.escape(c.source.name)}</span>"
            f"<h3 style='margin:6px 0'><a href='{html.escape(c.record.url, quote=True)}'>"
            f"{html.escape(c.record.title)}</a></h3>"
        )
        if a:
            text += [f"Кратко: {a.summary}"]
            body.append(f"<p>{html.escape(a.summary)}</p>")
            for title, items in (
                ("Что изменилось", a.key_changes),
                ("Сроки", a.deadlines),
                ("Что сделать", a.required_actions),
            ):
                if items:
                    text += [f"{title}:"] + [f" - {i}" for i in items]
                    body.append(
                        f"<b>{title}</b><ul style='margin:4px 0'>"
                        + "".join(f"<li>{html.escape(i)}</li>" for i in items)
                        + "</ul>"
                    )
            text += [f"Кого касается: {a.who_is_affected}", f"Почему {a.importance}: {a.importance_reason}"]
            body.append(
                f"<p><b>Кого касается:</b> {html.escape(a.who_is_affected)}<br>"
                f"<span style='color:#777'>Почему «{html.escape(a.importance)}»: "
                f"{html.escape(a.importance_reason)}</span></p>"
            )
        else:
            excerpt = _fallback_excerpt(c)
            text += ["Автоматический анализ недоступен. Фрагмент:", excerpt]
            body.append(
                "<p style='color:#777'>Автоматический анализ недоступен. Фрагмент:</p>"
                f"<pre style='white-space:pre-wrap;background:#f5f5f5;padding:8px'>{html.escape(excerpt)}</pre>"
            )
        if c.truncated:
            note = "Текст документа очень длинный и был передан анализу не целиком."
            text.append(note)
            body.append(f"<p style='color:#c62828'>{note}</p>")
        text.append("")
        body.append("</div>")

    if alerts:
        text += ["Сбои мониторинга:"] + [f" - {m}" for m in alerts] + [""]
        body.append(
            "<div style='border-top:1px solid #ddd;padding:12px 0'><b>Сбои мониторинга</b><ul>"
            + "".join(f"<li>{html.escape(m)}</li>" for m in alerts)
            + "</ul></div>"
        )

    html_doc = (
        "<html><body style='font-family:Arial,sans-serif;font-size:14px;max-width:720px'>"
        + "".join(body)
        + "</body></html>"
    )
    return _subject(changes, alerts), "\n".join(text).rstrip() + "\n", html_doc


def build_message(settings: Settings, subject: str, text: str, html_body: str) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.mail_from
    msg["To"] = ", ".join(settings.mail_to)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    msg.set_content(text)
    msg.add_alternative(html_body, subtype="html")
    return msg


def send_email(settings: Settings, subject: str, text: str, html_body: str) -> None:
    missing = settings.missing_mail_settings()
    if missing:
        raise RuntimeError(f"Не заданы настройки почты: {', '.join(missing)}")
    msg = build_message(settings, subject, text, html_body)

    security = settings.smtp_security or ("ssl" if settings.smtp_port == 465 else "starttls")
    context = ssl.create_default_context()
    if security == "ssl":
        server: smtplib.SMTP = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, context=context, timeout=30)
    else:
        server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30)
    with server:
        if security == "starttls":
            server.starttls(context=context)
        if settings.smtp_user:
            server.login(settings.smtp_user, settings.smtp_password)
        server.send_message(msg)
