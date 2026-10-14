"""Фильтр «только мясо и рыба»: инструкция агенту, отбор в пайплайне, письмо, таблица «что менять»."""

import json

import pytest

from sitewatch.analyze import Analyzer, build_system_prompt
from sitewatch.config import ConfigError, load_scope
from sitewatch.models import Analysis, GlossaryEntry, Scope
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
    make_analysis,
    make_change,
    make_settings,
    make_source,
    make_store,
)
from tests.test_analyze import FakeSession, completion

AUTH = GlossaryEntry("Авторизация и токен", ("авторизация", "токен"), "обработка ПолучитьТокен в ТАЛАРИКС MES")
MEAT = Scope(interests="Мясо и рыба", include_general=True, glossary=(AUTH,))
T20 = "Что нового в системе с 20.04.2026"
T13 = "Что нового в системе с 13.04.2026"


# --- инструкция агенту ---


def test_system_prompt_carries_topic_glossary_and_new_fields():
    prompt = build_system_prompt(MEAT)
    assert "Тема, которая интересует читателя: Мясо и рыба" in prompt
    assert "«не относится»" in prompt and "relevance_note" in prompt
    assert "<glossary>" in prompt and "- Авторизация и токен: авторизация, токен" in prompt
    assert "nearest_deadline" in prompt and '"affected_components": [\n    "Авторизация и токен"\n  ]' in prompt


def test_prompt_without_scope_has_no_topic_filter():
    prompt = build_system_prompt(Scope())
    assert "Тема, которая интересует" not in prompt and "<glossary>" not in prompt
    assert "nearest_deadline" in prompt  # срок в теме письма нужен всегда


def test_general_changes_can_be_switched_off_in_the_prompt():
    on = build_system_prompt(Scope(interests="Мясо", include_general=True))
    off = build_system_prompt(Scope(interests="Мясо", include_general=False))
    assert "касается всех товарных групп" in on
    assert "значение «общее» не используй" in off and "касается всех товарных групп и системы" not in off


def test_analyzer_sends_scope_prompt_and_keeps_only_known_components():
    answer = {
        "summary": "Новый срок", "importance": "высокая", "relevance": "Прямо", "nearest_deadline": "01.09.2026",
        "affected_components": ["авторизация и токен", "Придуманный компонент", "Авторизация и токен"],
    }
    session = FakeSession(completion(json.dumps(answer, ensure_ascii=False)))
    result = Analyzer("sk", session=session, scope=MEAT).analyze(make_change())
    assert result.relevance == "прямо" and result.nearest_deadline == "01.09.2026"
    assert result.affected_components == ["Авторизация и токен"]  # выдуманное убрано, регистр исправлен, дубль схлопнут
    assert "Мясо и рыба" in session.requests[0]["json"]["messages"][0]["content"]


def test_without_glossary_agent_components_are_dropped():
    session = FakeSession(completion(json.dumps({"summary": "x", "importance": "низкая", "affected_components": ["Что-то"]})))
    assert Analyzer("sk", session=session).analyze(make_change()).affected_components == []


@pytest.mark.parametrize("raw, expected", [("Не относится к теме", "не относится"), ("НЕ ОТНОСИТСЯ", "не относится"), ("Прямо", "прямо"), (" общее ", "общее")])
def test_relevance_is_normalized(raw, expected):
    assert Analysis(summary="x", importance="низкая", relevance=raw).relevance == expected


# --- отбор в пайплайне ---


def pages(numbers=(20, 13)):
    data = {INDEX_URL: index_html(list(numbers))}
    data.update({item_url(n): item_html(n) for n in numbers})
    return data


def do_run(analyzer, scope, mailbox, store=None, source=None, **kw):
    store = store or make_store()
    report = run([source or make_source(initial_notify=2)], fetcher=FakeFetcher(pages()), store=store, analyzer=analyzer,
                 mailer=mailbox, settings=make_settings(), scope=scope, **kw)
    return report, store


def test_irrelevant_documents_stay_out_of_the_letter_but_are_listed_in_the_footer():
    mailbox = Mailbox()
    report, _ = do_run(FakeAnalyzer(relevance_by_title={T20: "не относится"}), MEAT, mailbox)
    assert [c.record.title for c in report.changes] == [T13] and [c.record.title for c in report.filtered] == [T20]
    subject, text, html_body = mailbox.sent[0]
    assert T13 in subject and T20 not in subject
    assert "Не вошло в письмо как не относящееся к теме: 1" in text and T20 in text  # отфильтрованное видно в подвале


def test_when_everything_is_irrelevant_no_letter_is_sent_but_state_is_saved():
    mailbox, store = Mailbox(), make_store()
    report, _ = do_run(FakeAnalyzer(relevance="не относится"), MEAT, mailbox, store)
    assert mailbox.sent == [] and report.changes == [] and len(report.filtered) == 2
    assert store.known_urls("cz-releases") == {item_url(20), item_url(13)}  # не повторится завтра

    report2 = run([make_source(initial_notify=2)], fetcher=FakeFetcher(pages()), store=store, analyzer=FakeAnalyzer(),
                  mailer=mailbox, settings=make_settings(), scope=MEAT)
    assert report2.changes == [] and mailbox.sent == []


