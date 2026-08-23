# -*- coding: utf-8 -*-
"""Anthropic /v1/messages Claude-uyumluluk testi: thinking + stop_reason + tool metinleri."""
import httpx
import json

conn = httpx.get("http://127.0.0.1:8756/api/conn").json()
gk = conn["gateway_api_key"]
h = {"x-api-key": gk, "anthropic-version": "2023-06-01", "Content-Type": "application/json"}
URL = "http://127.0.0.1:8756/v1/messages"

# ---- 1) thinking'siz -> reasoning kapali olmali, cevap sade ----
print("=== 1) THINKING'SIZ (reasoning kapali) ===")
body = {
    "model": "", "max_tokens": 150,
    "messages": [{"role": "user", "content": [{"type": "text", "text": "2+3 kac? sadece rakam yaz"}]}],
}
r = httpx.post(URL, json=body, headers=h, timeout=120)
d = r.json()
print("status:", r.status_code, "| stop_reason:", d.get("stop_reason"))
print("content:", d.get("content"))

# ---- 2) thinking'li (budget) -> thinking block beklenir ----
print("\n=== 2) THINKING'LI (budget 4096) ===")
body2 = {
    "model": "", "max_tokens": 300,
    "thinking": {"type": "enabled", "budget_tokens": 4096},
    "messages": [{"role": "user", "content": "3+4 kac? dusun, sonra sadece rakam yaz"}],
    "stream": True,
}
evs = []
think_txt, text_txt = "", ""
with httpx.stream("POST", URL, json=body2, headers=h, timeout=120) as r:
    for line in r.iter_lines():
        if line.startswith("data: "):
            try:
                j = json.loads(line[6:])
            except Exception:
                continue
            t = j.get("type")
            if t == "content_block_start":
                evs.append(f"block_start({j['content_block']['type']})")
            elif t == "content_block_delta":
                if j["delta"].get("thinking"):
                    think_txt += j["delta"]["thinking"]
                if j["delta"].get("text"):
                    text_txt += j["delta"]["text"]
            elif t == "content_block_stop":
                evs.append("block_stop")
            elif t == "message_delta":
                evs.append(f"msg_delta({j['delta'].get('stop_reason')})")
print("event dizisi:", " -> ".join(evs))
print("THINKING blok metni:", (think_txt[:100] or "(yok)"))
print("TEXT blok metni:", (text_txt[:100] or "(yok)"))

# ---- 3) tool_result iceren cok bloklu mesaj cokmemeli ----
print("\n=== 3) TOOL_RESULT BLOCK (cokecek mi?) ===")
body3 = {
    "model": "", "max_tokens": 80,
    "messages": [
        {"role": "user", "content": [
            {"type": "tool_result", "content": "sicaklik 20 derece"},
            {"type": "text", "text": "Buna gore sicaklik kac?"},
        ]},
    ],
}
r = httpx.post(URL, json=body3, headers=h, timeout=120)
d = r.json()
print("status:", r.status_code, "| cevap:", (d.get("content") or [{}])[0].get("text", "")[:80])
