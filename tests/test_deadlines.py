"""Реестр сроков: разбор дат, пороги напоминаний, хранение, письмо и запуск."""

from datetime import date

import pytest

from sitewatch.config import ConfigError, load_milestones, load_scope
from sitewatch.deadlines import collect_deadlines, due_reminders, merge, parse_deadline, reminder_threshold, upcoming
from sitewatch.models import Analysis, Deadline, Scope
from sitewatch.notify import render
from sitewatch.pipeline import run
from tests.helpers import (
    INDEX_URL,
    FakeAnalyzer,
    FakeFetcher,
    Mailbox,
    index_html,
    item_html,
    item_url,
    make_change,
    make_settings,
    make_source,
    make_store,
)

TODAY = date(2026, 10, 14)


def dl(due, title="Срок", **kw):
    return Deadline(due, title, **kw)


# --- разбор строки со сроком ---


def test_parse_exact_date_strips_leading_date_from_title():
    assert parse_deadline("01.07.2026 — новый срок передачи сведений") == (date(2026, 7, 1), False, "новый срок передачи сведений")
    assert parse_deadline("до 1.9.2026: отключение метода")[:2] == (date(2026, 9, 1), False)


def test_parse_keeps_full_text_when_nothing_follows_the_date():
    assert parse_deadline("с 01.10.2026") == (date(2026, 10, 1), False, "с 01.10.2026")


def test_parse_month_only_is_approximate_end_of_month():
    due, approx, title = parse_deadline("отключение планируется в ноябре 2026")
    assert (due, approx) == (date(2026, 11, 30), True) and "ноябре" in title
    assert parse_deadline("в феврале 2028")[0] == date(2028, 2, 29)  # високосный год
    assert parse_deadline("в мае 2027")[0] == date(2027, 5, 31)
    assert parse_deadline("в марте 2027")[0] == date(2027, 3, 31)  # «март» не путается с «мая»


@pytest.mark.parametrize("text", ["с выходом новой версии", "", "31.02.2026 — несуществующая дата"])
def test_parse_without_usable_date_returns_none(text):
    assert parse_deadline(text) is None


def test_exact_date_wins_over_month():
    assert parse_deadline("в ноябре 2026, а именно 05.11.2026")[:2] == (date(2026, 11, 5), False)


# --- сроки из разбора документа ---


def analysis(**kw):
    base = dict(summary="x", importance="высокая", affected_components=["Отчет о нанесении (СУЗ, utilisation)"])
    base.update(kw)
    return Analysis(**base)


def change_with(a):
    c = make_change()
    c.analysis = a
    return c


def test_collect_uses_deadline_lines_and_ignores_duplicate_nearest():
    c = change_with(analysis(deadlines=["01.10.2026 — поле expDate72", "в ноябре 2026 — отключение"], nearest_deadline="01.10.2026"))
    got = collect_deadlines(c)
    assert [(d.due, d.approx) for d in got] == [(date(2026, 10, 1), False), (date(2026, 11, 30), True)]
    assert got[0].components == ("Отчет о нанесении (СУЗ, utilisation)",)
    assert got[0].url == c.record.url and c.source.name in got[0].origin


def test_collect_falls_back_to_nearest_deadline_and_skips_missing_analysis():
    assert [d.due for d in collect_deadlines(change_with(analysis(nearest_deadline="05.12.2026")))] == [date(2026, 12, 5)]
    assert collect_deadlines(make_change()) == []


# --- пороги напоминаний ---


def test_threshold_sequence_over_time():
    d = dl(date(2026, 11, 13))  # 30 дней от TODAY
    assert reminder_threshold(d, date(2026, 10, 13)) is None  # 31 день: рано
    assert reminder_threshold(d, TODAY) == 30
    d = dl(date(2026, 11, 13), reminded_at=30)
    assert reminder_threshold(d, date(2026, 10, 20)) is None  # 24 дня: порог 30 уже был
    assert reminder_threshold(d, date(2026, 10, 30)) == 14  # 14 дней
    assert reminder_threshold(dl(d.due, reminded_at=14), date(2026, 11, 13)) == 0  # в день срока


def test_threshold_picks_tightest_when_first_seen_late():
    assert reminder_threshold(dl(date(2026, 10, 19)), TODAY) == 7  # 5 дней: сразу порог 7, а не пачка писем
    assert reminder_threshold(dl(date(2026, 10, 15)), TODAY) == 1


