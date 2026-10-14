"""robots.txt: можно ли забирать страницу автоматически."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

from .fetch import FetchError

log = logging.getLogger(__name__)
AGENT = "sitewatch"  # имя, по которому сайт может адресовать правила именно этой программе


class RobotsBlocked(FetchError):
    """Сайт запрещает автоматическую загрузку этого адреса в robots.txt."""


@dataclass
class Verdict:
    allowed: bool
    robots_url: str = ""
    note: str = ""  # пояснение: файла нет, он недоступен и т.п.
    crawl_delay: float | None = None


class RobotsPolicy:
    """Читает robots.txt каждого сайта один раз за запуск и отвечает, можно ли грузить адрес."""

    def __init__(self, fetcher, agent: str = AGENT):
        self.fetcher = fetcher
        self.agent = agent
        self._cache: dict[str, tuple[RobotFileParser | None, str]] = {}

    def _load(self, base: str) -> tuple[RobotFileParser | None, str]:
        if base not in self._cache:
            parser, note = None, ""
            try:
                text = self.fetcher.get(f"{base}/robots.txt")
            except FetchError as e:
                if e.status is not None and 400 <= e.status < 500:
                    note = "robots.txt нет, запретов нет"  # по стандарту 4xx означает «файла нет»
                else:
                    note = f"robots.txt недоступен ({e}), считаю, что запретов нет"
                    log.warning("%s", note)
            else:
                parser = RobotFileParser()
                parser.parse(text.splitlines())
                parser.modified()  # без этого can_fetch всегда отвечает «нельзя»
            self._cache[base] = (parser, note)
        return self._cache[base]

    def check(self, url: str) -> Verdict:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            return Verdict(True, note="локальный файл")
        base = f"{parts.scheme}://{parts.netloc}"
        parser, note = self._load(base)
        if parser is None:
            return Verdict(True, f"{base}/robots.txt", note)
        return Verdict(parser.can_fetch(self.agent, url), f"{base}/robots.txt", crawl_delay=parser.crawl_delay(self.agent))


class GuardedFetcher:
    """Загрузчик, который перед каждым запросом сверяется с robots.txt.

    respect — запрещённое не загружается (ошибка с пояснением); warn — предупреждение в журнале, но грузим.
    """

    def __init__(self, fetcher, policy: RobotsPolicy, mode: str):
        self.fetcher, self.policy, self.mode = fetcher, policy, mode

    def _check(self, url: str) -> None:
        verdict = self.policy.check(url)
        if verdict.allowed:
            return
        message = f"robots.txt сайта запрещает автоматическую загрузку {url}"
        if self.mode == "warn":
            log.warning("%s (режим warn: загружаю всё равно)", message)
            return
        raise RobotsBlocked(
            f"{message}. Если вы вправе забирать эту страницу, разрешите явно: robots: warn или off в источнике, "
            "либо SITEWATCH_ROBOTS=warn в .env"
        )

    def get(self, url: str) -> str:
        self._check(url)
        return self.fetcher.get(url)

    def get_bytes(self, url: str) -> bytes:
        self._check(url)
        return self.fetcher.get_bytes(url)
