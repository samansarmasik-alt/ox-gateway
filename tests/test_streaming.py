# -*- coding: utf-8 -*-
"""SSE cevirisi ve hata/bos-akis testleri.

Upstream HIC CAGRILMAZ: gateway.open_stream / open_atria_stream mocklanir,
_candidate_models da tek model dondurecek sekilde kapatilir (ag yok).
Saglik ucu, bos akis ve hata yolu ASGITransport uzerinden gercek endpoint'lerden
test edilir; bu ortamda fastapi.testclient.TestClient calismiyor (starlette/httpx
uyumsuzlugu) o yuzden ASGI dogrudan surulur.
"""
import asyncio
import json
import unittest
from unittest import mock

import httpx

import gateway


class _FakeCloser:
    """open_stream/open_atria_stream dondurdugu sahte client/resp."""

    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class _LineIter:
    """Sahte upstream satir iteratoru; lines bittikten sonra _after firlatir."""

    def __init__(self, lines, after=None) -> None:
        self._lines = list(lines)
        self._after = after
        self.pulled = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        self.pulled += 1
        if self._lines:
            return self._lines.pop(0)
        if self._after is not None:
            exc, self._after = self._after, None
            raise exc
        raise StopAsyncIteration


def _upstream(lines, after=None):
    """(client, resp, line_iter, first_line) donduren sahte upstream.

    Gercek httpx gibi ilk satir iterator'dan OKUNUP geri dondurulur; yoksa
    ilk icerik iki kez islenir.
    """
    rest = list(lines)
    first = rest.pop(0) if rest else ""
    it = _LineIter(rest, after)
    return _FakeCloser(), _FakeCloser(), it, first


def sse_data(data: str) -> str:
    """'data: ' on eki olmadan govdeyi dondurur."""
    return data[6:] if data.startswith("data: ") else data


def parse_sse(lines) -> list[tuple[str | None, str]]:
    """aiter_lines() ciktisini [(event, data)] listesine cevirir."""
    out: list[tuple[str | None, str]] = []
    ev = None
    for ln in lines:
        if ln.startswith("event: "):
            ev = ln[7:]
        elif ln.startswith("data: "):
            out.append((ev, ln[6:]))
            ev = None
    return out


def events_named(events, name):
    return [json.loads(d) for e, d in events if e == name]


class TestSSEPrimitives(unittest.TestCase):
    def test_sse_event_format(self):
        raw = gateway._sse_event("error", {"type": "error", "error": {"type": "api_error"}})
        self.assertIsInstance(raw, bytes)
        self.assertTrue(raw.endswith(b"\n\n"))
        self.assertTrue(raw.startswith(b"event: error\ndata: "))
        body = json.loads(raw.decode().split("data: ", 1)[1].strip())
        self.assertEqual(body["error"]["type"], "api_error")

    def test_sse_event_unicode_survives_roundtrip(self):
        raw = gateway._sse_event("message_delta", {"delta": {"stop_reason": "ş",
                                                             "stop_sequence": None}})
        body = json.loads(raw.decode("utf-8").split("data: ", 1)[1].strip())
        self.assertEqual(body["delta"]["stop_reason"], "ş")

    def test_anthropic_graceful_events_are_well_formed(self):
        events = gateway._anthropic_graceful_events()
        self.assertEqual(len(events), 6)
        names = [e.decode().split("\n", 1)[0] for e in events]
        self.assertEqual(names, [
            "event: message_start", "event: content_block_start",
            "event: content_block_delta", "event: content_block_stop",
            "event: message_delta", "event: message_stop"])
        self.assertIn(gateway.GRACEFUL_TEXT, events[2].decode("utf-8"))

    def test_openai_graceful_chunks_well_formed(self):
        chunks = gateway._openai_graceful_chunks()
        self.assertEqual(len(chunks), 1)
        j = json.loads(sse_data(chunks[0].decode().strip()))
        self.assertEqual(j["object"], "chat.completion.chunk")
        self.assertIn(gateway.GRACEFUL_TEXT, j["choices"][0]["delta"]["content"])
        self.assertEqual(j["choices"][0]["finish_reason"], "stop")


