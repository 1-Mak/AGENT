"""Счётчики ошибок в журнале интеграции: чтение дописанного, ротация, рост относительно обычного уровня."""

import re

import pytest

from sitewatch.config import ConfigError
from sitewatch.logwatch import (
    LogSignature,
    evaluate,
    expand_paths,
    load_logwatch,
    run_logwatch,
    scan_log,
)
from tests.helpers import make_store

CONFIG = """
logs:
  - id: integration
    name: Журнал интеграции
    paths: ['{dir}/chz*.log']
    signatures:
      - {{id: deprecated, name: Устаревшее API, pattern: 'устаревшее api'}}
      - {{id: token, name: Токен не действителен, pattern: 'Токен не действителен', min_count: 5, growth: 3}}
"""


@pytest.fixture
def spec(tmp_path):
    path = tmp_path / "logwatch.yaml"
    path.write_text(CONFIG.format(dir=tmp_path.as_posix()), encoding="utf-8")
    return load_logwatch(path)[0]


def append(tmp_path, name, *lines, encoding="utf-8"):
    with (tmp_path / name).open("ab") as f:
        for line in lines:
            f.write((line + "\n").encode(encoding))


def day(spec, store, commit=True):
    result = run_logwatch([spec], store)
    if commit:
        result.commit()
    return result


def titles(result):
    return [s.title for s in result.signals]


# --- конфигурация ---


def test_load_logwatch_reads_paths_signatures_and_defaults(spec):
    assert (spec.id, spec.name, spec.encoding) == ("integration", "Журнал интеграции", "auto")
    deprecated, token = spec.signatures
    assert (deprecated.min_count, deprecated.growth) == (1, 3.0) and (token.min_count, token.growth) == (5, 3.0)
    assert deprecated.regex.search("ВНИМАНИЕ: Устаревшее API v3")  # регистр не важен


@pytest.mark.parametrize(
    "text, message",
    [
        ("logs:\n  - {id: a}\n", "id и paths"),
        ("logs:\n  - {id: a, paths: [x]}\n", "signatures"),
        ("logs:\n  - {id: a, paths: [x], signatures: [{id: s}]}\n", "id и pattern"),
        ("logs:\n  - {id: a, paths: [x], signatures: [{id: s, pattern: '['}]}\n", "регулярное выражение"),
        ("logs:\n  - {id: a, paths: [x], signatures: [{id: s, pattern: p}, {id: s, pattern: q}]}\n", "повторяющийся id шаблона"),
        ("logs:\n  - {id: a, paths: x, signatures: [{id: s, pattern: p}]}\n  - {id: a, paths: x, signatures: [{id: s, pattern: p}]}\n", "повторяющийся id"),
        ("logs: nope\n", "список logs"),
    ],
)
def test_load_logwatch_rejects_bad_files(tmp_path, text, message):
    path = tmp_path / "l.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match=message):
        load_logwatch(path)


# --- порог ---


def test_threshold_is_min_count_when_there_is_no_history_and_grows_with_the_usual_level():
    sig = LogSignature("s", "s", re.compile("x"), min_count=5, growth=3)
    assert evaluate(sig, 4, []) == (False, 0.0, 5)
    assert evaluate(sig, 5, [])[0] is True
    assert evaluate(sig, 29, [10, 10])[0] is False and evaluate(sig, 30, [10, 10]) == (True, 10.0, 30)
    assert evaluate(sig, 0, [])[0] is False  # ноль никогда не сигнал, даже если порог 0


# --- чтение журнала ---


def test_first_run_ignores_history_and_only_remembers_where_the_log_ends(spec, tmp_path):
    append(tmp_path, "chz.log", *["Устаревшее API"] * 50)
    first = day(spec, make_store())
    assert first.signals == [] and any("первый запуск" in n for n in first.notes)


