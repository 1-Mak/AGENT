"""Формирование и отправка письма-дайджеста."""

from __future__ import annotations

import html
import re
import smtplib
import ssl
from datetime import date, datetime
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Sequence

from .config import Settings
from .models import Change, Place, Scope
from .signals import Signal, sort_signals

KIND_LABEL = {"new": "новый документ", "updated": "документ обновлён"}
IMPORTANCE_ORDER = {"высокая": 0, "средняя": 1, "низкая": 2}
IMPORTANCE_COLOR = {"высокая": "#c62828", "средняя": "#ef6c00", "низкая": "#546e7a"}
LEVEL_COLOR = {"высокая": "#c62828", "средняя": "#ef6c00", "низкая": "#546e7a"}
QUOTE_SHOWN_CHARS = 200
EXCERPT_CHARS = 1200
EXCERPT_DIFF_LINES = 60
FILTERED_SHOWN = 5
NO_DEADLINE = "не указан"
NO_ACTIONS = "в документе не указано"


def importance_rank(change: Change) -> int:
    if change.analysis is None:
        return 3
    return IMPORTANCE_ORDER.get(change.analysis.importance, 3)


def deadline_date(value: str) -> date | None:
    """ДД.ММ.ГГГГ из строки срока; None, если даты нет."""
    m = re.search(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b", value or "")
    if not m:
        return None
    try:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None


def sort_key(change: Change) -> tuple[int, date]:
    """Сначала важное, внутри важности — у кого срок раньше; без срока — в конце."""
    a = change.analysis
    return importance_rank(change), (deadline_date(a.nearest_deadline) if a else None) or date.max


def _fallback_excerpt(change: Change) -> str:
    if change.kind == "updated" and change.diff:
        lines = change.diff.splitlines()
        excerpt = "\n".join(lines[:EXCERPT_DIFF_LINES])
        return excerpt + ("\n…" if len(lines) > EXCERPT_DIFF_LINES else "")
    text = change.record.text
    return text[:EXCERPT_CHARS] + ("…" if len(text) > EXCERPT_CHARS else "")


def _subject(changes: list[Change], alerts: list[str], signals: Sequence[Signal] = ()) -> str:
    signals = sort_signals(list(signals))
    if signals:
        head = f"[Мониторинг] СИГНАЛЫ API: {len(signals)}"
        tail = f"{signals[0].title}: {signals[0].message}"
        if changes:
            head += f", обновлений: {len(changes)}"
        return f"{head} — {tail}"[:200]
    if not changes:
        return "[Мониторинг] Сбой: источник недоступен"
    top = changes[0]
    a = top.analysis
    tags = []
    if a and a.nearest_deadline:
        tags.append(f"срок: {a.nearest_deadline}")
    if a and a.affected_components:
        tags.append("затронуто: " + ", ".join(a.affected_components[:3]).lower())
    prefix = ", ".join(tags)  # самое важное видно в списке писем, не открывая: «срок: 01.09.2026, затронуто: авторизация»
    if len(changes) == 1:
        title = top.record.title[:120]
        return f"[Мониторинг] {prefix} — {title}" if prefix else f"[Мониторинг] {title}"
    names = ", ".join(dict.fromkeys(c.source.name for c in changes))
    return f"[Мониторинг] Обновлений: {len(changes)} — {prefix or names}"[:200]


def _where_lines(change: Change, where: dict[str, str]) -> list[str]:
    """«Где менять»: объекты вашей системы из таблицы соответствий, затем методы и поля из документа."""
    a = change.analysis
    if a is None:
        return []
    lines = [f"{n} → {where[n]}" if where.get(n) else n for n in a.affected_components]
    seen = {n.casefold() for n in a.affected_components}  # «Отчёт о нанесении» и «отчёт о нанесении» — одно и то же
    for method in a.affected_methods:
        if method.casefold() not in seen:
            seen.add(method.casefold())
            lines.append(method)
    return lines


def _deadline_lines(change: Change) -> list[str]:
    a = change.analysis
    if a is None:
        return []
    if a.deadlines:
        return list(a.deadlines)
    return [a.nearest_deadline] if a.nearest_deadline else []


def _rows(change: Change, where: dict[str, str]) -> list[tuple[str, list[str]]]:
    a = change.analysis
    assert a is not None
    return [
        ("Что", [a.summary]),
        ("Где", _where_lines(change, where) or ["не указано"]),
        ("Как", list(a.required_actions) or [NO_ACTIONS]),
        ("Срок", _deadline_lines(change) or [NO_DEADLINE]),
    ]


def _short(text: str, limit: int = QUOTE_SHOWN_CHARS) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _places(change: Change) -> list[Place]:
    """Места в документе: только те, чья цитата подтверждена по тексту (остальные отбрасывает locate.ground_places)."""
    a = change.analysis
    return [p for p in (a.places if a else []) if p.verified]


def _place_text(place: Place) -> str:
    head = " — ".join(x for x in (place.section, f"«{_short(place.quote)}»") if x)
    line = head + (f" → {place.change}" if head and place.change else (f"→ {place.change}" if place.change else ""))
    return line + (f" (стр. {place.page})" if place.page else "")


def _place_html(place: Place, doc_url: str) -> str:
    parts = []
    if place.section:
        parts.append(f"<b>{html.escape(place.section)}</b>")
    parts.append(f"«{html.escape(_short(place.quote))}»")
    line = " — ".join(parts)
    if place.change:
        line += (" → " if line else "→ ") + html.escape(place.change)
    if place.page:
        line += f" (стр. {place.page})"
    if place.url != doc_url:  # у страницы-цитаты и PDF есть ссылка, ведущая прямо к месту
        line += f" · <a href='{html.escape(place.url, quote=True)}'>открыть место</a>"
    return line


def render(
    changes: list[Change],
    alerts: list[str],
    now: datetime | None = None,
    *,
    scope: Scope | None = None,
    filtered: Sequence[Change] = (),
    signals: Sequence[Signal] = (),
) -> tuple[str, str, str]:
    """Возвращает (тема, текстовая версия, HTML-версия)."""
    date_ = (now or datetime.now()).strftime("%d.%m.%Y")
    header = f"Мониторинг документации — {date_}"
    text: list[str] = [header, ""]
    body: list[str] = [f"<h2 style='margin:0 0 4px'>{html.escape(header)}</h2>"]
    where = {e.name: e.where for e in (scope.glossary if scope else ())}
    on_topic = bool(scope and scope.interests)
    signals = sort_signals(list(signals))

    if signals:
        text += [f"Сигналы API и журнала: {len(signals)}"] + [f" ! [{s.level.upper()}] {s.title}: {s.message}" for s in signals] + [""]
        body.append(
            "<div style='border:1px solid #c62828;border-radius:6px;padding:8px 12px;margin:8px 0 16px'>"
            f"<b style='color:#c62828'>Сигналы API и журнала: {len(signals)}</b><ul style='margin:6px 0'>"
            + "".join(
                f"<li><span style='color:{LEVEL_COLOR.get(s.level, '#546e7a')}'>[{html.escape(s.level)}]</span> "
                f"<b>{html.escape(s.title)}</b>: {html.escape(s.message)}</li>"
                for s in signals
            )
            + "</ul></div>"
        )

    if changes:
        text += [f"Найдено обновлений: {len(changes)}", ""]
        body.append(f"<p style='color:#555;margin:0 0 12px'>Найдено обновлений: {len(changes)}</p>")

    for c in changes:
        a = c.analysis
        label = KIND_LABEL.get(c.kind, c.kind)
        badge = a.importance if a else "без анализа"
        color = IMPORTANCE_COLOR.get(badge, "#546e7a")
        general = " · общее изменение" if on_topic and a and a.relevance == "общее" else ""
        origin = f"{c.source.name}{general}"
        text += [f"[{badge.upper()}] {c.record.title} ({label})"]
        body.append(
            "<div style='border-top:1px solid #ddd;padding:10px 0'>"
            f"<span style='background:{color};color:#fff;padding:2px 8px;border-radius:10px;font-size:12px'>"
            f"{html.escape(badge)}</span> "
            f"<span style='color:#777;font-size:12px'>{html.escape(label)} · {html.escape(origin)}</span>"
            f"<h3 style='margin:6px 0'><a href='{html.escape(c.record.url, quote=True)}'>"
            f"{html.escape(c.record.title)}</a></h3>"
        )
        if a:
            rows = _rows(c, where)
            width = max(len(name) for name, _ in rows) + 1
            html_rows = []
            for name, items in rows:
                if len(items) == 1:
                    text.append(f"  {name + ':':<{width + 1}} {items[0]}")
                else:
                    text.append(f"  {name}:")
                    text += [f"    - {i}" for i in items]
                muted = items in (["не указано"], [NO_ACTIONS], [NO_DEADLINE])
                cell = (
                    html.escape(items[0])
                    if len(items) == 1
                    else "<ul style='margin:0;padding-left:18px'>" + "".join(f"<li>{html.escape(i)}</li>" for i in items) + "</ul>"
                )
                strong = name == "Срок" and not muted
                style = "color:#999" if muted else ("font-weight:bold;color:#c62828" if strong else "")
                html_rows.append(
                    f"<tr><td style='vertical-align:top;padding:2px 12px 2px 0;color:#555;white-space:nowrap'><b>{name}</b></td>"
                    f"<td style='padding:2px 0;{style}'>{cell}</td></tr>"
                )
            places = _places(c)
            if places:
                text.append("  В документе:")
                for place in places:
                    text.append(f"    - {_place_text(place)}")
                    if place.url != c.record.url:
                        text.append(f"      {place.url}")
                html_rows.append(
                    "<tr><td style='vertical-align:top;padding:2px 12px 2px 0;color:#555;white-space:nowrap'><b>В документе</b></td>"
                    "<td style='padding:2px 0'><ul style='margin:0;padding-left:18px'>"
                    + "".join(f"<li>{_place_html(p, c.record.url)}</li>" for p in places)
                    + "</ul></td></tr>"
                )
            body.append("<table style='border-collapse:collapse'>" + "".join(html_rows) + "</table>")
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
        text.append(f"  Источник: {origin}")
        text.append(f"  Ссылка:   {c.record.url}")
        if c.found_on:
            text.append(f"  Файл найден на странице: {c.found_on}")
        text.append("")
        found = (
            f" · файл найден на странице <a href='{html.escape(c.found_on, quote=True)}'>{html.escape(c.found_on)}</a>"
            if c.found_on
            else ""
        )
        body.append(
            "<p style='margin:8px 0 0;font-size:12px;color:#555'>"
            f"Источник: {html.escape(origin)} · <a href='{html.escape(c.record.url, quote=True)}'>ссылка на документ</a>{found}</p>"
        )
        body.append("</div>")

    if filtered:
        # Отфильтрованное не пропадает бесследно: если агент ошибся, это видно и можно поправить тему в конфиге.
        shown = list(filtered)[:FILTERED_SHOWN]
        lines = [c.record.title + (f" ({c.analysis.relevance_note})" if c.analysis and c.analysis.relevance_note else "") for c in shown]
        more = f" и ещё {len(filtered) - len(shown)}" if len(filtered) > len(shown) else ""
        text += [f"Не вошло в письмо как не относящееся к теме: {len(filtered)}{more}"] + [f" - {line}" for line in lines] + [""]
        body.append(
            "<div style='border-top:1px solid #ddd;padding:10px 0;color:#777;font-size:13px'>"
            f"Не вошло в письмо как не относящееся к теме: {len(filtered)}{html.escape(more)}<ul style='margin:4px 0'>"
            + "".join(f"<li>{html.escape(line)}</li>" for line in lines)
            + "</ul></div>"
        )

    if alerts:
        text += ["Сбои мониторинга:"] + [f" - {m}" for m in alerts] + [""]
        body.append(
            "<div style='border-top:1px solid #ddd;padding:10px 0'><b>Сбои мониторинга</b><ul>"
            + "".join(f"<li>{html.escape(m)}</li>" for m in alerts)
            + "</ul></div>"
        )

    html_doc = (
        "<html><body style='font-family:Arial,sans-serif;font-size:14px;max-width:720px'>"
        + "".join(body)
        + "</body></html>"
    )
    return _subject(changes, alerts, signals), "\n".join(text).rstrip() + "\n", html_doc


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


class MailError(RuntimeError):
    """Письмо не ушло; текст ошибки написан для человека и подсказывает, что поправить."""


def _explain(error: Exception, settings: Settings) -> str:
    target = f"{settings.smtp_host}:{settings.smtp_port}"
    if isinstance(error, smtplib.SMTPAuthenticationError):
        return (
            f"сервер {target} отклонил логин или пароль (SMTP_USER / SMTP_PASSWORD). "
            "Для Gmail, Яндекс и Mail.ru нужен пароль приложения, а не обычный пароль от почты"
        )
    if isinstance(error, smtplib.SMTPNotSupportedError):
        return (
            f"сервер {target} не поддерживает авторизацию. Если он без пароля, оставьте SMTP_USER "
            "и SMTP_PASSWORD пустыми; иначе проверьте SMTP_PORT и SMTP_SECURITY"
        )
    if isinstance(error, smtplib.SMTPRecipientsRefused):
        return f"сервер не принял адреса получателей (MAIL_TO): {', '.join(error.recipients) or 'все'}"
    if isinstance(error, smtplib.SMTPSenderRefused):
        return "сервер не принял адрес отправителя MAIL_FROM — у многих серверов он должен совпадать с SMTP_USER"
    if isinstance(error, ssl.SSLError):
        return (
            f"не удалось установить защищённое соединение с {target}: {error}. "
            "Для порта 465 нужен SMTP_SECURITY=ssl, для порта 587 — starttls"
        )
    if isinstance(error, TimeoutError):
        return f"сервер {target} не отвечает (таймаут). Проверьте SMTP_HOST, SMTP_PORT и доступ из вашей сети"
    if isinstance(error, smtplib.SMTPException):
        return f"ошибка почтового сервера {target}: {error}"
    return f"не удалось подключиться к {target}: {error}. Проверьте SMTP_HOST, SMTP_PORT, интернет и файрвол"


def send_email(settings: Settings, subject: str, text: str, html_body: str) -> None:
    missing = settings.missing_mail_settings()
    if missing:
        raise MailError(f"не заданы настройки почты: {', '.join(missing)} (файл .env)")
    msg = build_message(settings, subject, text, html_body)

    security = settings.smtp_security or ("ssl" if settings.smtp_port == 465 else "starttls")
    context = ssl.create_default_context()
    try:
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
    except (smtplib.SMTPException, OSError) as e:
        raise MailError(_explain(e, settings)) from e