class TestDiagCounters(unittest.TestCase):
    def setUp(self):
        self._old = dict(gateway.DIAG)
        gateway.DIAG.update({"last": None, "turns": 0, "empty_turns": 0,
                             "truncated_turns": 0, "empty_stream_turns": 0})

    def tearDown(self):
        gateway.DIAG.clear()
        gateway.DIAG.update(self._old)

    def test_text_turn_counted_once(self):
        gateway._diag({"path": "/v1/messages", "has_text": True, "has_tool_use": False,
                       "stop_reason": "end_turn"})
        self.assertEqual(gateway.DIAG["turns"], 1)
        self.assertEqual(gateway.DIAG["empty_turns"], 0)
        self.assertEqual(gateway.DIAG["truncated_turns"], 0)

    def test_empty_turn_counted(self):
        gateway._diag({"path": "/v1/messages", "has_text": False, "has_tool_use": False,
                       "stop_reason": "end_turn"})
        self.assertEqual(gateway.DIAG["empty_turns"], 1)

    def test_truncated_turn_counted(self):
        gateway._diag({"path": "/v1/messages", "has_text": True, "has_tool_use": False,
                       "stop_reason": "max_tokens"})
        self.assertEqual(gateway.DIAG["truncated_turns"], 1)

    def test_last_turn_payload_stored(self):
        turn = {"path": "/v1/messages", "client_max_tokens": 131072,
                "sent_max_tokens": 32000, "has_text": True, "has_tool_use": False,
                "stop_reason": "end_turn"}
        gateway._diag(turn)
        self.assertEqual(gateway.DIAG["last"]["sent_max_tokens"], 32000)


class AnthropicStreamMixin:
    """/v1/messages SSE yolunu sahte upstream ile uctan uca surer."""

    async def _collect(self, lines, body=None, after=None, atria=False):
        payload = body or {"model": "test-model", "max_tokens": 131072, "stream": True,
                           "messages": [{"role": "user", "content": "selam"}]}
        if atria:
            ctx = mock.patch.object(gateway, "open_atria_stream",
                                    new=mock.AsyncMock(return_value=_upstream(lines, after)))
            route_ctx = mock.patch.object(gateway, "_should_route_atria", return_value=True)
        else:
            ctx = mock.patch.object(gateway, "open_stream",
                                    new=mock.AsyncMock(return_value=_upstream(lines, after)))
            route_ctx = mock.patch.object(gateway, "_should_route_atria", return_value=False)
        cands = mock.patch.object(gateway, "_candidate_models",
                                  new=mock.AsyncMock(return_value=["test-model"]))
        transport = httpx.ASGITransport(app=gateway.app)
        with ctx, route_ctx, cands:
            async with httpx.AsyncClient(transport=transport, base_url="http://test",
                                         timeout=10) as ac:
                headers = {"x-api-key": gateway.GATEWAY_KEY}
                async with ac.stream("POST", "/v1/messages", json=payload,
                                     headers=headers) as r:
                    self.assertEqual(r.status_code, 200)
                    self.assertIn("text/event-stream", r.headers.get("content-type", ""))
                    return parse_sse([ln async for ln in r.aiter_lines()])


