# -*- coding: utf-8 -*-
"""Streaming test: gateway uzerinden SSE akisini dogrular."""
import httpx
import json
import time

conn = httpx.get("http://127.0.0.1:8756/api/conn").json()
gk = conn["gateway_api_key"]
h = {"Authorization": f"Bearer {gk}"}
body = {"messages": [{"role": "user", "content": "Count 1 to 5"}], "stream": True}

t0 = time.time()
chunks = 0
first = None
text = ""
with httpx.stream("POST", "http://127.0.0.1:8756/v1/chat/completions",
                  json=body, headers=h, timeout=90) as r:
    print("STATUS:", r.status_code, "| content-type:", r.headers.get("content-type"))
    for line in r.iter_lines():
        if n_first_print := False:
            pass
        if line.startswith("data: ") and "[DONE]" not in line:
            chunks += 1
            if first is None:
                first = time.time() - t0
            try:
                j = json.loads(line[6:])
                d = (j.get("choices") or [{}])[0].get("delta", {})
                text += d.get("content") or d.get("reasoning") or ""
            except Exception:
                pass
            if chunks <= 3:
                print("ORN:", repr(line[:120]))
        elif line.startswith("data: [DONE]"):
            print("DONE marker alindi")
        elif chunks == 0 and line and not line.startswith("data: "):
            print("DIGER SATIR:", repr(line[:120]))

print(f"chunks={chunks} | TTFT={(f'{first:.2f}s' if first else 'YOK')} | total={time.time()-t0:.2f}s")
print("AKIS:", text[:200] or "(bos)")
