# -*- coding: utf-8 -*-
"""Claude Code akis simulasyonu: uzun streaming istegin tamamlanip tamamlanmadigini izler."""
import httpx
import json
import time

conn = httpx.get("http://127.0.0.1:8756/api/conn").json()
gk = conn["gateway_api_key"]
h = {"x-api-key": gk, "anthropic-version": "2023-06-01", "Content-Type": "application/json"}

body = {
    "model": "", "max_tokens": 500,
    "system": "You are a coding assistant. Be thorough but complete.",
    "messages": [{"role": "user", "content": [
        {"type": "text", "text": "Simdi proje yapisina bakiyorum ve bir HTTP istemi icin 3 satirlik Python kodu yaz: kullan, sonucu yazdir."}
    ]}],
    "stream": True,
}

t0 = time.time()
text = ""
evs = []
last_ev = None
with httpx.stream("POST", "http://127.0.0.1:8756/v1/messages",
                  json=body, headers=h, timeout=120) as r:
    print("STATUS:", r.status_code, "| content-type:", r.headers.get("content-type"))
    for line in r.iter_lines():
        if line.startswith("event: "):
            ev = line[7:].strip()
            if ev != last_ev:
                evs.append(ev)
                last_ev = ev
        elif line.startswith("data: "):
            try:
                j = json.loads(line[6:])
            except Exception:
                continue
            t = j.get("type")
            if t == "content_block_delta" and j["delta"].get("text"):
                text += j["delta"]["text"]
            elif t == "message_delta":
                print(f"FINISH stop_reason={j['delta'].get('stop_reason')} usage={j.get('usage')}")

elapsed = time.time() - t0
print("\n--- OZET ---")
print(f"sure: {elapsed:.1f}s | event dizisi: {' -> '.join(evs)}")
print(f"metin uzunlugu: {len(text)}")
print("ICERIK (ilk 300):", text[:300] or "(BOS)")
