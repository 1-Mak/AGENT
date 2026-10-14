"""Контрольные вызовы API: сигналы об отключении метода, смене хоста, текста ошибки и схемы ответа."""

import json
import subprocess

import pytest
import requests

from sitewatch.config import ConfigError
from sitewatch.probes import (
    Probe,
    ProbeConfig,
    call,
    compare,
    error_text,
    load_probes,
    prepare,
    run_probes,
    schema_paths,
)
from tests.api_server import ApiServer, closed_port_url
from tests.helpers import make_store


@pytest.fixture
def api():
    server = ApiServer()
    yield server
    server.close()


def sig(status=200, host="h", location_host="", error="", schema=()):
    return {"status": status, "host": host, "location_host": location_host, "error": error, "schema": list(schema)}


def one_probe(url, **kw):
    params = dict(id="cises-info", name="cises/info", url=url)
    params.update(kw)
    return ProbeConfig(probes=(Probe(**params),))


# --- конфигурация ---


def write(tmp_path, text):
    path = tmp_path / "probes.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_load_probes_reads_methods_headers_body_and_token_settings(tmp_path):
    config = load_probes(write(tmp_path, """
token: {env: MY_TOKEN, command: 'python get_token.py --demo'}
unreachable_after: 3
probes:
  - id: auth
    name: Ключ авторизации
    url: ${BASE}/auth/key
  - id: info
    method: post
    url: ${BASE}/cises/info
    headers: {Authorization: 'Bearer ${MY_TOKEN}'}
    json: ['0104600000000000']
    timeout: 5
"""))
    assert [p.id for p in config.probes] == ["auth", "info"] and config.probes[0].method == "GET"
    info = config.probes[1]
    assert (info.method, info.json_body, info.timeout) == ("POST", ["0104600000000000"], 5.0)
    assert info.headers == (("Authorization", "Bearer ${MY_TOKEN}"),) and info.name == "info"
    assert config.token_var == "MY_TOKEN" and config.token_command[-1] == "--demo" and config.unreachable_after == 3


@pytest.mark.parametrize(
    "text, message",
    [
        ("probes:\n  - {id: a}\n", "id и url"),
        ("probes:\n  - {id: a, url: x, method: FETCH}\n", "method"),
        ("probes:\n  - {id: a, url: x}\n  - {id: a, url: y}\n", "повторяющийся"),
        ("probes:\n  - {id: a, url: x, headers: [1]}\n", "headers"),
        ("probes: nope\n", "списком"),
        ("- 1\n", "словарь"),
    ],
)
def test_load_probes_rejects_bad_files(tmp_path, text, message):
    with pytest.raises(ConfigError, match=message):
        load_probes(write(tmp_path, text))


def test_missing_probes_file_is_a_clear_error(tmp_path):
    with pytest.raises(ConfigError, match="не удалось прочитать"):
        load_probes(tmp_path / "нет.yaml")


# --- подготовка запроса ---


def test_prepare_substitutes_variables_everywhere_and_skips_when_url_or_body_variable_is_missing():
    probe = Probe("p", "p", "${BASE}/x", method="POST", json_body={"codes": ["${KM}"]}, headers=(("X-Id", "${KM}"),))
    ready = prepare(probe, {"BASE": "https://api.test", "KM": "0104"})
    assert (ready.url, ready.json_body, ready.headers, ready.mode) == ("https://api.test/x", {"codes": ["0104"]}, {"X-Id": "0104"}, "full")

    skipped = prepare(probe, {"BASE": "https://api.test"})
    assert isinstance(skipped, str) and "KM" in skipped and "пропущена" in skipped
    assert "BASE" in prepare(probe, {"KM": "1"})


def test_missing_token_drops_only_that_header_and_switches_to_anonymous_mode():
    probe = Probe("p", "p", "https://api.test/x", headers=(("Authorization", "Bearer ${TOKEN}"), ("Accept", "application/json")))
    anon = prepare(probe, {})
    assert anon.headers == {"Accept": "application/json"} and anon.mode == "anon"
    assert prepare(probe, {"TOKEN": "abc"}).headers["Authorization"] == "Bearer abc"


# --- разбор ответа ---


def test_error_text_ignores_volatile_parts():
    first = error_text('{"error_message": "Токен 8f14e45fceea167a5a36dedd4bea2543 истёк в 2026-10-14T09:15:00Z", "code": 4011}', json.loads('{"error_message": "Токен 8f14e45fceea167a5a36dedd4bea2543 истёк в 2026-10-14T09:15:00Z", "code": 4011}'))
    other_time = error_text("", {"error_message": "Токен 1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d истёк в 2026-10-15T11:00:01Z", "code": 4011})
    assert first == other_time == "Токен <hex> истёк в <время> | 4011"
    assert error_text("", {"error_message": "Устаревшее API"}) != first