class TestAnthropicEmptyStream(unittest.IsolatedAsyncioTestCase, AnthropicStreamMixin):
    """Oncelik 1: sessiz basarili-bos tur YASAK, error eventi gelmeli."""

    async def test_empty_stream_emits_api_error_not_successful_turn(self):
        events = await self._collect(["data: [DONE]"])
        self.assertEqual(len(events_named(events, "error")), 1)
        err = events_named(events, "error")[0]
        self.assertEqual(err["type"], "error")
        self.assertEqual(err["error"]["type"], "api_error")
        self.assertEqual(events_named(events, "message_stop"), [],
                         "bos akis message_stop GONDERMEMELI (sahte basari)")
        self.assertEqual(events_named(events, "message_start"), [],
                         "bos akis message_start ONCESI gonderilmemeli")

    async def test_stream_of_empty_chunks_emits_api_error(self):
        """delta bos + finish_reason yok: yine de hata olmali."""
        events = await self._collect([
            'data: {"choices":[{"delta":{},"finish_reason":null}]}',
            "data: [DONE]",
        ])
        self.assertEqual(len(events_named(events, "error")), 1)
        self.assertEqual(events_named(events, "error")[0]["error"]["type"], "api_error")

    async def test_finish_reason_error_emits_api_error(self):
        """Upstream finish_reason=error, icerik yok: hata olarak yansimali."""
        events = await self._collect([
            'data: {"choices":[{"delta":{},"finish_reason":"error"}]}',
            "data: [DONE]",
        ])
        self.assertEqual(len(events_named(events, "error")), 1)
        self.assertEqual(events_named(events, "message_stop"), [])

    async def test_empty_stream_counter_increments(self):
        old = dict(gateway.DIAG)
        try:
            before = gateway.DIAG.get("empty_stream_turns", 0)
            await self._collect(["data: [DONE]"])
            self.assertEqual(gateway.DIAG.get("empty_stream_turns", 0), before + 1)
        finally:
            gateway.DIAG.clear()
            gateway.DIAG.update(old)

    async def test_open_stream_raises_graceful_stream_still_terminates(self):
        """Upstream hic acilmazsa ASGI cokmez ve akis DUZGUN sonlanir.

        Bilincli davranis degisikligi: once model zincirinde hata olunca
        sagli cevap gibi gorunen GRACEFUL_TEXT akitiliyordu; bu, upstream
        hatayi istemciden gizliyordu. Artik:
          - zincirdeki tum modeller denenir (bizim ekledigimiz retry)
          - hepsi acilamazsa TEK bir 'error' event'i uretilir
        Testin asil amaci olan "ASGI cokmez, akis temiz kapanir" korunur.
        """
        transport = httpx.ASGITransport(app=gateway.app)
        with mock.patch.object(gateway, "open_stream",
                               new=mock.AsyncMock(side_effect=RuntimeError("kapali"))), \
             mock.patch.object(gateway, "_should_route_atria", return_value=False), \
             mock.patch.object(gateway, "_candidate_models",
                               new=mock.AsyncMock(return_value=["test-model"])):
            async with httpx.AsyncClient(transport=transport, base_url="http://test",
                                         timeout=10) as ac:
                async with ac.stream("POST", "/v1/messages", json={
                    "model": "test-model", "max_tokens": 512, "stream": True,
                    "messages": [{"role": "user", "content": "selam"}],
                }, headers={"x-api-key": gateway.GATEWAY_KEY}) as r:
                    self.assertEqual(r.status_code, 200)
                    events = [e for e, _ in parse_sse([ln async for ln in r.aiter_lines()])]
        # Akis tek bir terminal event ile bitti: sahte basari YOK.
        self.assertEqual(events[-1], "error")
        self.assertNotIn("message_stop", events)
        # message_start uretilmemis olmali (hicbir icerik akmadi).
        self.assertNotIn("message_start", events)


class TestAnthropicStreamContent(unittest.IsolatedAsyncioTestCase, AnthropicStreamMixin):
    async def test_text_stream_full_sequence(self):
        events = await self._collect([
            'data: {"choices":[{"delta":{"content":"Merhaba"}}]}',
            'data: {"choices":[{"delta":{"content":"!"}}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}],'
            '"usage":{"completion_tokens":5}}',
            "data: [DONE]",
        ])
        self.assertEqual([e for e, _ in events if e == "message_start"], ["message_start"])
        deltas = events_named(events, "content_block_delta")
        self.assertEqual([d["delta"]["text"] for d in deltas], ["Merhaba", "!"])
        self.assertEqual(deltas[0]["delta"]["type"], "text_delta")
        self.assertEqual(len(events_named(events, "content_block_stop")), 1)
        md = events_named(events, "message_delta")[0]
        self.assertEqual(md["delta"]["stop_reason"], "end_turn")
        self.assertEqual(md["usage"]["output_tokens"], 5)
        self.assertEqual(events[-1][0], "message_stop")
        self.assertEqual(events_named(events, "error"), [])

    async def test_thinking_block_precedes_text(self):
        events = await self._collect([
            'data: {"choices":[{"delta":{"reasoning":"dusunuyorum"}}]}',
            'data: {"choices":[{"delta":{"content":"cevap"}}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
            "data: [DONE]",
        ])
        starts = events_named(events, "content_block_start")
        self.assertEqual([s["content_block"]["type"] for s in starts], ["thinking", "text"])
        self.assertEqual(starts[1]["index"], 1)
        think = events_named(events, "content_block_delta")[0]
        self.assertEqual(think["delta"], {"type": "thinking_delta",
                                          "thinking": "dusunuyorum"})

    async def test_tool_call_arguments_stream_as_input_json_delta(self):
        events = await self._collect([
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1",'
            '"function":{"name":"get_weather","arguments":"{\\"city\\":"}}]}}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
            '"function":{"arguments":"\\"Izmir\\"}"}}]}}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
            "data: [DONE]",
        ])
        start = [s for s in events_named(events, "content_block_start")
                 if s["content_block"]["type"] == "tool_use"]
        self.assertEqual(len(start), 1)
        self.assertEqual(start[0]["content_block"]["name"], "get_weather")
        self.assertEqual(start[0]["content_block"]["id"], "call_1")
        parts = [d["delta"]["partial_json"] for d in events_named(events, "content_block_delta")
                 if d["delta"]["type"] == "input_json_delta"]
        self.assertEqual("".join(parts), '{"city":"Izmir"}')
        self.assertEqual(events_named(events, "message_delta")[0]["delta"]["stop_reason"],
                         "tool_use")

    async def test_stall_timeout_emits_timeout_error(self):
        """Satir gelmezse (bekleme zaman asimina ugrarsa) timeout_error + kapanis."""
        events = await self._collect(
            ['data: {"choices":[{"delta":{"content":"kismi"}}]}'],
            after=asyncio.TimeoutError())
        self.assertEqual(len(events_named(events, "error")), 1)
        self.assertEqual(events_named(events, "error")[0]["error"]["type"], "timeout_error")
        self.assertEqual(events[-1][0], "message_stop")

    async def test_malformed_json_line_is_skipped(self):
        events = await self._collect([
            "data: {bozuk json",
            'data: {"choices":[{"delta":{"content":"iyi"}}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
            "data: [DONE]",
        ])
        self.assertEqual(events_named(events, "error"), [])
        self.assertEqual([d["delta"]["text"] for d in
                          events_named(events, "content_block_delta")], ["iyi"])


