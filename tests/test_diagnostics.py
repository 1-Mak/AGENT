"""doctor и check-sources: что видит человек при отладке."""

import re

import pytest

from sitewatch.config import Settings
from sitewatch.doctor import check_sources, doctor
from sitewatch.fetch import FetchError
from sitewatch.models import DocRecord
from sitewatch.store import Store
from tests.helpers import (
    INDEX_URL,
    FakeFetcher,
    index_html,
    item_html,
    item_url,
    make_settings,
    make_source,
)

SECRET = "sk-super-secret-key-123456"
SOURCES_YAML = """
sources:
  - id: cz
    name: Тестовый источник
    type: list
    url: https://xn--80ajghhoc2aj1c8b.xn--p1ai/info/releasenotes/
    link_pattern: '/info/releasenotes/chto-novogo-[^/?#]+/?$'
"""


@pytest.fixture
def config_file(tmp_path):
    path = tmp_path / "sources.yaml"
    path.write_text(SOURCES_YAML, encoding="utf-8")
    return str(path)


def mail_settings(tmp_path, **kw):
    return make_settings(db_path=str(tmp_path / "state.db"), smtp_user="bot@example.com", **kw)


# --- doctor ---


def test_doctor_without_mail_fails_and_never_prints_secrets(capsys, tmp_path, config_file):
    settings = Settings(llm_api_key=SECRET, smtp_password="пароль-почты-12345", db_path=str(tmp_path / "state.db"))
    rc = doctor(settings, config_file, str(tmp_path / ".env"), online=False)
    out = capsys.readouterr().out
    assert rc == 1
    assert "SMTP_HOST" in out and "MAIL_TO" in out
    assert SECRET not in out and "пароль-почты-12345" not in out
    assert "sk-" in out  # видно, что ключ задан


def test_doctor_all_good_offline(capsys, tmp_path, config_file):
    rc = doctor(mail_settings(tmp_path, llm_api_key=SECRET, smtp_password="x" * 12), config_file, str(tmp_path / ".env"), online=False)
    out = capsys.readouterr().out
    assert rc == 0 and "Критичных проблем нет" in out
    assert "базы ещё нет" in out and "cz [list]" in out


def test_doctor_warns_when_llm_key_missing_but_does_not_fail(capsys, tmp_path, config_file):
    rc = doctor(mail_settings(tmp_path, smtp_password="x" * 12), config_file, str(tmp_path / ".env"), online=False)
    out = capsys.readouterr().out
    assert rc == 0 and "DEEPSEEK_API_KEY не задан" in out


def test_doctor_reports_bad_config(capsys, tmp_path):
    rc = doctor(mail_settings(tmp_path), str(tmp_path / "нет.yaml"), str(tmp_path / ".env"), online=False)
    assert rc == 1 and "не найден" in capsys.readouterr().out


def test_doctor_shows_state_from_database(capsys, tmp_path, config_file):
    store = Store(str(tmp_path / "state.db"))
    store.save_records([DocRecord(item_url(6), "cz", "t", "h", "x"), DocRecord(item_url(13), "cz", "t", "h", "x")])
    store.mark_success("cz")
    store.record_failure("cz", "HTTP 403")
    store.close()
    doctor(mail_settings(tmp_path), config_file, str(tmp_path / ".env"), online=False)
    out = capsys.readouterr().out
    assert "известно документов: 2" in out and "сбоев подряд 1" in out and "HTTP 403" in out


def test_doctor_online_reports_unreachable_hosts(capsys, tmp_path, config_file, monkeypatch):
    calls = []

    def fake_tcp(host, port, timeout=5.0):
        calls.append((host, port))
        return (host != "smtp.example.com"), "TimeoutError: timed out"

    monkeypatch.setattr("sitewatch.doctor._tcp_check", fake_tcp)
    rc = doctor(mail_settings(tmp_path, llm_api_key=SECRET, smtp_password="x" * 12), config_file, str(tmp_path / ".env"))
    out = capsys.readouterr().out
    assert rc == 1 and "почтовый сервер: smtp.example.com:465" in out and "timed out" in out
    assert ("xn--80ajghhoc2aj1c8b.xn--p1ai", 443) in calls and ("api.deepseek.com", 443) in calls


# --- check-sources ---


def test_check_sources_list_shows_links_and_text_preview(capsys):
    pages = {INDEX_URL: index_html([20, 13, 6]), item_url(20): item_html(20)}
    rc = check_sources([make_source()], FakeFetcher(pages))
    out = capsys.readouterr().out
    assert rc == 0
    assert re.search(r"ссылок по шаблону .*: 3 ", out) and item_url(13) in out
    assert "Что нового в системе с 20.04.2026" in out and "Добавлено новое требование" in out
    assert "content_selector" in out  # подсказка, что делать с мусором в тексте


def test_check_sources_explains_empty_result_with_stub_page(capsys):
    stub = "<html><head><title>Проверка браузера</title></head><body><a href='/about/'>О нас</a></body></html>"
    rc = check_sources([make_source()], FakeFetcher({INDEX_URL: stub}))
    out = capsys.readouterr().out
    assert rc == 1
    assert "Проверка браузера" in out  # видно, что вместо списка пришла заглушка
    assert "всего ссылок на странице: 1" in out and "sitemap" in out


def test_check_sources_reports_fetch_errors_and_continues(capsys):
    good = make_source(id="good")
    bad = make_source(id="bad", url="https://example.com/bad/")
    pages = {INDEX_URL: index_html([20]), item_url(20): item_html(20), "https://example.com/bad/": FetchError("HTTP 403")}
    rc = check_sources([bad, good], FakeFetcher(pages))
    out = capsys.readouterr().out
    assert rc == 1 and "HTTP 403" in out and "Что нового в системе с 20.04.2026" in out


def test_check_sources_page_and_sitemap_types(capsys):
    page = make_source(id="p", type="page", url="https://x.ru/req/", link_pattern=None)
    sm_url = "https://x.ru/sitemap.xml"
    sitemap = make_source(id="s", type="sitemap", url=sm_url)
    sm_xml = f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>{item_url(20)}</loc></url></urlset>'
    pages = {"https://x.ru/req/": "<main><h1>Требования</h1><p>Текст страницы с требованиями.</p></main>",
             sm_url: sm_xml, item_url(20): item_html(20)}
    rc = check_sources([page, sitemap], FakeFetcher(pages))
    out = capsys.readouterr().out
    assert rc == 0 and "Требования" in out and "адресов в sitemap" in out


def test_check_sources_survives_garbage_sitemap(capsys):
    src = make_source(id="s", type="sitemap", url="https://x.ru/sitemap.xml")
    rc = check_sources([src], FakeFetcher({"https://x.ru/sitemap.xml": "<html>не xml"}))
    assert rc == 1 and "ошибка разбора" in capsys.readouterr().out