def test_error_text_from_html_and_unknown_json():
    assert error_text("<html><style>p{}</style><body><h1>Метод отключён</h1>\n<p>Используйте v4</p></body></html>", None) == "Метод отключён Используйте v4"
    assert error_text('{"unknown": 1}', {"unknown": 1}) == ""
    assert len(error_text("x" * 1000, None)) == 300


def test_schema_paths_describe_structure_not_values():
    assert schema_paths({"a": 1, "b": {"c": [{"d": 1}, {"e": 2}]}, "f": []}) == {"a", "b", "b.c", "b.c[].d", "b.c[].e", "f"}
    assert schema_paths([{"x": 1}]) == {"[].x"}
    assert schema_paths({str(i): i for i in range(60)}) == {"*"}  # набор данных, а не схема
    assert schema_paths({"items": {str(i): i for i in range(60)}}) == {"items", "items.*"}


# --- сравнение (чистая логика) ---


def test_first_run_is_silent_except_for_gone_methods():
    assert compare(None, sig(200, schema=["a"])) == []
    assert compare(None, sig(401, error="Токен не действителен")) == []
    assert [m for _, m in compare(None, sig(410))] == ["HTTP 410 Gone: метод отключён"]


def test_unchanged_response_gives_no_signals():
    state = sig(401, error="Токен не действителен", schema=["error_message"])
    assert compare(state, dict(state)) == []


def test_method_becoming_gone_gives_exactly_one_signal_and_stays_quiet_afterwards():
    assert compare(sig(200), sig(410)) == [("высокая", "HTTP 410 Gone: метод отключён")]
    assert compare(sig(410), sig(410)) == []


def test_status_change_is_high_when_it_leaves_success_and_mentions_error_text():
    [(level, message)] = compare(sig(200), sig(404, error="Метод не найден"))
    assert level == "высокая" and "HTTP 200 → HTTP 404" in message and "Метод не найден" in message
    [(level, _)] = compare(sig(401), sig(200))
    assert level == "средняя"


def test_new_error_text_with_same_status_is_a_signal_but_changing_numbers_are_not():
    [(level, message)] = compare(sig(401, error="Токен не действителен"), sig(401, error="Устаревшее API"))
    assert level == "средняя" and "«Устаревшее API»" in message and "«Токен не действителен»" in message
    assert compare(sig(404, error="не найден <число>"), sig(404, error="не найден <число>")) == []


def test_schema_changes_removed_fields_are_high_added_are_low():
    signals = compare(sig(200, schema=["a", "b", "c"]), sig(200, schema=["a", "d"]))
    assert ("высокая", "в ответе пропали поля: b, c") in signals and ("низкая", "в ответе появились поля: d") in signals


def test_redirect_to_another_host_is_a_signal():
    signals = compare(sig(200), sig(301, location_host="new.example.com"))
    texts = " | ".join(m for _, m in signals)
    assert "перенаправляется на другой хост: new.example.com (раньше: без перенаправления)" in texts
    assert "HTTP 200 → HTTP 301" in texts
    assert compare(sig(301, location_host="new.example.com"), sig(301, location_host="new.example.com")) == []


def test_changing_the_probe_url_in_settings_is_not_reported_as_an_api_change():
    assert compare(sig(200, host="old.test", schema=["a"]), sig(404, host="new.test")) == []


# --- настоящие запросы к локальному серверу ---


def run_day(config, store, *, commit=True, **kw):
    result = run_probes(config, store, **kw)
    if commit:
        result.commit()
    return result


def messages(result):
    return [s.message for s in result.signals]


def test_week_of_a_method_baseline_then_quiet_then_switched_off(api):
    api.reply("/cises/info", 200, {"results": [{"code": "0104", "status": "EMITTED"}]})
    config, store = one_probe(api.base + "/cises/info", method="POST", json_body=["0104"]), make_store()

    day1 = run_day(config, store)
    assert day1.signals == [] and any("первый запуск" in n for n in day1.notes)
    assert run_day(config, store).signals == []

    api.reply("/cises/info", 410, {"error_message": "Метод отключён, используйте v4"})
    day3 = run_day(config, store)
    assert [(s.level, s.message, s.title) for s in day3.signals] == [("высокая", "HTTP 410 Gone: метод отключён", "cises/info")]
    assert run_day(config, store).signals == []  # повторно не сообщаем: состояние обновлено


