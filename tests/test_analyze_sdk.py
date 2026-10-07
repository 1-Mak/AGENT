"""Проверка реального пути через SDK anthropic с подменённым HTTP-транспортом (без сети и без ключа)."""

import json

import anthropic
import httpx2 as httpx

from sitewatch.analyze import Analyzer
from sitewatch.models import Change, DocRecord
from tests.helpers import item_url, make_source

ANALYSIS_JSON = {
    "summary": "Срок перехода перенесён на 1 июля 2026.",
    "key_changes": ["перенос срока перехода"],
    "who_is_affected": "участники оборота",
    "deadlines": ["01.07.2026 — переход"],
    "required_actions": [],
    "importance": "высокая",
    "importance_reason": "изменён обязательный срок",
}


def make_client(captured: list[dict], stop_reason: str = "end_turn") -> anthropic.Anthropic:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "model": "claude-opus-5-5",
                "content": [{"type": "text", "text": json.dumps(ANALYSIS_JSON, ensure_ascii=False)}],
                "stop_reason": stop_reason,
                "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 10},
            },
        )

    return anthropic.Anthropic(
        api_key="test-key", http_client=httpx.Client(transport=httpx.MockTransport(handler)), max_retries=0
    )


def make_change() -> Change:
    rec = DocRecord(item_url(27), "cz-releases", "Что нового 27", "h", "Срок перехода перенесён на 1 июля 2026.")
    return Change(make_source(), rec, "new")


def test_real_sdk_request_shape_and_response_parsing():
    captured: list[dict] = []
    result = Analyzer("claude-opus-5-5", client=make_client(captured)).analyze(make_change())

    assert result is not None and result.importance == "высокая"
    assert result.deadlines == ["01.07.2026 — переход"]

    body = captured[0]
    assert body["model"] == "claude-opus-5-5"
    system_text = body["system"] if isinstance(body["system"], str) else json.dumps(body["system"], ensure_ascii=False)
    assert "аналитик" in system_text
    schema = body["output_config"]["format"]["schema"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {
        "summary", "key_changes", "who_is_affected", "deadlines",
        "required_actions", "importance", "importance_reason",
    }  # fmt: skip
    assert schema["properties"]["importance"]["enum"] == ["высокая", "средняя", "низкая"]
    assert "Срок перехода перенесён" in json.dumps(body["messages"], ensure_ascii=False)


def test_real_sdk_refusal_stop_reason_yields_no_analysis():
    captured: list[dict] = []
    client = make_client(captured, stop_reason="refusal")
    assert Analyzer("claude-opus-5-5", client=client).analyze(make_change()) is None