def test_no_reminders_for_past_or_dismissed():
    assert reminder_threshold(dl(date(2026, 10, 13)), TODAY) is None
    assert reminder_threshold(dl(date(2026, 10, 20), dismissed=True), TODAY) is None


def test_upcoming_window_hides_far_future_dismissed_and_long_past():
    entries = [
        dl(date(2026, 10, 1), "прошёл недавно"), dl(date(2026, 9, 1), "давно прошёл"),
        dl(date(2026, 11, 20), "скоро"), dl(date(2027, 3, 1), "далеко"), dl(date(2026, 10, 20), "снят", dismissed=True),
    ]
    assert [d.title for d in upcoming(entries, TODAY)] == ["прошёл недавно", "скоро"]


def test_merge_keeps_reminder_state_and_updates_description():
    old = dl(date(2026, 10, 20), "Срок", note="старое", reminded_at=7, dismissed=True)
    new = dl(date(2026, 10, 20), "Срок", note="новое")
    (merged,) = merge([old], [new])
    assert (merged.note, merged.reminded_at, merged.dismissed) == ("новое", 7, True)
    assert len(merge([old], [dl(date(2026, 10, 21), "Другой")])) == 2


# --- хранение ---


def test_store_roundtrip_reminders_dismiss_and_manual_sync():
    store = make_store()
    auto = dl(date(2026, 10, 20), "Из документа", origin="Источник", url="https://x/1", components=("А", "Б"))
    manual = dl(date(2026, 11, 1), "Вручную", manual=True, note="заметка")
    store.save_deadlines([auto])
    assert store.sync_manual_deadlines([manual]) == 0

    loaded = {d.title: d for d in store.load_deadlines()}
    assert loaded["Из документа"].components == ("А", "Б") and loaded["Вручную"].manual and loaded["Вручную"].note == "заметка"

    store.mark_reminded({auto.key: 7})
    store.save_deadlines([auto])  # повторное сохранение не сбрасывает «напомнено»
    assert {d.title: d for d in store.load_deadlines()}["Из документа"].reminded_at == 7

    assert store.dismiss_deadline(auto.key[:4]) == ["Из документа"]
    assert store.dismiss_deadline("нет такого") == []
    assert {d.title: d for d in store.load_deadlines()}["Из документа"].dismissed

    assert store.sync_manual_deadlines([]) == 1  # убрали из файла: убралось и из базы
    assert [d.title for d in store.load_deadlines()] == ["Из документа"]


# --- milestones.yaml ---


def test_load_milestones_and_scope_link(tmp_path):
    (tmp_path / "m.yaml").write_text(
        "milestones:\n  - date: 01.10.2026\n    title: Поле expDate72\n    components: Отчет\n"
        "  - date: 2026-12-31\n    title: Конец года\n    approx: true\n",
        encoding="utf-8",
    )
    (tmp_path / "s.yaml").write_text(
        "scope:\n  interests: мясо\n  milestones: m.yaml\nsources:\n  - {id: a, type: page, url: 'https://e.com/'}\n", encoding="utf-8"
    )
    first, second = load_scope(tmp_path / "s.yaml").milestones
    assert (first.due, first.manual, first.components, first.origin) == (date(2026, 10, 1), True, ("Отчет",), "вручную")
    assert (second.due, second.approx) == (date(2026, 12, 31), True)


