"""Загрузка страниц настоящим браузером (Playwright) для сайтов, где список подгружается скриптом
или обычный запрос режется антиботом (форумы, личные кабинеты без входа).

Включается на уровне источника: `fetch: browser`. Остальные источники по-прежнему идут через requests.
Playwright нужен только им: pip install playwright (отдельный браузер скачивать не обязательно, см. SITEWATCH_BROWSER).
"""

from __future__ import annotations

import logging
import sys
import time

from .fetch import FetchError

log = logging.getLogger(__name__)

# Порядок подбора, если SITEWATCH_BROWSER не задан: на Windows Edge есть всегда, поэтому качать ничего не нужно.
AUTO_CHANNELS = ("msedge", "chrome", "chromium") if sys.platform.startswith("win") else ("chrome", "chromium", "msedge")
SETTLE_MS = 4000  # сколько ждём затихания сетевых запросов после загрузки (страница может дорисовываться)


class BrowserUnavailable(FetchError):
    """Браузер не запустился: нет Playwright или подходящего браузера."""


class BrowserFetcher:
    """Тот же интерфейс, что у Fetcher: get(url) -> HTML после выполнения скриптов, get_bytes(url) -> файл."""

    def __init__(self, user_agent: str, timeout: float = 30.0, delay: float = 1.0, channel: str = "", headless: bool = True):
        self.user_agent, self.timeout, self.delay = user_agent, timeout, delay
        self.channel, self.headless = channel, headless
        self._pw = self._browser = self._context = None
        self._last = 0.0

    # --- запуск ---

    def _start(self) -> None:
        if self._context is not None:
            return
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            raise BrowserUnavailable(
                "для fetch: browser нужен пакет playwright (команда: pip install playwright). "
                "Скачивать браузер не обязательно: на Windows используется установленный Edge или Chrome"
            ) from e
        self._pw = sync_playwright().start()
        tried: list[str] = []
        for channel in [self.channel] if self.channel else AUTO_CHANNELS:
            try:
                kwargs = {"headless": self.headless}
                if channel != "chromium":
                    kwargs["channel"] = channel
                self._browser = self._pw.chromium.launch(**kwargs)
                log.info("Браузер для fetch: browser запущен (%s)", channel)
                break
            except Exception as e:  # noqa: BLE001 — Playwright кидает свои исключения; для пользователя важен только итог
                tried.append(f"{channel}: {str(e).splitlines()[0][:120]}")
        if self._browser is None:
            self.close()
            raise BrowserUnavailable(
                "не удалось запустить браузер (" + "; ".join(tried) + "). Задайте SITEWATCH_BROWSER=msedge или chrome, "
                "либо выполните: python -m playwright install chromium"
            )
        self._context = self._browser.new_context(user_agent=self.user_agent, locale="ru-RU")

    def close(self) -> None:
        for obj, method in ((self._context, "close"), (self._browser, "close"), (self._pw, "stop")):
            try:
                if obj is not None:
                    getattr(obj, method)()
            except Exception:  # noqa: BLE001 — закрываем молча: при выходе важнее не упасть
                pass
        self._pw = self._browser = self._context = None

    def _throttle(self) -> None:
        wait = self.delay - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    # --- загрузка ---

    def get(self, url: str) -> str:
        self._start()
        self._throttle()
        page = self._context.new_page()
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=self.timeout * 1000)
            if response is not None and response.status >= 400:
                raise FetchError(f"{url}: HTTP {response.status}", response.status)
            try:
                page.wait_for_load_state("networkidle", timeout=SETTLE_MS)
            except Exception:  # noqa: BLE001 — вечные фоновые запросы (аналитика, чат): берём то, что успело нарисоваться
                pass
            html = page.content()
            log.info("BROWSER %s -> %d символов", url, len(html))
            return html
        except FetchError:
            raise
        except Exception as e:  # noqa: BLE001
            raise FetchError(f"{url}: браузер не смог открыть страницу ({type(e).__name__}: {str(e).splitlines()[0][:160]})") from e
        finally:
            page.close()

    def get_bytes(self, url: str) -> bytes:
        """Файл (PDF) скачивается через сессию браузера: те же куки, что у открытых страниц."""
        self._start()
        self._throttle()
        try:
            response = self._context.request.get(url, timeout=self.timeout * 1000)
        except Exception as e:  # noqa: BLE001
            raise FetchError(f"{url}: браузер не смог скачать файл ({type(e).__name__}: {str(e).splitlines()[0][:160]})") from e
        if response.status >= 400:
            raise FetchError(f"{url}: HTTP {response.status}", response.status)
        return response.body()


class LazyBrowser:
    """Браузер запускается при первом обращении: если нет источников с fetch: browser, он не стартует вообще."""

    def __init__(self, **kwargs):
        self._kwargs = kwargs
        self._fetcher: BrowserFetcher | None = None

    def _get(self) -> BrowserFetcher:
        if self._fetcher is None:
            self._fetcher = BrowserFetcher(**self._kwargs)
        return self._fetcher

    def get(self, url: str) -> str:
        return self._get().get(url)

    def get_bytes(self, url: str) -> bytes:
        return self._get().get_bytes(url)

    def close(self) -> None:
        if self._fetcher is not None:
            self._fetcher.close()
            self._fetcher = None