class TestOpenAIStreamSurface(unittest.IsolatedAsyncioTestCase):
    """/v1/chat/completions SSE yollari."""

    async def _chat_stream(self, lines, atria, body=None, after=None):
        payload = body or {"model": "atria-test" if atria else "test-model", "stream": True,
                           "messages": [{"role": "user", "content": "selam"}]}
        if atria:
            ctx = mock.patch.object(gateway, "open_atria_stream",
                                    new=mock.AsyncMock(return_value=_upstream(lines, after)))
            route_ctx = mock.patch.object(gateway, "_should_route_atria", return_value=True)
        else:
            ctx = mock.patch.object(gateway, "open_stream",
                                    new=mock.AsyncMock(return_value=_upstream(lines, after)))
            route_ctx = mock.patch.object(gateway, "_should_route_atria", return_value=False)
        transport = httpx.ASGITransport(app=gateway.app)
        with ctx, route_ctx:
            async with httpx.AsyncClient(transport=transport, base_url="http://test",
                                         timeout=10) as ac:
                async with ac.stream("POST", "/v1/chat/completions", json=payload,
                                     headers={"x-api-key": gateway.GATEWAY_KEY}) as r:
                    self.assertEqual(r.status_code, 200)
                    return [ln for ln in [x async for x in r.aiter_lines()] if ln.strip()]

    async def test_openrouter_stream_passthrough_and_done(self):
        lines = await self._chat_stream([
            'data: {"choices":[{"delta":{"content":"a"}}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
            "data: [DONE]",
        ], atria=False)
        self.assertEqual(lines[-1], "data: [DONE]")
        body = [sse_data(ln) for ln in lines if ln.startswith("data: ") and "[DONE]" not in ln]
        self.assertEqual(len(body), 2)
        self.assertEqual(json.loads(body[0])["choices"][0]["delta"]["content"], "a")

    async def test_openrouter_stream_always_terminates_with_done(self):
        lines = await self._chat_stream(
            ['data: {"choices":[{"delta":{"content":"a"}}]}'], atria=False)
        self.assertEqual(lines[-1], "data: [DONE]")

    async def test_openrouter_stream_upsert_fail_returns_done(self):
        transport = httpx.ASGITransport(app=gateway.app)
        with mock.patch.object(gateway, "open_stream",
                               new=mock.AsyncMock(side_effect=RuntimeError("kapali"))), \
             mock.patch.object(gateway, "_should_route_atria", return_value=False):
            async with httpx.AsyncClient(transport=transport, base_url="http://test",
                                         timeout=10) as ac:
                async with ac.stream("POST", "/v1/chat/completions", json={
                    "model": "test-model", "stream": True,
                    "messages": [{"role": "user", "content": "x"}],
                }, headers={"x-api-key": gateway.GATEWAY_KEY}) as r:
                    self.assertEqual(r.status_code, 200)
                    lines = [ln for ln in [x async for x in r.aiter_lines()] if ln.strip()]
        self.assertEqual(lines[-1], "data: [DONE]")

    async def test_atria_stream_converted_to_openai_chunks(self):
        lines = await self._chat_stream([
            'data: {"type":"content_block_start","index":0,'
            '"content_block":{"type":"thinking","thinking":""}}',
            'data: {"type":"content_block_delta","index":0,'
            '"delta":{"type":"thinking_delta","thinking":"dusundu"}}',
            'data: {"type":"content_block_start","index":1,"content_block":'
            '{"type":"tool_use","id":"call_9","name":"get_weather","input":{}}}',
            'data: {"type":"content_block_delta","index":1,'
            '"delta":{"type":"input_json_delta","partial_json":"{\\"city\\":\\"Izmir\\"}"}}',
            'data: {"type":"content_block_delta","index":1,'
            '"delta":{"type":"text_delta","text":"oldu"}}',
            'data: {"type":"message_delta","delta":{"stop_reason":"tool_use"}}',
            'data: {"type":"message_stop"}',
        ], atria=True)
        chunks = [json.loads(sse_data(ln)) for ln in lines
                  if ln.startswith("data: ") and "[DONE]" not in ln]
        deltas = [c["choices"][0]["delta"] for c in chunks]
        self.assertEqual(deltas[0].get("reasoning"), "dusundu")
        tool_chunks = [d for d in deltas if d.get("tool_calls")]
        self.assertEqual(tool_chunks[0]["tool_calls"][0]["function"]["name"], "get_weather")
        args = "".join(tc["tool_calls"][0]["function"].get("arguments", "")
                       for tc in tool_chunks)
        self.assertEqual(args, '{"city":"Izmir"}')
        self.assertIn("oldu", [d.get("content") for d in deltas if d.get("content")])
        self.assertEqual(chunks[-1]["choices"][0]["finish_reason"], "tool_calls")
        self.assertEqual(lines[-1], "data: [DONE]")