def test_post_body_and_headers_are_sent(api):
    api.reply("/cises/info", 200, {"ok": True})
    config = one_probe(api.base + "/cises/info", method="POST", json_body={"codes": ["${KM}"]}, headers=(("Authorization", "Bearer ${TRUE_API_TOKEN}"),))
    run_day(config, make_store(), env={"KM": "0104", "TRUE_API_TOKEN": "secret-token"})
    [request] = api.requests
    assert request["method"] == "POST" and json.loads(request["body"]) == {"codes": ["0104"]}
    assert request["headers"]["Authorization"] == "Bearer secret-token"


def test_state_is_not_advanced_until_commit_so_unsent_signals_come_back(api):
    api.reply("/m", 200, {"a": 1})
    config, store = one_probe(api.base + "/m"), make_store()
    run_day(config, store)
    api.reply("/m", 404, {"error_message": "нет такого метода"})

    assert len(run_day(config, store, commit=False).signals) == 1  # письмо «не ушло»
    assert len(run_day(config, store, commit=False).signals) == 1  # поэтому завтра сигнал снова на месте
    assert len(run_day(config, store).signals) == 1 and run_day(config, store).signals == []


def test_new_error_text_on_401_and_404_is_reported(api):
    config, store = one_probe(api.base + "/m"), make_store()
    api.reply("/m", 401, {"error_message": "Токен не действителен"})
    run_day(config, store)
    assert run_day(config, store).signals == []
    api.reply("/m", 401, {"error_message": "Устаревшее API"})
    [signal] = run_day(config, store).signals
    assert "новый текст ошибки (HTTP 401): «Устаревшее API»" in signal.message


def test_schema_change_in_success_response_is_reported(api):
    config, store = one_probe(api.base + "/m"), make_store()
    api.reply("/m", 200, {"results": [{"code": "x", "gtin": "y"}], "total": 1})
    run_day(config, store)
    api.reply("/m", 200, {"results": [{"code": "x"}], "total": 1, "next": None})
    assert sorted(messages(run_day(config, store))) == ["в ответе появились поля: next", "в ответе пропали поля: results[].gtin"]


def test_redirect_is_not_followed_and_reports_the_new_host(api):
    api.reply("/m", 200, {"a": 1})
    config, store = one_probe(api.base + "/m"), make_store()
    run_day(config, store)
    api.reply("/m", 301, "", {"Location": "https://api-v2.example.com/m"})
    texts = " | ".join(messages(run_day(config, store)))
    assert "api-v2.example.com" in texts and "HTTP 200 → HTTP 301" in texts
    assert [r["path"] for r in api.requests] == ["/m", "/m"]  # за перенаправлением не ходили


def test_unreachable_api_is_reported_after_two_runs_in_a_row_and_only_once(api):
    config = one_probe(closed_port_url() + "/m")
    store = make_store()
    assert run_day(config, store).signals == []  # одна неудача — может быть сбой сети
    [signal] = run_day(config, store).signals
    assert signal.level == "средняя" and "2 запуска(ов) подряд" in signal.message
    assert run_day(config, store).signals == []  # не повторяем каждый день


def test_server_errors_do_not_replace_the_baseline_and_recovery_resets_the_counter(api):
    config, store = one_probe(api.base + "/m"), make_store()
    api.reply("/m", 200, {"a": 1})
    run_day(config, store)
    api.reply("/m", 503, {"error_message": "перегрузка"})
    assert run_day(config, store).signals == []
    api.reply("/m", 200, {"a": 1})
    assert run_day(config, store).signals == []  # вернулся прежний ответ — для базы 503 не существовало
    api.reply("/m", 503, "")
    assert run_day(config, store).signals == []  # счётчик начался заново, второго подряд ещё нет


def test_anonymous_and_authorized_modes_have_separate_baselines(api):
    api.reply("/m", 401, {"error_message": "Токен не действителен"})
    config = one_probe(api.base + "/m", headers=(("Authorization", "Bearer ${TRUE_API_TOKEN}"),))
    store = make_store()
    run_day(config, store, env={})  # без токена: baseline «401»
    api.reply("/m", 200, {"results": []})
    assert run_day(config, store, env={"TRUE_API_TOKEN": "t"}).signals == []  # с токеном — первый запуск своего режима
    api.reply("/m", 200, {"results": []})
    assert run_day(config, store, env={"TRUE_API_TOKEN": "t"}).signals == []
    assert api.requests[0]["headers"].get("Authorization") is None


