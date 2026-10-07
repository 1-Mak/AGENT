"""Fetcher: размер файлов, повторы, кодировки (настоящие requests.Response, подменена только сессия)."""

import io

import pytest
import requests

from sitewatch.fetch import FetchError, Fetcher


def response(body: bytes, status=200, headers=None) -> requests.Response:
    resp = requests.Response()
    resp.status_code = status
    resp.raw = io.BytesIO(body)
    resp.headers.update(headers or {})
    return resp


class FakeSession:
    def __init__(self, *items):
        self.items = list(items)
        self.calls = []

    def get(self, url, timeout=None, stream=False):
        self.calls.append(url)
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr("sitewatch.fetch.time.sleep", lambda s: None)


def make_fetcher(*items, **kw):
    fetcher = Fetcher("test-agent", delay=0, **kw)
    fetcher.session = FakeSession(*items)
    return fetcher


def test_get_bytes_returns_file_as_is():
    data = b"%PDF-1.4\n" + bytes(range(256)) * 10
    assert make_fetcher(response(data)).get_bytes("https://x.ru/a.pdf") == data


def test_get_decodes_windows_1251_pages_without_charset():
    html = "<html><body><h1>Что нового в системе</h1><p>Срок перехода перенесён на первое июля.</p></body></html>"
    text = make_fetcher(response(html.encode("cp1251"), headers={"Content-Type": "text/html"})).get("https://x.ru/")
    assert "Что нового в системе" in text and "Срок перехода" in text


def test_oversized_file_is_refused_by_declared_length_without_downloading():
    big = response(b"x" * 10, headers={"Content-Length": str(5 * 1024 * 1024)})
    fetcher = make_fetcher(big, max_bytes=1024 * 1024)
    with pytest.raises(FetchError, match="больше лимита"):
        fetcher.get_bytes("https://x.ru/huge.pdf")
    assert len(fetcher.session.calls) == 1  # не повторяем: файл от этого не уменьшится


def test_oversized_file_without_content_length_is_cut_off_while_streaming():
    fetcher = make_fetcher(response(b"x" * (3 * 1024 * 1024)), max_bytes=1024 * 1024)
    with pytest.raises(FetchError, match="больше лимита"):
        fetcher.get_bytes("https://x.ru/huge.pdf")


def test_client_errors_are_not_retried_and_server_errors_are():
    not_found = make_fetcher(response(b"no", status=404))
    with pytest.raises(FetchError, match="HTTP 404"):
        not_found.get("https://x.ru/missing")
    assert len(not_found.session.calls) == 1

    flaky = make_fetcher(response(b"err", status=503), response(b"err", status=500), response(b"ok"))
    assert flaky.get("https://x.ru/") == "ok" and len(flaky.session.calls) == 3


def test_network_errors_are_retried_then_reported():
    fetcher = make_fetcher(*[requests.ConnectionError("нет сети")] * 3)
    with pytest.raises(FetchError, match="ConnectionError"):
        fetcher.get("https://x.ru/")
    assert len(fetcher.session.calls) == 3
