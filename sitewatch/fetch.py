"""Загрузка страниц: вежливо, с повторами и правильной кодировкой."""

from __future__ import annotations

import logging
import time

import requests

log = logging.getLogger(__name__)


class FetchError(Exception):
    pass


class Fetcher:
    def __init__(self, user_agent: str, timeout: float = 30.0, delay: float = 1.0, retries: int = 2):
        self.timeout = timeout
        self.delay = delay
        self.retries = retries
        self._last_request = 0.0
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.5",
            }
        )

    def _throttle(self) -> None:
        wait = self.delay - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def get(self, url: str) -> str:
        last_error = "неизвестная ошибка"
        for attempt in range(self.retries + 1):
            self._throttle()
            try:
                resp = self.session.get(url, timeout=self.timeout)
            except requests.RequestException as e:
                last_error = f"{type(e).__name__}: {e}"
            else:
                if resp.status_code < 400:
                    return _decode(resp)
                last_error = f"HTTP {resp.status_code}"
                if resp.status_code < 500 and resp.status_code != 429:
                    break  # 4xx (кроме 429) повторять бессмысленно
            if attempt < self.retries:
                pause = 2 ** (attempt + 1)
                log.info("Повтор через %s с: %s (%s)", pause, url, last_error)
                time.sleep(pause)
        raise FetchError(f"{url}: {last_error}")


def _decode(resp: requests.Response) -> str:
    # Для text/* без charset requests по умолчанию ставит ISO-8859-1 — для русских сайтов это мусор.
    if resp.encoding is None or resp.encoding.lower() == "iso-8859-1":
        resp.encoding = resp.apparent_encoding or "utf-8"
    return resp.text