def test_only_lines_written_since_the_last_run_are_counted(spec, tmp_path):
    store = make_store()
    append(tmp_path, "chz.log", "старая строка: Устаревшее API")
    day(spec, store)

    append(tmp_path, "chz.log", "2026-10-14 ошибка: Устаревшее API (метод cises/info)", "обычная строка")
    result = day(spec, store)
    assert titles(result) == ["Журнал интеграции: Устаревшее API"]
    assert "1 записей с прошлой проверки, раньше не встречалось; файлы: chz.log" in result.signals[0].message

    assert day(spec, store).signals == []  # строки учтены один раз


def test_log_lines_never_reach_the_signal(spec, tmp_path):
    store = make_store()
    append(tmp_path, "chz.log", "начало")
    day(spec, store)
    append(tmp_path, "chz.log", "Устаревшее API Bearer eyJhbGciOiJSUzI1NiJ9.секрет клиент +7 900 000-00-00")
    [signal] = day(spec, store).signals
    assert "eyJ" not in signal.message and "секрет" not in signal.message and "900" not in signal.message


def test_token_errors_alert_only_above_min_count_and_then_only_on_growth(spec, tmp_path):
    store = make_store()
    append(tmp_path, "chz.log", "начало")
    day(spec, store)

    # порог = max(min_count=5, 3 x среднее за прошлые проверки): 5, 12, 21, 26 — первые четыре дня фон, потом всплеск
    for count, expect_signal in [(4, False), (10, False), (12, False), (11, False), (60, True)]:
        append(tmp_path, "chz.log", *["Токен не действителен"] * count)
        assert bool(day(spec, store).signals) is expect_signal, count


def test_token_errors_at_min_count_alert_when_there_is_no_history_yet(spec, tmp_path):
    store = make_store()
    append(tmp_path, "chz.log", "начало")
    day(spec, store)
    append(tmp_path, "chz.log", *["Токен не действителен"] * 5)
    [signal] = day(spec, store).signals
    assert signal.title.endswith("Токен не действителен") and signal.level == "высокая"


def test_unsent_letter_does_not_consume_the_log(spec, tmp_path):
    store = make_store()
    append(tmp_path, "chz.log", "начало")
    day(spec, store)
    append(tmp_path, "chz.log", "Устаревшее API")
    assert len(day(spec, store, commit=False).signals) == 1
    assert len(day(spec, store).signals) == 1  # письмо не ушло — запись найдётся снова
    assert day(spec, store).signals == []


def test_rotation_new_file_after_baseline_is_read_from_the_start(spec, tmp_path):
    store = make_store()
    append(tmp_path, "chz-2026-10-13.log", "начало")
    day(spec, store)
    append(tmp_path, "chz-2026-10-14.log", "Устаревшее API", "Устаревшее API")
    [signal] = day(spec, store).signals
    assert "2 записей" in signal.message and "chz-2026-10-14.log" in signal.message


def test_truncated_or_replaced_file_is_read_from_the_beginning(spec, tmp_path):
    store = make_store()
    append(tmp_path, "chz.log", "очень длинная первая строка " * 10, "и вторая")
    day(spec, store)
    (tmp_path / "chz.log").write_bytes("Устаревшее API\n".encode("utf-8"))  # файл пересоздан и стал короче
    result = day(spec, store)
    assert len(result.signals) == 1 and any("ротация" in n for n in result.notes)


def test_replaced_file_that_is_already_longer_is_detected_by_its_first_bytes(spec, tmp_path):
    store = make_store()
    (tmp_path / "chz.log").write_bytes(b"AAAAAAAAAAAAAAAAAAAA\n")
    day(spec, store)
    # файл пересоздан и успел вырасти дальше прежней позиции: по размеру ротацию не заметить, по началу файла видно
    (tmp_path / "chz.log").write_bytes(("BBBBBBBBBBBBBBBBBBBB\nУстаревшее API\n" + "x" * 50 + "\n").encode())
    result = day(spec, store)
    assert len(result.signals) == 1 and any("ротация" in n for n in result.notes)