class TestAnthropicModelChainRetry(unittest.IsolatedAsyncioTestCase):
    """Zincir uzerinde retry: hicbir sey akmadan basarisiz olunan deneme
    bir sonraki modele gecmeli, hepsi basarisizsa TEK hata event'i olmali.

    Bu yol gercek hayatta sik goruluyor: space-bunny gunluk limite girince
    gateway free yedeklere geciyor ve nvidia/nemotron bazen HTTP 200 + 0
    icerik + finish_reason=error donuyordu.
    """

    def setUp(self):
        self._diag = dict(gateway.DIAG)
        # sayaci sifirla: mutlak deger degil, BU testin katkisi olcumlenir
        gateway.DIAG["empty_stream_turns"] = 0

    def tearDown(self):
        gateway.DIAG.clear()
        gateway.DIAG.update(self._diag)

    async def _run(self, per_model, candidates=("m1", "m2", "m3")):
        """per_model: {model: (lines, after_exc|None)} veya model -> Exception"""
        calls = []

        async def fake_open(payload, m):
            calls.append(m)
            spec = per_model.get(m)
            if isinstance(spec, Exception):
                raise spec
            lines, after = spec
            return _upstream(lines, after)

        transport = httpx.ASGITransport(app=gateway.app)
        with mock.patch.object(gateway, "open_stream", new=fake_open), \
             mock.patch.object(gateway, "_should_route_atria", return_value=False), \
             mock.patch.object(gateway, "_candidate_models",
                               new=mock.AsyncMock(return_value=list(candidates))):
            async with httpx.AsyncClient(transport=transport, base_url="http://test",
                                         timeout=10) as ac:
                async with ac.stream("POST", "/v1/messages", json={
                    "model": "m1", "max_tokens": 512, "stream": True,
                    "messages": [{"role": "user", "content": "selam"}],
                }, headers={"x-api-key": gateway.GATEWAY_KEY}) as r:
                    self.assertEqual(r.status_code, 200)
                    return calls, parse_sse([ln async for ln in r.aiter_lines()])

    async def test_empty_stream_falls_through_to_next_model(self):
        """1. model hicbir sey uretmiyor -> 2. model basarili."""
        good = [
            'data: {"choices":[{"delta":{"content":"Merhaba"}}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
            "data: [DONE]",
        ]
        calls, events = await self._run({"m1": ([], None), "m2": (good, None)})
        names = [e for e, _ in events]
        self.assertEqual(calls, ["m1", "m2"], "ikinci modele gecmeliydi")
        self.assertIn("message_start", names)
        self.assertIn("message_stop", names)
        self.assertNotIn("error", names, "basarili cevapta hata olmamali")
        deltas = events_named(events, "content_block_delta")
        self.assertIn("Merhaba", [d["delta"]["text"] for d in deltas])

    async def test_upstream_error_finish_falls_through_to_next_model(self):
        """finish_reason=error -> sahte basari degil, sonraki modele gec."""
        err_stream = [
            'data: {"choices":[{"delta":{},"finish_reason":"error"}]}',
            "data: [DONE]",
        ]
        good = [
            'data: {"choices":[{"delta":{"content":"tamam"}}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
            "data: [DONE]",
        ]
        calls, events = await self._run({"m1": (err_stream, None), "m2": (good, None)})
        self.assertEqual(calls, ["m1", "m2"])
        names = [e for e, _ in events]
        self.assertIn("message_stop", names)
        self.assertNotIn("error", names)

    async def test_connection_error_falls_through_to_next_model(self):
        """Baglanti kurulamazsa (ConnectionError) sonraki model denenir."""
        good = [
            'data: {"choices":[{"delta":{"content":"ok"}}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
            "data: [DONE]",
        ]
        calls, events = await self._run(
            {"m1": OSError("baglanti yok"), "m2": (good, None)})
        self.assertEqual(calls, ["m1", "m2"])
        self.assertIn("message_stop", [e for e, _ in events])

    async def test_midstream_drop_before_content_retries(self):
        """Icerik hic baslamadan koptuysa (ReadError) tekrar denenir."""
        import httpx as _h
        good = [
            'data: {"choices":[{"delta":{"content":"ok"}}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
            "data: [DONE]",
        ]
        calls, events = await self._run(
            {"m1": ([], _h.ReadError("koptu")), "m2": (good, None)})
        self.assertEqual(calls, ["m1", "m2"])
        self.assertIn("message_stop", [e for e, _ in events])

    async def test_all_models_fail_yields_single_error_event(self):
        """Zincir tukendi -> TEK error event'i, sahte basari YOK, ASGI cokmez."""
        calls, events = await self._run(
            {"m1": ([], None), "m2": ([], None), "m3": ([], None)})
        names = [e for e, _ in events]
        self.assertEqual(calls, ["m1", "m2", "m3"], "tum modeller denenmeliydi")
        self.assertEqual(names.count("error"), 1, "tam olarak bir hata olmali")
        self.assertEqual(names[-1], "error")
        self.assertNotIn("message_stop", names)
        self.assertNotIn("message_start", names)
        self.assertEqual(gateway.DIAG.get("empty_stream_turns", 0), 1)

    async def test_diag_records_models_tried_on_total_failure(self):
        _, events = await self._run({"m1": ([], None), "m2": ([], None)})
        self.assertEqual(events[-1][0], "error")
        last = gateway.DIAG.get("last") or {}
        self.assertTrue(last.get("empty_stream"))
        self.assertEqual(last.get("requested_model"), "m1")
        # tum adaylar denenir (per_model'da tanimsiz olan da bos kabul edilir)
        self.assertEqual(last.get("models_tried"), ["m1", "m2", "m3"])

    async def test_no_retry_once_content_already_streamed(self):
        """Icerik aktiktan sonra kopma -> YENIDEN DENEMEZ (icerik tekrar etmez)."""
        import httpx as _h
        partial = [
            'data: {"choices":[{"delta":{"content":"yarim"}}]}',
        ]
        calls, events = await self._run(
            {"m1": (partial, _h.ReadError("koptu")), "m2": ([], None)})
        self.assertEqual(calls, ["m1"], "icerik aktiktan sonra tekrar denenmemeli")
        names = [e for e, _ in events]
        self.assertIn("message_stop", names)
        deltas = events_named(events, "content_block_delta")
        texts = [d["delta"].get("text", "") for d in deltas]
        # graceful metin satir basinda gelir
        self.assertEqual(texts, ["yarim", "\n" + gateway.GRACEFUL_TEXT])


if __name__ == "__main__":
    unittest.main()
