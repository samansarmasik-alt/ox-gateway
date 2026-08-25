# -*- coding: utf-8 -*-
"""Multi-tool-call simulasyonu: tek chunk'ta gelen 3 tool_call ayri bloklara
dagilmali ve her blokun JSON'u gecerli olmali (Claude Code hatasinin regresyon testi)."""
import asyncio
import json

import gateway
from fastapi.testclient import TestClient

FAKE_LINES = [
    'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_a","function":{"name":"read","arguments":""}}]}}]}',
    'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"filePath\\": \\"a.js\\"}"}}]}}]}',
    'data: {"choices":[{"delta":{"tool_calls":[{"index":1,"id":"call_b","function":{"name":"read","arguments":"{\\"filePath\\": \\"b.json\\"}"}}]}}]}',
    'data: {"choices":[{"delta":{"tool_calls":[{"index":2,"id":"call_c","function":{"name":"read","arguments":"{\\"filePath\\": \\"c.py\\"}"}}]},"finish_reason":"tool_calls"}]}',
    "data: [DONE]",
]


class FakeResp:
    def __init__(self):
        self._it = iter(FAKE_LINES)

    async def aiter_lines(self):
        for l in self._it:
            yield l

    async def aclose(self):
        pass


class FakeClient:
    async def aclose(self):
        pass


async def fake_open_stream(payload, model=None):
    return FakeClient(), FakeResp(), FakeResp().aiter_lines(), next(iter(FAKE_LINES))


gateway.open_stream = fake_open_stream
client = TestClient(gateway.app)

body = {
    "model": "", "max_tokens": 300,
    "tools": [{"name": "read", "description": "okur",
               "input_schema": {"type": "object", "properties": {"filePath": {"type": "string"}}}}],
    "messages": [{"role": "user", "content": "3 dosyayi oku"}],
    "stream": True,
}
r = client.post("/v1/messages", json=body,
                headers={"x-api-key": gateway.GATEWAY_KEY})
print("STATUS:", r.status_code)

blocks = []
ev = ""
for raw in r.text.split("\n"):
    if raw.startswith("event: "):
        ev = raw[7:].strip()
    elif raw.startswith("data: ") and ev == "content_block_start":
        cb = json.loads(raw[6:])["content_block"]
        if cb["type"] == "tool_use":
            blocks.append({"name": cb["name"], "json": ""})
    elif raw.startswith("data: ") and ev == "content_block_delta":
        d = json.loads(raw[6:])["delta"]
        if d.get("partial_json") is not None and blocks:
            blocks[-1]["json"] += d["partial_json"]

print("tool_use blok sayisi:", len(blocks))
ok = True
for b in blocks:
    try:
        parsed = json.loads(b["json"])
        print(f"  OK {b['name']}: {parsed}")
    except Exception as e:
        ok = False
        print(f"  BOZUK {b['name']}: {b['json'][:80]} ({e})")

if len(blocks) == 3 and ok:
    print("\nPASSED: 3 ayri blok, 3 gecerli JSON")
else:
    print("\nFAILED!")
