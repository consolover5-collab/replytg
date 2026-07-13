import json

import httpx
import pytest

from replytg.suggest import SuggestError, generate_variants


def llm_response(content: str) -> dict:
    return {"choices": [{"message": {"content": content}}]}


def make_client(responses: list):
    """MockTransport: очередь ответов; фиксирует отправленные запросы."""
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        status, body = responses.pop(0)
        return httpx.Response(status, json=body)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler),
                             base_url="http://llm"), sent


async def test_happy_path():
    client, sent = make_client(
        [(200, llm_response('{"variants": ["Привет!", "Здравствуй."]}'))])
    variants = await generate_variants(
        client, model="m", style_profile="стиль", history_text="история",
        wave_text="новые сообщения")
    assert variants == ["Привет!", "Здравствуй."]
    assert sent[0]["model"] == "m"
    assert "стиль" in sent[0]["messages"][0]["content"]


async def test_fenced_json_accepted():
    client, _ = make_client(
        [(200, llm_response('```json\n{"variants": ["а", "б"]}\n```'))])
    assert await generate_variants(client, "m", "", "", "x") == ["а", "б"]


async def test_bad_json_retries_once():
    client, sent = make_client([
        (200, llm_response("ну вот варианты: 1) привет 2) хай")),
        (200, llm_response('{"variants": ["а", "б"]}')),
    ])
    variants = await generate_variants(client, "m", "", "", "x")
    assert variants == ["а", "б"] and len(sent) == 2


async def test_http_error_raises():
    client, _ = make_client([(500, {"error": "boom"}), (500, {"error": "boom"})])
    with pytest.raises(SuggestError):
        await generate_variants(client, "m", "", "", "x")


async def test_wrong_shape_raises():
    client, _ = make_client([
        (200, llm_response('{"variants": ["только один"]}')),
        (200, llm_response('{"variants": "не список"}')),
    ])
    with pytest.raises(SuggestError):
        await generate_variants(client, "m", "", "", "x")


async def test_too_long_variant_rejected():
    long = "х" * 2000
    body = json.dumps({"variants": [long, "б"]}, ensure_ascii=False)
    client, sent = make_client([(200, llm_response(body)), (200, llm_response(body))])
    with pytest.raises(SuggestError):
        await generate_variants(client, "m", "", "", "x", max_len=1000)
    assert len(sent) == 2  # был ретрай


async def test_identical_variants_retry_then_ok():
    client, _ = make_client([
        (200, llm_response('{"variants": ["одно", "одно"]}')),
        (200, llm_response('{"variants": ["одно", "другое"]}')),
    ])
    assert await generate_variants(client, "m", "", "", "x") == ["одно", "другое"]
