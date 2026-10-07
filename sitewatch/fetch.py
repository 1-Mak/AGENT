"""Загрузка страниц и файлов: вежливо, с повторами, лимитом размера и правильной кодировкой."""

from __future__ import annotations

import logging
import time

import requests

log = logging.getLogger(__name__)

DEFAULT_MAX_BYTES = 50 * 1024 * 1024
CHUNK = 64 * 1024


class FetchError(Exception):
    pass


class Fetcher:
    def __init__(
        self,
        user_agent: str,
        timeout: float = 30.0,
        delay: float = 1.0,
        retries: int = 2,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ):
        self.timeout = timeout
        self.delay = delay
        self.retries = retries
        self.max_bytes = max_bytes
        self._last_request = 0.0
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml,application/pdf;q=0.9,*/*;q=0.8",
                "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.5",
            }
        )

    def _throttle(self) -> None:
        wait = self.delay - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def _load_body(self, resp: requests.Response, url: str) -> None:
        """Читает тело ответа целиком, но не больше max_bytes (защита от огромных файлов)."""
        limit_mb = self.max_bytes / (1024 * 1024)
        declared = resp.headers.get("Content-Length", "")
        if declared.isdigit() and int(declared) > self.max_bytes:
            resp.close()
            raise FetchError(f"{url}: файл {int(declared) / 1024 / 1024:.0f} МБ больше лимита {limit_mb:.0f} МБ")
        buffer = bytearray()
        for chunk in resp.iter_content(CHUNK):
            buffer.extend(chunk)
            if len(buffer) > self.max_bytes:
                resp.close()
                raise FetchError(f"{url}: файл больше лимита {limit_mb:.0f} МБ (SITEWATCH_MAX_PDF_MB)")
        resp._content = bytes(buffer)  # так requests хранит уже прочитанное тело
        resp._content_consumed = True

    def _request(self, url: str) -> requests.Response:
        last_error = "неизвестная ошибка"
        for attempt in range(self.retries + 1):
            self._throttle()
            started = time.monotonic()
            try:
                resp = self.session.get(url, timeout=self.timeout, stream=True)
                self._load_body(resp, url)
            except requests.RequestException as e:
                last_error = f"{type(e).__name__}: {e}"
                log.warning("GET %s -> ошибка: %s", url, last_error)
            else:
                log.info(
                    "GET %s -> %s, %d байт, %.1f с", url, resp.status_code, len(resp.content), time.monotonic() - started
                )
                if resp.status_code < 400:
                    return resp
                last_error = f"HTTP {resp.status_code}"
                if resp.status_code < 500 and resp.status_code != 429:
                    break  # 4xx (кроме 429) повторять бессмысленно
            if attempt < self.retries:
                pause = 2 ** (attempt + 1)
                log.info("Повтор через %s с: %s (%s)", pause, url, last_error)
                time.sleep(pause)
        raise FetchError(f"{url}: {last_error}")

    def get(self, url: str) -> str:
        """Страница как текст (HTML, XML)."""
        return _decode(self._request(url))

    def get_bytes(self, url: str) -> bytes:
        """Файл как есть (PDF)."""
        return self._request(url).content


def _decode(resp: requests.Response) -> str:
    # Для text/* без charset requests по умолчанию ставит ISO-8859-1 — для русских сайтов это мусор.
    if resp.encoding is None or resp.encoding.lower() == "iso-8859-1":
        resp.encoding = resp.apparent_encoding or "utf-8"
    return resp.text
