"""Разведка адреса: группы ссылок, готовый link_pattern, PDF, sitemap, понятные ответы на 404 и пустые страницы."""

from sitewatch import cli
from sitewatch.discover import discover_url, link_groups
from sitewatch.fetch import FetchError
from sitewatch.menu import run_menu
from tests.helpers import FakeFetcher

SITE = "https://example.org"
SECTION = f"{SITE}/community"


def page(*hrefs, pdfs=(), footer_pdf="/policy.pdf"):
    links = "".join(f'<a href="{h}">x</a>' for h in hrefs)
    files = "".join(f'<a href="{p}">файл</a>' for p in pdfs)
    return (
        "<html><head><title>Сообщество</title></head><body><nav><a href='/about/'>О нас</a></nav>"
        f"<main><h1>Сообщество</h1><p>{'Текст раздела. ' * 20}</p>{links}{files}</main>"
        f"<footer><a href='{footer_pdf}'>Политика</a></footer></body></html>"
    )


def article_links(group, n):
    return [f"/community/{group}/stat-{i}" for i in range(n)]


def run(fetcher, url=SECTION, capsys=None):
    rc = discover_url(url, fetcher)
    return rc, capsys.readouterr().out if capsys else ""


def test_groups_links_by_folder_and_suggests_a_working_pattern(capsys):
    import re

    hrefs = article_links("myaso", 12) + article_links("ikra", 5) + ["/community/developers", "https://other.site/x/y"]
    fetcher = FakeFetcher({SECTION: page(*hrefs, pdfs=["/upload/memo.pdf"]), f"{SITE}/robots.txt": "User-agent: *\nDisallow:\nSitemap: https://example.org/sm.xml\n"})
    rc, out = run(fetcher, capsys=capsys)

    assert rc == 0 and "страница получена: «Сообщество»" in out
    assert out.index("12  /community/myaso/") < out.index("5  /community/ikra/")  # самая большая группа первой
    assert "пример: stat-0" in out and "https://other.site" not in out  # чужие сайты не считаются
    assert "https://example.org/sm.xml" in out and "robots.txt: загрузка этого адреса разрешена" in out

    pattern = re.search(r"link_pattern: '(/community/myaso/[^']+)'", out).group(1)
    assert pattern == "/community/myaso/[^/?#]+/?$"
    assert re.search(pattern, f"{SITE}/community/myaso/stat-3") and not re.search(pattern, f"{SITE}/community/ikra/stat-3")


def test_pdf_links_are_split_into_main_block_and_the_rest(capsys):
    fetcher = FakeFetcher({SECTION: page(*article_links("a", 6), pdfs=["/upload/memo.pdf"])})
    _, out = run(fetcher, capsys=capsys)
    assert "всего ссылок на PDF: 2, из них в основном блоке (их возьмёт follow_pdf): 1" in out
    assert "https://example.org/upload/memo.pdf" in out and "policy.pdf" not in out.split("PDF на странице")[1]


def test_page_without_pdf_says_so(capsys):
    _, out = run(FakeFetcher({SECTION: page(*article_links("a", 6), footer_pdf="/about/")}), capsys=capsys)
    assert "PDF-ссылок нет" in out


def test_missing_page_explains_what_to_do_and_still_shows_sitemap(capsys):
    fetcher = FakeFetcher({f"{SITE}/robots.txt": "User-agent: *\nDisallow:\nSitemap: https://example.org/sm.xml\n"})  # раздела в макете нет -> 404
    rc, out = run(fetcher, capsys=capsys)
    assert rc == 1 and "HTTP 404" in out and "Страницы с таким адресом нет" in out
    assert "https://example.org/sm.xml" in out  # зацепка для type: sitemap остаётся


def test_standard_sitemap_address_is_tried_when_robots_has_none(capsys):
    fetcher = FakeFetcher({SECTION: page(*article_links("a", 6)), f"{SITE}/sitemap.xml": "<urlset/>"})
    assert "sitemap по стандартному адресу есть: https://example.org/sitemap.xml" in run(fetcher, capsys=capsys)[1]
    assert "не найден" in run(FakeFetcher({SECTION: page(*article_links("a", 6))}), capsys=capsys)[1]


def test_few_links_and_blocked_robots_are_called_out(capsys):
    fetcher = FakeFetcher({SECTION: page("/community/a/1"), f"{SITE}/robots.txt": "User-agent: *\nDisallow: /community\n"})
    _, out = run(fetcher, capsys=capsys)
    assert "ссылок мало" in out and "подгружается скриптом" in out
    assert "robots.txt сайта запрещает автоматическую загрузку этого адреса" in out


def test_thin_page_is_flagged_as_stub(capsys):
    _, out = run(FakeFetcher({SECTION: "<html><body><p>Включите JavaScript</p></body></html>"}), capsys=capsys)
    assert "текста очень мало" in out


def test_link_groups_sorts_by_size_merges_trailing_slash_and_keeps_root_pages_last():
    groups = link_groups([f"{SITE}/a/1", f"{SITE}/a/2", f"{SITE}/b/1", f"{SITE}/top", f"{SITE}/", f"{SITE}/a/2/"])
    assert [(parent, len(members)) for parent, members in groups] == [("/a", 3), ("/", 1), ("/b", 1)]  # главная страница не считается


def test_discover_is_wired_into_cli_and_menu(monkeypatch, capsys):
    fetcher = FakeFetcher({SECTION: page(*article_links("a", 6))})
    monkeypatch.setattr(cli, "Fetcher", lambda *a, **k: fetcher)
    assert cli.main(["--log-file", "", "--env", "нет.env", "discover", SECTION]) == 0
    assert "Группы ссылок" in capsys.readouterr().out
    cli.shutdown_logging()

    calls = []
    answers = iter(["14", SECTION, "", "14", "", "0"])
    run_menu(lambda argv: calls.append(argv) or 0, ["--env", ".env"], "c.yaml", ".env", "", lambda prompt="": next(answers))
    assert calls == [["--env", ".env", "discover", SECTION]]  # пустой адрес — возврат в меню без запуска
