"""Новые типы источников (Telegram, text, file://), выключенные источники и их проверка."""

import json
from pathlib import Path

import pytest

from sitewatch import cli
from sitewatch.config import ConfigError, load_sources
from sitewatch.doctor import check_sources, doctor
from sitewatch.extract import ExtractError, extract_text_file, is_file_url, local_path, parse_telegram
from sitewatch.models import Scope
from sitewatch.pipeline import SourceError, check_source, run
from tests.helpers import FIXTURES, FakeAnalyzer, FakeFetcher, Mailbox, make_settings, make_source, make_store

TG_URL = "https://t.me/s/oms_mdlp_notif"


def tg_message(post_id, text_html, when="2026-10-10T09:15:00+00:00", reply=""):
    return f"""
<div class="tgme_widget_message_wrap js-widget_message_wrap">
 <div class="tgme_widget_message text_not_supported_wrap js-widget_message" data-post="oms_mdlp_notif/{post_id}" data-view="abc">
  <div class="tgme_widget_message_bubble">{reply}
   {f'<div class="tgme_widget_message_text js-message_text" dir="auto">{text_html}</div>' if text_html else '<div class="tgme_widget_message_photo_wrap"></div>'}
   <div class="tgme_widget_message_footer"><a class="tgme_widget_message_date" href="https://t.me/oms_mdlp_notif/{post_id}"><time datetime="{when}" class="time">12:15</time></a></div>
  </div>
 </div>
</div>"""


def tg_page(*messages):
    return "<html><body><section class='tgme_channel_history'>" + "".join(messages) + "</section></body></html>"


PAGE = tg_page(
    tg_message(100, "Старое сообщение о релизе.", when="2026-10-01T08:00:00+00:00"),
    tg_message(101, 'Плановые технические работы 15.10 с 02:00 до 04:00.<br/>API будет недоступно.<br/><a href="https://example.com/info">Подробности</a>'),
    tg_message(102, "Работы завершены, сервисы доступны.", reply='<a class="tgme_widget_message_reply" href="https://t.me/oms_mdlp_notif/101"><div class="tgme_widget_message_text">Ответ на старое</div></a>'),
    tg_message(103, ""),  # только картинка, текста нет
)


def tg_source(**kw):
    params = dict(id="mdlp-tg", name="МДЛП", type="telegram", url=TG_URL, link_pattern=None, initial_notify=1, recheck_latest=3)
    params.update(kw)
    return make_source(**params)


# --- Telegram ---


def test_parse_telegram_orders_newest_first_and_extracts_clean_text():
    messages = parse_telegram(PAGE)
    assert [m[0] for m in messages] == ["https://t.me/oms_mdlp_notif/102", "https://t.me/oms_mdlp_notif/101", "https://t.me/oms_mdlp_notif/100"]
    url102, title102, text102 = messages[0]
    assert text102 == "Работы завершены, сервисы доступны." and title102.startswith("10.10.2026: Работы завершены")  # не текст цитаты-ответа
    assert messages[1][2].splitlines() == [
        "Плановые технические работы 15.10 с 02:00 до 04:00.", "API будет недоступно.", "Подробности [https://example.com/info]",
    ]  # <br> стал переводом строки, адрес ссылки не потерялся
    assert parse_telegram("<html>канал закрыт</html>") == []


def test_telegram_first_run_notifies_newest_then_only_new_and_edited_messages():
    store, mailbox = make_store(), Mailbox()

    def do_run(page):
        return run([tg_source()], fetcher=FakeFetcher({TG_URL: page}), store=store, analyzer=FakeAnalyzer(), mailer=mailbox, settings=make_settings())

    first = do_run(PAGE)
    assert [c.record.title[:12] for c in first.changes] == ["10.10.2026: "] and first.changes[0].record.url.endswith("/102")
    assert store.known_urls("mdlp-tg") == {f"https://t.me/oms_mdlp_notif/{i}" for i in (100, 101, 102)}
    assert do_run(PAGE).changes == []  # ничего нового

    newer = tg_page(tg_message(101, "Старое, без правок"), tg_message(104, "Срочно: меняется хост API."))
    news = do_run(newer)
    assert [c.record.url.rsplit("/", 1)[1] for c in news.changes if c.kind == "new"] == ["104"]

    edited = tg_page(tg_message(104, "Срочно: меняется хост API. Новый адрес: api2.example."))
    updated = do_run(edited)
    assert [(c.kind, c.record.url.rsplit("/", 1)[1]) for c in updated.changes] == [("updated", "104")]
    assert "+Срочно: меняется хост API. Новый адрес: api2.example." in updated.changes[0].diff


def test_telegram_empty_page_explains_what_to_check():
    with pytest.raises(SourceError, match="https://t.me/s/"):
        check_source(tg_source(), FakeFetcher({TG_URL: "<html><body>Если у вас Telegram, откройте канал</body></html>"}), make_store())


# --- text и OpenAPI ---


def test_json_is_canonical_so_reordering_is_not_a_change():
    a = json.dumps({"openapi": "3.0.0", "info": {"title": "TRUE API", "version": "4.19"}, "paths": {"/a": {}, "/b": {}}})
    b = json.dumps({"paths": {"/b": {}, "/a": {}}, "info": {"version": "4.19", "title": "TRUE API"}, "openapi": "3.0.0"}, indent=4)
    (title_a, text_a), (title_b, text_b) = extract_text_file(a, "x.json"), extract_text_file(b, "x.json")
    assert text_a == text_b and title_a == title_b == "TRUE API 4.19"