def test_half_written_last_line_waits_for_the_next_run(spec, tmp_path):
    store = make_store()
    append(tmp_path, "chz.log", "начало")
    day(spec, store)
    with (tmp_path / "chz.log").open("ab") as f:
        f.write("Устаревшее API без перевода строки".encode())
    assert day(spec, store).signals == []
    with (tmp_path / "chz.log").open("ab") as f:
        f.write(b"\n")
    assert len(day(spec, store).signals) == 1


def test_windows_1251_log_is_understood(spec, tmp_path):
    store = make_store()
    append(tmp_path, "chz.log", "начало", encoding="cp1251")
    day(spec, store)
    append(tmp_path, "chz.log", "Токен не действителен", *["Токен не действителен"] * 5, "Устаревшее API", encoding="cp1251")
    assert len(day(spec, store).signals) == 2


def test_missing_files_are_reported_in_notes_not_as_signals(spec, tmp_path):
    result = day(spec, make_store())
    assert result.signals == [] and any("не найдено" in n for n in result.notes)


def test_only_the_tail_is_read_when_the_log_grew_beyond_the_limit(spec, tmp_path):
    store = make_store()
    append(tmp_path, "chz.log", "начало")
    day(spec, store)
    append(tmp_path, "chz.log", *["Устаревшее API — метод отключён"] * 3000)  # ~100 КБ кириллицы
    limited = type(spec)(**{**spec.__dict__, "max_read_mb": 0.01})  # читаем не больше ~10 КБ
    result = scan_log(limited, store)
    assert any("только последние" in n for n in result.notes)
    [signal] = result.signals  # хвост начинается с границы строки, кириллица не превращается в иероглифы
    count = int(signal.message.split()[0])
    assert 100 < count < 400


def test_line_unfinished_at_first_run_is_skipped_whole_not_counted_by_its_tail(spec, tmp_path):
    store = make_store()
    (tmp_path / "chz.log").write_bytes("запись 1\nначало строки: ".encode())  # на момент первой проверки строка дописана не до конца
    day(spec, store)
    with (tmp_path / "chz.log").open("ab") as f:
        f.write("Устаревшее API\nзапись 3\n".encode())
    assert day(spec, store).signals == []  # кусок чужой строки не считаем: ждём целых строк


def test_expand_paths_returns_only_files_matching_the_pattern(spec, tmp_path):
    append(tmp_path, "chz-1.log", "a")
    append(tmp_path, "other.log", "a")
    (tmp_path / "chz-dir.log").mkdir()
    assert [p.name for p in expand_paths(spec)] == ["chz-1.log"]


def test_shipped_log_config_is_valid():
    from pathlib import Path

    [spec] = load_logwatch(Path(__file__).resolve().parent.parent / "config" / "logwatch.yaml")
    deprecated, token = spec.signatures
    assert deprecated.regex.search("WARN  Устаревшее   API: метод отключат") and token.regex.search("ТОКЕН не действителен")
    assert not deprecated.regex.search("Токен не действителен") and (token.min_count, token.growth) == (5, 3.0)


def test_log_that_did_not_exist_yet_is_not_marked_as_started(spec, tmp_path):
    store = make_store()
    assert day(spec, store).signals == []  # путь неверный или журнала ещё нет
    append(tmp_path, "chz.log", *["Устаревшее API"] * 50)  # путь поправили: в файле уже большая история
    first = day(spec, store)
    assert first.signals == [] and any("первый запуск" in n for n in first.notes)  # история — не новость
    append(tmp_path, "chz.log", "Устаревшее API")
    assert len(day(spec, store).signals) == 1


def test_missing_files_do_not_claim_that_a_position_was_remembered(spec, tmp_path):
    result = day(spec, make_store())
    assert not any("первый запуск" in n for n in result.notes) and any("не найдено" in n for n in result.notes)
