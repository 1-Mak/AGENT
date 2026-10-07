"""Генератор тестовых PDF (запускается вручную, результат лежит в репозитории рядом).

Нужен reportlab: pip install reportlab. Сами тесты reportlab не требуют — они читают готовые файлы.
    python tests/fixtures/make_pdfs.py
"""

from pathlib import Path

from pypdf import PdfWriter
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

HERE = Path(__file__).parent
FONT = "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"
PAGES = 8


def body(page: int, version: int) -> list[str]:
    lines = [f"Раздел {page}. Описание метода обмена данными.", "Участник оборота передаёт сведения в систему."]
    if page == 3:
        deadline = "01.05.2026" if version == 1 else "01.07.2026"
        lines.append(f"Срок обязательного перехода на формат 1.4 — {deadline}.")
    if page == 5 and version == 2:
        lines.append("Добавлено новое необязательное поле: страна происхождения товара.")
    return lines


def build(path: Path, version: int, header: str = "Руководство участника оборота. Честный ЗНАК") -> None:
    pdfmetrics.registerFont(TTFont("Liberation", FONT))
    pdf = canvas.Canvas(str(path), pagesize=A4)
    for page in range(1, PAGES + 1):
        pdf.setFont("Liberation", 10)
        pdf.drawString(50, 800, header)  # колонтитул на каждой странице
        pdf.setFont("Liberation", 12)
        if page == 1:
            pdf.drawString(50, 740, "Спецификация обмена данными, версия 4.19" if version == 1 else "Спецификация обмена данными, версия 4.20")
        y = 700
        for line in body(page, version):
            pdf.drawString(50, y, line)
            y -= 20
        pdf.setFont("Liberation", 10)
        pdf.drawString(250, 40, f"Страница {page} из {PAGES}")
        pdf.showPage()
    pdf.save()


def build_scan(path: Path) -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)  # страница без текстового слоя, как у скана
    with path.open("wb") as fh:
        writer.write(fh)


if __name__ == "__main__":
    build(HERE / "guide_v1.pdf", 1)
    build(HERE / "guide_v2.pdf", 2)
    build(HERE / "guide_v1_header.pdf", 1, header="Руководство участника оборота. Честный ЗНАК. Версия 4.20")  # тот же текст, другой колонтитул
    build_scan(HERE / "scan.pdf")
    print("готово:", *sorted(p.name for p in HERE.glob("*.pdf")))