def test_probe_with_missing_variable_is_skipped_without_request_or_signal(api):
    config = one_probe("${NO_SUCH_BASE}/m")
    result = run_day(config, make_store(), env={})
    assert result.signals == [] and any("NO_SUCH_BASE" in n and "пропущена" in n for n in result.notes) and api.requests == []


# --- токен из команды ---


def runner(stdout="", code=0, error=None):
    def run(args, **kwargs):
        if error:
            raise error
        run.calls.append(args)
        return subprocess.CompletedProcess(args, code, stdout=stdout, stderr="")

    run.calls = []
    return run


def test_token_command_provides_the_token_and_it_never_leaks_into_notes_or_signals(api):
    api.reply("/m", 200, {"a": 1})
    config = ProbeConfig(
        probes=(Probe("m", "Метод", api.base + "/m", headers=(("Authorization", "Bearer ${TRUE_API_TOKEN}"),)),),
        token_command=("get_token.bat",),
    )
    fake = runner("предупреждение\ntok-123\n")
    result = run_day(config, make_store(), env={}, token_runner=fake)
    assert api.requests[0]["headers"]["Authorization"] == "Bearer tok-123" and fake.calls == [["get_token.bat"]]
    assert "tok-123" not in " ".join(result.notes) + " ".join(s.message for s in result.signals)


@pytest.mark.parametrize("fake", [runner("", 1), runner("", 0), runner(error=FileNotFoundError("нет файла"))])
def test_failed_token_command_falls_back_to_anonymous_probes(api, fake):
    api.reply("/m", 401, {"error_message": "нужен токен"})
    config = ProbeConfig(
        probes=(Probe("m", "Метод", api.base + "/m", headers=(("Authorization", "Bearer ${TRUE_API_TOKEN}"),)),),
        token_command=("get_token.bat",),
    )
    result = run_day(config, make_store(), env={}, token_runner=fake)
    assert result.signals == [] and "Authorization" not in api.requests[0]["headers"]
    assert any("токен" in n and "без токена" in n for n in result.notes)


def test_token_command_is_not_run_when_token_is_already_in_environment(api):
    api.reply("/m", 200, {})
    config = ProbeConfig(probes=(Probe("m", "m", api.base + "/m"),), token_command=("get_token.bat",))
    fake = runner("x")
    run_day(config, make_store(), env={"TRUE_API_TOKEN": "already"}, token_runner=fake)
    assert fake.calls == []


# --- устойчивость вызова ---


def test_oversized_and_non_json_bodies_do_not_break_the_call(api):
    api.reply("/big", 200, "x" * 2_500_000, {"Content-Type": "text/plain"})
    prepared = prepare(Probe("big", "big", api.base + "/big"), {})
    result = call(prepared, requests.Session())
    assert result["status"] == 200 and result["schema"] == [] and "failure" not in result


def test_windows_1251_error_page_is_decoded(api):
    api.reply("/old", 404, "Метод не найден".encode("cp1251"), {"Content-Type": "text/html"})
    prepared = prepare(Probe("old", "old", api.base + "/old"), {})
    assert call(prepared, requests.Session())["error"] == "Метод не найден"


def test_default_value_in_placeholder_is_used_only_when_variable_is_empty():
    probe = Probe("p", "p", "https://api.test/x", method="POST", json_body=["${KM:-0104FAKE}"])
    assert prepare(probe, {}).json_body == ["0104FAKE"] and prepare(probe, {"KM": ""}).json_body == ["0104FAKE"]
    assert prepare(probe, {"KM": "0104REAL"}).json_body == ["0104REAL"]


def test_shipped_probe_config_is_valid_and_quiet_until_the_api_address_is_set():
    from pathlib import Path

    config = load_probes(Path(__file__).resolve().parent.parent / "config" / "probes.yaml")
    assert [p.id for p in config.probes] == ["auth-key", "cises-info"]
    skipped = prepare(config.probes[0], {})
    assert isinstance(skipped, str) and "TRUE_API_BASE" in skipped  # без адреса ничего не вызывается
    ready = prepare(config.probes[1], {"TRUE_API_BASE": "https://api.test"})  # тестового кода нет — берётся запасной
    assert ready.url == "https://api.test/cises/info" and ready.mode == "anon" and ready.json_body[0].startswith("0104")


def test_connection_error_text_does_not_leak_query_parameters_with_keys():
    config = one_probe(closed_port_url() + "/m?apikey=SECRET123&x=1")
    store = make_store()
    run_day(config, store)
    [signal] = run_day(config, store).signals
    assert "SECRET123" not in signal.message and "apikey" not in signal.message