def test_general_changes_follow_the_include_general_switch():
    analyzer = FakeAnalyzer(relevance="общее")
    kept, _ = do_run(analyzer, Scope(interests="Мясо", include_general=True), Mailbox())
    assert len(kept.changes) == 2 and kept.filtered == []

    dropped, _ = do_run(FakeAnalyzer(relevance="общее"), Scope(interests="Мясо", include_general=False), Mailbox())
    assert dropped.changes == [] and len(dropped.filtered) == 2


def test_no_topic_means_no_filtering():
    report, _ = do_run(FakeAnalyzer(relevance="не относится"), Scope(), Mailbox())
    assert len(report.changes) == 2 and report.filtered == []


def test_documents_without_analysis_are_kept_not_silently_dropped():
    class Silent:
        disabled_reason = None

        def analyze(self, change):
            return None

    report, _ = do_run(Silent(), MEAT, Mailbox())
    assert len(report.changes) == 2 and report.filtered == []  # без разбора судить о теме нельзя — показываем


# --- письмо ---


def test_subject_shows_deadline_and_affected_components():
    change = make_change(title="Новый срок по токенам")
    change.analysis = make_analysis()
    change.analysis.nearest_deadline = "01.09.2026"
    change.analysis.affected_components = ["Авторизация и токен"]
    subject, text, html_body = render([change], [], scope=MEAT)
    assert subject == "[Мониторинг] срок: 01.09.2026, затронуто: авторизация и токен — Новый срок по токенам"
    assert "Что менять в системе:" in text and "Авторизация и токен → обработка ПолучитьТокен в ТАЛАРИКС MES" in text
    assert "Авторизация и токен" in html_body

    only_deadline = make_change(title="Срок")
    only_deadline.analysis = make_analysis()
    only_deadline.analysis.nearest_deadline = "01.09.2026"
    assert render([only_deadline], [])[0] == "[Мониторинг] срок: 01.09.2026 — Срок"

    other = make_change(title="Другое")
    other.analysis = make_analysis()
    assert render([change, other], [], scope=MEAT)[0].startswith("[Мониторинг] Обновлений: 2 — срок: 01.09.2026, затронуто:")


def test_component_without_where_is_shown_by_name_only_and_topic_label_appears():
    scope = Scope(interests="Мясо", glossary=(GlossaryEntry("Отчёт о нанесении"),))
    change = make_change()
    change.analysis = make_analysis()
    change.analysis.affected_components = ["Отчёт о нанесении"]
    change.analysis.relevance = "общее"
    change.analysis.relevance_note = "отобраны пункты про общие форматы"
    _, text, _ = render([change], [], scope=scope)
    assert " - Отчёт о нанесении\n" in text and "→" not in text
    assert "общее изменение" in text and "Отбор по теме: отобраны пункты про общие форматы" in text


# --- конфигурация ---


def write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_load_scope_reads_topic_and_glossary_relative_to_config(tmp_path):
    write(tmp_path, "glossary.yaml", "entries:\n  - {name: Авторизация, terms: [токен, auth], where: 'ПолучитьТокен'}\n  - {name: Отчёт}\n")
    config = write(tmp_path, "sources.yaml", "scope:\n  interests: >\n    Мясо\n    и рыба\n  include_general: false\n  glossary: glossary.yaml\nsources: []\n")
    scope = load_scope(config)
    assert scope.interests == "Мясо и рыба" and scope.include_general is False
    assert scope.glossary == (GlossaryEntry("Авторизация", ("токен", "auth"), "ПолучитьТокен"), GlossaryEntry("Отчёт"))


def test_load_scope_defaults_and_errors(tmp_path):
    assert load_scope(write(tmp_path, "a.yaml", "sources: []\n")) == Scope()  # блока нет — фильтра нет
    with pytest.raises(ConfigError, match="файл не найден"):
        load_scope(write(tmp_path, "b.yaml", "scope: {glossary: нет.yaml}\n"))
    write(tmp_path, "bad.yaml", "entries:\n  - {terms: [x]}\n")
    with pytest.raises(ConfigError, match="без поля name"):
        load_scope(write(tmp_path, "c.yaml", "scope: {glossary: bad.yaml}\n"))


def test_shipped_config_is_valid_and_meat_and_fish_focused():
    from pathlib import Path

    from sitewatch.config import load_sources

    root = Path(__file__).resolve().parent.parent / "config" / "sources.yaml"
    scope, sources = load_scope(root), load_sources(root)
    assert "Мясо" in scope.interests and "рыба" in scope.interests and len(scope.glossary) >= 5
    assert [s.id for s in sources if s.enabled] == ["chestny-znak-releases"]  # непроверенные адреса выключены
    assert {s.type for s in sources} >= {"list", "page", "telegram"}