@pytest.mark.parametrize("body", ["milestones:\n  - title: без даты\n", "milestones:\n  - {date: вчера, title: x}\n"])
def test_load_milestones_rejects_bad_records(tmp_path, body):
    (tmp_path / "m.yaml").write_text(body, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_milestones(tmp_path / "m.yaml")


def test_real_milestones_file_loads():
    assert load_scope("config/sources.yaml").milestones


# --- письмо ---


def test_digest_block_lists_deadline_with_where_and_reminder_mark():
    from sitewatch.models import GlossaryEntry

    scope = Scope(interests="мясо", glossary=(GlossaryEntry("Отчет", (), "документ «Отчет о нанесении»"),))
    d = dl(date(2026, 10, 19), "поле expDate72", origin="Анонс СУЗ", components=("Отчет",), approx=True)
    subject, text, html = render([], [], scope=scope, deadlines=[d], reminded={d.key}, today=TODAY)
    assert subject == "[Мониторинг] Срок через 5 дн. (19.10.2026): поле expDate72"
    assert "Ближайшие сроки:" in text and "(напоминание)" in text and "~месяц" in text
    assert "менять: Отчет → документ «Отчет о нанесении»" in text
    assert "Ближайшие сроки" in html and "напоминание" in html


def test_alert_subject_wins_over_reminder_subject():
    d = dl(date(2026, 10, 19), "срок")
    subject, _, _ = render([], ["источник недоступен"], deadlines=[d], reminded={d.key}, today=TODAY)
    assert "Сбой" in subject


def test_past_deadline_is_flagged_for_checking():
    _, text, _ = render([], [], deadlines=[dl(date(2026, 10, 1), "уже было")], today=TODAY)
    assert "прошёл 13 дн. назад" in text and "проверьте, что сделано" in text


# --- запуск: календарь живёт между письмами ---


PAGES = {INDEX_URL: index_html([20, 13]), **{item_url(n): item_html(n) for n in (20, 13)}}


def go(store, mailbox, today, analyzer=None, scope=None, **kw):
    return run(
        [make_source()], fetcher=FakeFetcher(PAGES), store=store, analyzer=analyzer or FakeAnalyzer(), mailer=mailbox,
        settings=make_settings(), scope=scope, today=today, **kw,
    )


def test_deadline_from_document_is_registered_and_reminded_once_per_threshold():
    store, mailbox = make_store(), Mailbox()
    go(store, mailbox, TODAY, FakeAnalyzer(deadline="20.10.2026"))
    assert len(mailbox.sent) == 1 and "Ближайшие сроки:" in mailbox.sent[0][1]
    (saved,) = store.load_deadlines()
    assert saved.due == date(2026, 10, 20) and saved.reminded_at == 7

    go(store, mailbox, TODAY)  # тот же день, новых документов нет
    assert len(mailbox.sent) == 1

    report = go(store, mailbox, date(2026, 10, 17))  # осталось 3 дня: следующий порог
    assert len(mailbox.sent) == 2 and report.changes == [] and len(report.reminders) == 1
    assert mailbox.sent[1][0].startswith("[Мониторинг] Срок через 3 дн. (20.10.2026)")
    assert store.load_deadlines()[0].reminded_at == 3

    go(store, mailbox, date(2026, 10, 17))
    assert len(mailbox.sent) == 2


def test_manual_milestone_reminds_without_any_new_document():
    store, mailbox = make_store(), Mailbox()
    scope = Scope(milestones=(dl(date(2026, 10, 20), "Ручной срок", manual=True, origin="вручную"),))
    go(store, mailbox, TODAY, scope=scope)  # первый запуск: приходит письмо о самом свежем документе
    assert "Ручной срок" in mailbox.sent[0][1]
    go(store, mailbox, TODAY, scope=scope)
    assert len(mailbox.sent) == 1  # напоминание об этом пороге уже было

    go(store, mailbox, date(2026, 10, 20), scope=scope)  # в день срока
    assert len(mailbox.sent) == 2 and "сегодня" in mailbox.sent[1][0]


def test_dry_run_neither_saves_deadlines_nor_marks_reminders():
    store, mailbox = make_store(), Mailbox()
    out: list[str] = []
    go(store, mailbox, TODAY, FakeAnalyzer(deadline="20.10.2026"), dry_run=True, out=out.append)
    assert mailbox.sent == [] and store.load_deadlines() == []
    assert "Ближайшие сроки:" in out[0]


def test_failed_mail_leaves_reminders_unmarked():
    store = make_store()
    with pytest.raises(ConnectionError):
        go(store, Mailbox(fail=True), TODAY, FakeAnalyzer(deadline="20.10.2026"))
    assert store.load_deadlines() == []  # ничего не сохранено: при следующем запуске письмо уйдет снова


def test_irrelevant_documents_do_not_feed_the_calendar():
    store, mailbox = make_store(), Mailbox()
    scope = Scope(interests="мясо")
    go(store, mailbox, TODAY, FakeAnalyzer(relevance="не относится", deadline="20.10.2026"), scope=scope)
    assert store.load_deadlines() == []


def test_removed_manual_milestone_stops_reminding():
    store, mailbox = make_store(), Mailbox()
    scope = Scope(milestones=(dl(date(2026, 10, 20), "Ручной срок", manual=True),))
    go(store, mailbox, TODAY, scope=scope)
    go(store, mailbox, TODAY, scope=Scope())
    assert store.load_deadlines() == []
    go(store, mailbox, date(2026, 10, 20), scope=Scope())
    assert len(mailbox.sent) == 1
