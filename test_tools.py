# -*- coding: utf-8 -*-
"""Claude Code tool_use testi: model tool cagirirken gateway tool_use bloklarini uretiyor mu?"""
import httpx
import json

conn = httpx.get("http://127.0.0.1:8756/api/conn").json()
gk = conn["gateway_api_key"]
h = {"x-api-key": gk, "anthropic-version": "2023-06-01", "Content-Type": "application/json"}

# ---- Non-stream: tool tanimli istek, model tool cagirmali ----
body = {
    "model": "", "max_tokens": 300,
    "tools": [
        {
            "name": "read_file",
            "description": "Dosyayi okur",
            "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
        }
    ],
    "messages": [{"role": "user", "content": [
        {"type": "text", "text": "gateway.py dosyasini oku"}
    ]}],
}
r = httpx.post("http://127.0.0.1:8756/v1/messages", json=body, headers=h, timeout=120)
print("NON-STREAM STATUS:", r.status_code)
d = r.json()
print("stop_reason:", d.get("stop_reason"))
print("content bloklari:")
for b in d.get("content", []):
    print("  -", b.get("type"), "|", (b.get("text") or b.get("name") or "")[:80])

# ---- Stream: tool tanimli istek ----
body["stream"] = True
evs = []
with httpx.stream("POST", "http://127.0.0.1:8756/v1/messages",
                  json=body, headers=h, timeout=120) as r:
    print("\nSTREAM STATUS:", r.status_code)
    for line in r.iter_lines():
        if line.startswith("event: "):
            ev = line[7:].strip()
            if ev and ev not in evs:
                evs.append(ev)
        elif line.startswith("data: ") and "tool_use" in line:
            print("TOOL SATIR:", line[:200])
print("event dizisi:", " -> ".join(evs))