def test_plain_text_is_normalized_and_empty_file_is_an_error():
    title, text = extract_text_file("\ufeffстрока 1   \r\nстрока 2\r\n\r\n", "https://x.ru/files/spec%20v2.txt")
    assert text == "строка 1\nстрока 2" and title == "spec v2.txt"
    with pytest.raises(ExtractError, match="пустой"):
        extract_text_file("  \n", "x.txt")


def test_local_spec_copy_is_monitored_through_file_url(tmp_path):
    spec = tmp_path / "trueapi.json"
    spec.write_text(json.dumps({"info": {"title": "TRUE API", "version": "4.19"}, "paths": {"/auth": {}}}), encoding="utf-8")
    source = make_source(id="spec", type="text", url=f"file://{spec}", link_pattern=None, initial_notify=0)
    assert is_file_url(source.url) and local_path(source.url) == spec
    store, mailbox = make_store(), Mailbox()

    def do_run():
        return run([source], fetcher=FakeFetcher({}), store=store, analyzer=FakeAnalyzer(), mailer=mailbox, settings=make_settings(robots_mode="respect"))

    assert do_run().changes == []  # первый запуск молчит; сеть не нужна, robots.txt не спрашивался
    spec.write_text(json.dumps({"info": {"title": "TRUE API", "version": "4.20"}, "paths": {"/auth": {}, "/cises/info": {}}}), encoding="utf-8")
    report = do_run()
    assert [c.kind for c in report.changes] == ["updated"]
    diff = report.changes[0].diff
    assert '+    "/cises/info": {}' in diff and '+    "version": "4.20"' in diff  # разница по смыслу: новый метод и новая версия


def test_missing_local_file_and_local_pdf(tmp_path):
    with pytest.raises(SourceError, match="не удалось прочитать файл"):
        check_source(make_source(type="text", url=f"file://{tmp_path}/нет.json", link_pattern=None), FakeFetcher({}), make_store())

    pdf = make_source(type="pdf", url=f"file://{FIXTURES / 'guide_v1.pdf'}", link_pattern=None, initial_notify=1)
    result = check_source(pdf, FakeFetcher({}), make_store())
    assert "версия 4.19" in result.changes[0].record.title


def test_file_url_survives_config_normalization(tmp_path):
    config = tmp_path / "s.yaml"
    config.write_text('sources:\n  - {id: spec, type: text, url: "file:///C:/specs/trueapi.json"}\n', encoding="utf-8")
    assert load_sources(config)[0].url == "file:///C:/specs/trueapi.json"


# --- enabled / robots в конфиге ---


def write(tmp_path, text):
    path = tmp_path / "sources.yaml"
    path.write_text(text, encoding="utf-8")
    return str(path)


# id «on»/«off» в YAML без кавычек превращаются в true/false, поэтому в примерах другие имена
TWO = "sources:\n  - {id: alpha, type: page, url: 'https://a.ru/'}\n  - {id: beta, type: page, url: 'https://b.ru/', enabled: false}\n"


def test_disabled_sources_are_skipped_by_run_but_available_for_checking(tmp_path, capsys):
    config = write(tmp_path, TWO)
    assert [s.id for s in cli._select_sources(config, None)] == ["alpha"]  # боевой запуск: только включённые
    assert [s.id for s in cli._select_sources(config, ["beta"])] == ["beta"]  # явно названный проверить можно
    assert [s.id for s in cli._select_sources(config, None, include_disabled=True)] == ["alpha", "beta"]  # check-sources видит все

    all_off = write(tmp_path, "sources:\n  - {id: x, type: page, url: 'https://a.ru/', enabled: false}\n")
    assert cli._select_sources(all_off, None) is None and "выключены" in capsys.readouterr().err


def test_robots_option_is_validated(tmp_path):
    ok = write(tmp_path, "sources:\n  - {id: a, type: page, url: 'https://a.ru/', robots: warn}\n")
    assert load_sources(ok)[0].robots == "warn"
    with pytest.raises(ConfigError, match="robots должен быть"):
        load_sources(write(tmp_path, "sources:\n  - {id: a, type: page, url: 'https://a.ru/', robots: whatever}\n"))


# --- диагностика ---


def test_check_sources_marks_disabled_and_previews_telegram(capsys):
    source = tg_source(enabled=False)
    assert check_sources([source], FakeFetcher({TG_URL: PAGE})) == 0
    out = capsys.readouterr().out
    assert "(ВЫКЛЮЧЕН)" in out and "сообщений с текстом на странице: 3" in out and "Работы завершены" in out

    assert check_sources([tg_source()], FakeFetcher({TG_URL: "<html></html>"})) == 1
    assert "https://t.me/s/" in capsys.readouterr().out


def test_doctor_shows_topic_glossary_and_disabled_sources(tmp_path, capsys):
    (tmp_path / "glossary.yaml").write_text("entries:\n  - {name: Авторизация, where: ПолучитьТокен}\n  - {name: Отчёт}\n", encoding="utf-8")
    config = tmp_path / "sources.yaml"
    config.write_text("scope: {interests: Мясо и рыба, glossary: glossary.yaml}\n" + TWO, encoding="utf-8")
    settings = make_settings(db_path=str(tmp_path / "s.db"), smtp_user="u")
    doctor(settings, str(config), str(tmp_path / ".env"), online=False)
    out = capsys.readouterr().out
    assert "в письма попадает только: Мясо и рыба" in out and "записей 2, с указанием места правки 1" in out
    assert "не заполнено where" in out and "[ВЫКЛЮЧЕН: enabled: false]" in out and "включено 1" in out

    plain = tmp_path / "plain.yaml"
    plain.write_text(TWO, encoding="utf-8")
    doctor(settings, str(plain), str(tmp_path / ".env"), online=False)
    assert "фильтра по теме нет" in capsys.readouterr().out
