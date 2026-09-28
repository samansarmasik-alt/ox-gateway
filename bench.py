#!/usr/bin/env python3
"""bench.py - ox-gateway gecikme / guvenilirlik olcumu.

Streamed SSE (POST /v1/messages) uzerinden olcer; gercek istemcilerin
gördugu yol ayni. Olculer: ilk bayt, ilk gercek icerik, toplam sure,
outcome siniflandirmasi, token sayimi, key/model kirilimi.

Kurallar:
- Gateway anahtari sadece GET /api/conn'dan calisma aninda alinir, bellekte
  tutulur; hicbir yere yazilmaz, ekrana basilmaz.
- /api/stats yanitindaki "full" alani (duz anahtar) HICBIR ZAMAN
  tutulmaz; yalnizca gateway'in kendi maskelemis "key" degeri kullanilir.
- Upstream'in gercekten icerik uretmemesi bir OLUMDUR, hata degil: satir
  olarak raporlanir, kosuyu oldurmez.
- Gateway'de degisiklik yapmaz; /mode/set cagrilmaz.
- /api/diag'dan last.model (GERCEKTEN kullanilan) ile last.requested_model
  (ISTENEN) karsilastirilir; farkliysa sessiz fallback vardir ve UYARI basilir.
  last.empty_stream + last.models_tried + last.upstream_error ise zincirin
  TAMAMININ dustugunu gosterir (api_error SSE'si sadece bu yolda uretilir).
- logs\\gateway.log OKUNMAZ; log rotasyonu (.1/.2/.3) bu dosyayi etkilemez.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import time
from pathlib import Path

import httpx

DEFAULT_BASE = "http://127.0.0.1:8756"
SCHEMA = 2
# Buyuk deger: gateway'in _clamp_max_tokens (tavan 32000) her kosuda gorunsun.
REQ_MAX_TOKENS = 131072
MAX_CONCURRENCY = 8
# /api/diag sayaclari (empty_stream_turns 2026'da eklendi; once hep 0 idi).
DIAG_KEYS = ("turns", "empty_turns", "empty_stream_turns", "truncated_turns")
DEFAULT_PROMPT = ("Projenin kok dosyalarini listele. Once list_files aracini "
                  "kullan, sonra ilk 3 dosyanin adini yaz.")
TOOLS = [{
    "name": "list_files",
    "description": "Verilen dizindeki dosya adlarini listeler.",
    "input_schema": {
        "type": "object",
        "properties": {"dir": {"type": "string", "description": "Dizin yolu"}},
        "required": ["dir"],
    },
}]


# ---------------------------------------------------------------- yardimci
def _ms(t0: float, t1: float | None = None) -> float:
    return round(((t1 if t1 is not None else time.perf_counter()) - t0) * 1000, 1)


def _mask(k: str) -> str:
    """Gateway'in maskeli 'key' degerini gecir (güvenlik ağı).
    Gateway 'k[:10]+...+k[-4:]' uretiyor (17 karakter). Icinde '...' varsa
    zaten maskelidir; yoksa maskelenir. Duz anahtar hicbir zaman cikamaz."""
    k = (k or "").strip()
    if "..." in k:
        return k
    return k[:10] + "..." + k[-4:] if len(k) > 18 else "***"


def _int(v, d: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return d


def _pct(vals: list[float], p: float):
    """Yakin-rank yuzdelik."""
    if not vals:
        return None
    s = sorted(vals)
    return round(s[max(1, math.ceil(p / 100 * len(s))) - 1], 1)


def _keymap(stats: dict) -> dict:
    """/api/stats -> maskeli anahtar -> sayaçlar. 'full' alani atilir."""
    out = {}
    for k in (stats or {}).get("keys") or []:
        if not isinstance(k, dict):
            continue
        out[_mask(str(k.get("key") or ""))] = {
            "requests": _int(k.get("requests")), "ok": _int(k.get("ok")),
            "fail": _int(k.get("fail")), "avg_ms": k.get("avg_ms"),
            "state": k.get("state"),
        }
    return out


def _chain_status(row: dict) -> str:
    """Zincir sonucunu ikiye ayirir: model mi tilkendi, zincirin tamami mi dustu?

    Gateway'in 'api_error' SSE'si SADECE tum modeller tukendiginde uretilir
    (gateway.py sse_anthropic); dolayisiyla empty_error == zincir tukendi.
    """
    tried = [str(m) for m in (row.get("models_tried") or []) if m]
    if row.get("empty_stream") or row.get("outcome") == "empty_error":
        return "chain_down" if len(tried) > 1 else "chain_down_single"
    if row.get("fallback"):
        return "model_flaky"  # ilk model dustu, zincirdeki baska model cevap verdi
    return "single_model"    # kanit yok (tek aday ya da es zamanlilik>1)


def _apply_diag(row: dict, last: dict) -> None:
    """/api/diag 'last' kaydini satira isle. DIAG sentinel'leri None'dir."""
    row["model_used"] = last.get("model")
    row["model_requested"] = last.get("requested_model")
    used, req = row["model_used"], row["model_requested"]
    row["fallback"] = bool(used and req and used != req)
    row["models_tried"] = [str(m) for m in (last.get("models_tried") or []) if m]
    row["upstream_error"] = (str(last["upstream_error"])[:160]
                             if last.get("upstream_error") else None)
    row["empty_stream"] = bool(last.get("empty_stream")) or None
    row["client_max_tokens"] = last.get("client_max_tokens")
    row["sent_max_tokens"] = last.get("sent_max_tokens")
    row["diag_note"] = str(last.get("mid_stream_error") or "")[:120]
    row["chain"] = _chain_status(row)


# ------------------------------------------------------------------ olcum
async def _one(client: httpx.AsyncClient, url: str, headers: dict, body: dict,
                timeout: float, idx: int) -> dict:
    """Tek istek: sert timeout'lu, her durumda satir ureten olcum."""
    row = {
        "i": idx, "outcome": None, "http": None, "ttfb_ms": None,
        "tt_content_ms": None, "total_ms": None, "text_chars": 0,
        "tools": [], "output_tokens": None, "stop_reason": None,
        "error": None, "graceful": False, "model_seen": None,
        "model_used": None, "model_requested": None, "fallback": None,
        "models_tried": [], "upstream_error": None, "empty_stream": None,
        "chain": "single_model", "diag_turns_delta": None,
        "client_max_tokens": None, "sent_max_tokens": None, "diag_note": "",
    }
    t0 = time.perf_counter()
    ev, chars, tools = "", 0, []
    usage = stop = err_type = err_msg = None
    graceful = False  # GRACEFUL_TEXT: zincir tukendi, icerik sahte

    def mark_content(now: float) -> None:
        if row["tt_content_ms"] is None:
            row["tt_content_ms"] = _ms(t0, now)

    try:
        async with client.stream("POST", url, json=body, headers=headers) as resp:
            row["http"] = resp.status_code
            if resp.status_code != 200:
                try:
                    txt = (await resp.aread()).decode("utf-8", "replace")
                except Exception:
                    txt = ""
                row["outcome"] = f"http_{resp.status_code}"
                row["error"] = " ".join(txt[:160].split())
                row["total_ms"] = _ms(t0)
                return row
            async def pump():
                nonlocal ev, chars, usage, stop, err_type, err_msg, graceful
                async for ln in resp.aiter_lines():
                    now = time.perf_counter()
                    if row["ttfb_ms"] is None and ln.strip():
                        row["ttfb_ms"] = _ms(t0, now)
                    if ln.startswith("event:"):
                        ev = ln[6:].strip()
                    elif ln.startswith("data:"):
                        raw = ln[5:].strip()
                        if raw == "[DONE]":
                            break
                        try:
                            j = json.loads(raw)
                        except ValueError:
                            continue
                        if not isinstance(j, dict):
                            continue
                        t = j.get("type") or ev
                        if t == "message_start":
                            mm = (j.get("message") or {}).get("model")
                            row["model_seen"] = mm
                            if mm == "ox-gateway":
                                graceful = True  # zincir tukendi, GRACEFUL_TEXT
                        elif t == "content_block_start":
                            cb = j.get("content_block") or {}
                            kind = cb.get("type")
                            if kind == "tool_use":
                                tools.append(str(cb.get("name") or "?"))
                                mark_content(now)
                            elif kind == "text":
                                mark_content(now)
                            # thinking: gercek icerik degil, sayilmaz
                        elif t == "content_block_delta":
                            dl = j.get("delta") or {}
                            dt = dl.get("type")
                            if dt == "text_delta":
                                txt = dl.get("text") or ""
                                chars += len(txt)
                                if txt.strip():
                                    mark_content(now)
                                if "[ox-gateway]" in txt:
                                    graceful = True
                            elif dt == "input_json_delta" and dl.get("partial_json"):
                                mark_content(now)
                        elif t == "message_delta":
                            stop = (j.get("delta") or {}).get("stop_reason") or stop
                            usage = (j.get("usage") or {}).get("output_tokens", usage)
                        elif t == "error":
                            e = j.get("error") or {}
                            err_type = e.get("type") or "error"
                            err_msg = str(e.get("message") or "")[:160]

            await asyncio.wait_for(pump(), timeout=timeout)
            row["total_ms"] = _ms(t0)
    except asyncio.TimeoutError:
        row["outcome"] = "timeout"
        row["total_ms"] = _ms(t0)
    except Exception as e:  # ag yok, baglanti koptu vs -> olcum olarak kalir
        row["outcome"] = "network"
        row["error"] = f"{type(e).__name__}: {e}"[:160]
        row["total_ms"] = _ms(t0)

    row["text_chars"] = chars
    row["tools"] = tools
    row["output_tokens"] = usage
    row["stop_reason"] = stop
    row["graceful"] = graceful or ("[ox-gateway]" in (err_msg or ""))

    # --- outcome siniflandirmasi (oncelik sirasi onemli) ---
    if row["outcome"] in ("network", "timeout"):
        pass  # istemci tarafinda oldu, dokunma
    elif err_type == "api_error":
        row["outcome"] = "empty_error"
    elif err_type == "timeout_error":
        row["outcome"] = "stall"  # gateway'in kendi stall_timeout'i kapatti
    elif err_type:
        row["outcome"] = f"error_{err_type}"
    elif row["graceful"]:
        row["outcome"] = "graceful"  # tum modeller/anahtarlar tukendi
    elif tools:
        row["outcome"] = "tool_use"
    elif chars > 0:
        row["outcome"] = "content"
    else:
        row["outcome"] = "empty_silent"  # 200 + bitis, icerik YOK (eski sessiz hata)
    if err_msg and not row["error"]:
        row["error"] = err_msg
    return row


# ------------------------------------------------------------------ toplu
async def _run(client, url, headers, body, rows_out, n, conc, timeout,
                diag_url, serial) -> None:
    sem = asyncio.Semaphore(conc)

    async def one(i: int):
        async with sem:
            before = None
            if serial:
                try:
                    before = (await client.get(diag_url, timeout=10)).json()
                except Exception:
                    before = None
            row = await _one(client, url, headers, body, timeout, i)
            if before is not None:
                try:
                    after = (await client.get(diag_url, timeout=10)).json()
                except Exception:
                    after = {}
                d = _int((after or {}).get("turns")) - _int((before or {}).get("turns"))
                row["diag_turns_delta"] = d
                last = (after or {}).get("last") or {}
                if d >= 1 and isinstance(last, dict):
                    _apply_diag(row, last)
                else:
                    row["diag_note"] = "gateway bu turu saymadi (turns artmadi)"
            rows_out.append(row)

    await asyncio.gather(*(one(i) for i in range(n)))


# ----------------------------------------------------------------- ozet
def _summarize(rows: list[dict], diag_b: dict, diag_a: dict,
               kb: dict, ka: dict) -> dict:
    total = len(rows) or 1
    ok = [r for r in rows if r["outcome"] in ("content", "tool_use")]
    empty = [r for r in rows if r["outcome"] in ("empty_error", "empty_silent", "graceful")]
    tt = [r["tt_content_ms"] for r in ok if r["tt_content_ms"] is not None]
    tot = [r["total_ms"] for r in rows if r["total_ms"] is not None]
    fb = [r["ttfb_ms"] for r in rows if r["ttfb_ms"] is not None]
    outs: dict = {}
    for r in rows:
        outs[r["outcome"]] = outs.get(r["outcome"], 0) + 1
    by_model: dict = {}
    for r in rows:
        k = r.get("model_seen") or r.get("model_used") or "bilinmiyor"
        e = by_model.setdefault(k, {"n": 0, "ok": 0})
        e["n"] += 1
        e["ok"] += 1 if r["outcome"] in ("content", "tool_use") else 0
    # Fallback kaniti: satirdan (ser kosu) ya da kosu sonu /api/diag 'last' kaydindan.
    fb_rows = [r for r in rows if r.get("fallback")]
    last: dict = {}
    if not fb_rows and isinstance(diag_a.get("last"), dict) and _int(diag_a.get("turns")) > _int(diag_b.get("turns")):
        last = diag_a["last"]
    used = (fb_rows[0].get("model_used") if fb_rows else last.get("model")) or None
    req = (fb_rows[0].get("model_requested") if fb_rows else last.get("requested_model")) or None
    fallback = bool(used and req and used != req) if not fb_rows else True
    chain: dict = {}
    for r in rows:
        cs = r.get("chain") or "single_model"
        chain[cs] = chain.get(cs, 0) + 1
    tried_all = [m for r in rows for m in (r.get("models_tried") or [])]
    keys = []
    for mk, cur in ka.items():
        prev = kb.get(mk, {})
        d_req = cur["requests"] - prev.get("requests", cur["requests"])
        d_ok = cur["ok"] - prev.get("ok", 0)
        d_fail = cur["fail"] - prev.get("fail", 0)
        if d_req:
            keys.append({"key": mk, "requests": d_req, "ok": d_ok, "fail": d_fail,
                         "avg_ms": cur.get("avg_ms"), "state": cur.get("state")})
    keys.sort(key=lambda x: -x["requests"])
    return {
        "n": len(rows),
        "success_rate": round(len(ok) / total, 3),
        "empty_rate": round(len(empty) / total, 3),
        "tool_rate": round(len([r for r in rows if r["outcome"] == "tool_use"]) / total, 3),
        "outcomes": outs,
        "latency_ms": {
            "ttfb": {"p50": _pct(fb, 50), "p90": _pct(fb, 90), "max": round(max(fb), 1) if fb else None},
            "tt_content": {"p50": _pct(tt, 50), "p90": _pct(tt, 90), "max": round(max(tt), 1) if tt else None},
            "total": {"p50": _pct(tot, 50), "p90": _pct(tot, 90), "max": round(max(tot), 1) if tot else None},
        },
        "output_tokens": [r["output_tokens"] for r in rows if r["output_tokens"] is not None],
        "by_model": by_model,
        "by_key": keys,
        "model_used": used,
        "model_requested": req,
        "fallback": fallback,
        "fallback_rows": len(fb_rows),
        "chain": {
            "status": chain,
            "models_tried_last": [str(m) for m in (last.get("models_tried") or []) if m] or None,
            "models_tried": sorted(set(tried_all)),
            "max_models_tried": max([len(r.get("models_tried") or []) for r in rows] or [0]),
            "last_upstream_error": next((r["upstream_error"] for r in rows
                                         if r.get("upstream_error")), None)
                                    or (str(last.get("upstream_error"))[:160]
                                        if last.get("upstream_error") else None),
        },
        "diag_delta": {
            "turns": _int(diag_a.get("turns")) - _int(diag_b.get("turns")),
            "empty_turns": _int(diag_a.get("empty_turns")) - _int(diag_b.get("empty_turns")),
            "empty_stream_turns": _int(diag_a.get("empty_stream_turns")) - _int(diag_b.get("empty_stream_turns")),
            "truncated_turns": _int(diag_a.get("truncated_turns")) - _int(diag_b.get("truncated_turns")),
        },
    }


# ----------------------------------------------------------------- cikti
def _print_human(h: dict, s: dict, rows: list[dict], diag_b: dict, diag_a: dict) -> None:
    p = print
    p(f"ox-gateway bench - mod {h['mode']} ({h['provider']}) - {h['model_actual']} "
      f"- anahtar {h['total_keys']} (kullanilabilir {h['active_keys']})")
    p(f"kosu: n={s['n']} es zamanlilik={h['concurrency']} timeout={h['timeout']}s "
      f"tools={'yok' if h['no_tools'] else 'var'} - istenen max_tokens={REQ_MAX_TOKENS}")
    if h["model_requested"] != h["model_actual"]:
        p(f"UYARI: istenen model '{h['model_requested']}' != aktif '{h['model_actual']}'")
    p("")
    p(f"{'#':>2} {'sonuc':<13} {'http':>4} {'ttfb':>7} {'icerk':>7} {'toplam':>8} "
      f"{'tok':>5} {'stop':<11} not")
    for r in sorted(rows, key=lambda x: x["i"]):
        p(f"{r['i']:>2} {r['outcome']:<13} {str(r['http'] or '-'):>4} "
          f"{str(r['ttfb_ms'] or '-'):>7} {str(r['tt_content_ms'] or '-'):>7} "
          f"{str(r['total_ms'] or '-'):>8} {str(r['output_tokens'] if r['output_tokens'] is not None else '-'):>5} "
          f"{str(r['stop_reason'] or '-'):<11} {(r['error'] or '')[:44]}")
    p("")
    p(f"basari {s['success_rate'] * 100:.0f}% - bos/hata {s['empty_rate'] * 100:.0f}% - "
      f"tool {s['tool_rate'] * 100:.0f}%")
    for name, d in s["latency_ms"].items():
        p(f"  {name:<11} p50={d['p50']} p90={d['p90']} max={d['max']} ms")
    p("  sonuc dagilimi: " + " | ".join(f"{k}={v}" for k, v in sorted(s["outcomes"].items())))
    # --- model kaniti: kullanilan vs istenen (sessiz fallback artik gorunur)
    mu, mr = s.get("model_used"), s.get("model_requested")
    if mu or mr:
        p(f"  model_used={mu or '-'}  model_requested={mr or '-'}  fallback={s['fallback']}")
    if s.get("fallback"):
        p(f"  UYARI: SESSIZ FALLBACK - istenen '{mr}' yerine '{mu}' cevap verdi "
          f"({s['fallback_rows']}/{s['n']} satir).")
    for mk, e in s["by_model"].items():
        tag = " (FALLBACK)" if mk and h["model_requested"] and mk != h["model_requested"] else ""
        p(f"  model {mk}{tag}: n={e['n']} basarili={e['ok']}")
    ch = s.get("chain") or {}
    if ch:
        p("  zincir: " + " | ".join(f"{k}={v}" for k, v in sorted((ch.get("status") or {}).items())))
        if ch.get("max_models_tried"):
            p(f"  zincir sonrasi: en cok {ch['max_models_tried']} model denendi"
              + (f" -> {', '.join(ch['models_tried'])}" if ch.get("models_tried") else ""))
        if ch.get("last_upstream_error"):
            p(f"  zincir upstream hatasi: {ch['last_upstream_error'][:120]}")
    if s["by_key"]:
        p("  anahtar dagilimi (maskeli): " + " | ".join(
            f"{k['key']} istek={k['requests']} ok={k['ok']} hata={k['fail']} ort={k['avg_ms']}ms"
            for k in s["by_key"]))
    else:
        p("  anahtar dagilimi: yok (/api/stats sayaci artmadi)")
    d = s["diag_delta"]
    p(f"  /api/diag  tur {diag_b.get('turns')} -> {diag_a.get('turns')} (+{d['turns']}) - "
      f"bos {diag_b.get('empty_turns')} -> {diag_a.get('empty_turns')} (+{d['empty_turns']}) - "
      f"bos-akis {diag_b.get('empty_stream_turns')} -> {diag_a.get('empty_stream_turns')} "
      f"(+{d['empty_stream_turns']}) - "
      f"kirpilan {diag_b.get('truncated_turns')} -> {diag_a.get('truncated_turns')} (+{d['truncated_turns']})")
    clamps = sorted({(r["client_max_tokens"], r["sent_max_tokens"]) for r in rows
                     if r["client_max_tokens"] is not None})
    if clamps:
        p("  max_tokens (istemci -> gateway): " +
          " | ".join(f"{a} -> {b}" + (" [KIRPILDI]" if a and b and a != b else "") for a, b in clamps))
    else:
        p("  max_tokens: okunmadi (es zamanlilik>1 veya gateway turu saymadi)")
    un = [r for r in rows if r["diag_note"]]
    if un:
        p("  diag not: " + " | ".join(f"#{r['i']} {r['diag_note']}" for r in un[:3]))
    p("")
    p("NOT: 'zincir chain_down' = gateway'deki 'api_error' SSE'si; tum modeller "
      "tukendi. 'model_flaky' = ilk model dustu, zincirdeki digeri cevap verdi.")
    p("NOT: es zamanlilik>1'de diag 'last' tek satira baglanamaz; satir bazli "
      "model/token kirilimi sadece ser kosuda okunur.")


# ------------------------------------------------------------------- main
async def _amain(a) -> int:
    timeout = httpx.Timeout(a.timeout, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        # 1) on kontrol: gateway ayakta mi?
        try:
            r = await client.get(a.base + "/api/conn", timeout=8)
            r.raise_for_status()
            conn = r.json()
        except Exception as e:
            print(f"HATA: gateway'a ulASILAMADI ({a.base}). "
                  f"Once gateway'i ayaga kaldir: py -3 launcher.py", file=sys.stderr)
            print(f"ayrinti: {type(e).__name__}: {e}", file=sys.stderr)
            return 2
        key = str(conn.get("gateway_api_key") or "")
        if not key:
            print("HATA: /api/conn gateway_api_key dondurmedi; olcum yapilamaz.", file=sys.stderr)
            return 2

        base = str(conn.get("anthropic_base_url") or a.base)
        mode = str(a.mode or conn.get("active_mode") or "1")
        provider = ((conn.get("providers") or {}).get(mode) or {}).get("name", "?")
        pm = conn.get("provider_models") or {}
        model_actual = str(conn.get("model") or "")
        model_req = str(a.model or model_actual or pm.get(mode) or "")
        if not a.model and a.mode and a.mode != str(conn.get("active_mode")):
            model_req = str(pm.get(mode) or model_actual)
            print(f"UYARI: gateway aktif modu {conn.get('active_mode')} degil, "
                  f"istenen {mode} modu icin '{model_req}' olcumleniyor. "
                  f"(Mod degistirilmedi; yonlendirme model adina bakar.)")

        # 2) taban durum: diag + key sayaclari
        try:
            diag_b = (await client.get(base + "/api/diag", timeout=10)).json()
        except Exception:
            diag_b = {}
        try:
            stats = (await client.get(base + f"/api/stats?mode={mode}", timeout=10)).json()
        except Exception:
            stats = {}
        kb = _keymap(stats)

        body = {
            "model": model_req, "max_tokens": REQ_MAX_TOKENS, "stream": True,
            "messages": [{"role": "user", "content": a.prompt}],
        }
        if not a.no_tools:
            body["tools"] = TOOLS
            body["tool_choice"] = {"type": "auto"}
        headers = {"x-api-key": key, "authorization": "Bearer " + key,
                   "content-type": "application/json", "accept": "text/event-stream"}

        n = max(1, a.n)
        conc = max(1, min(a.concurrency, n, MAX_CONCURRENCY))
        rows: list[dict] = []
        t0 = time.perf_counter()
        await _run(client, base + "/v1/messages", headers, body, rows, n, conc,
                   a.timeout, base + "/api/diag", serial=(conc == 1))
        wall = _ms(t0)

        try:
            diag_a = (await client.get(base + "/api/diag", timeout=10)).json()
        except Exception:
            diag_a = {}
        try:
            stats_a = (await client.get(base + f"/api/stats?mode={mode}", timeout=10)).json()
        except Exception:
            stats_a = {}
        ka = _keymap(stats_a)

    s = _summarize(rows, diag_b, diag_a, kb, ka)
    s["wall_ms"] = wall
    rec = {
        "schema": SCHEMA, "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "mode": mode, "provider": provider,
        "model_requested": model_req, "model_active": model_actual,
        "model_used": s.get("model_used"), "fallback": s.get("fallback"),
        "n": n, "concurrency": conc, "timeout": a.timeout, "tools": not a.no_tools,
        "max_tokens_requested": REQ_MAX_TOKENS,
        "diag_before": {k: diag_b.get(k) for k in DIAG_KEYS},
        "diag_after": {k: diag_a.get(k) for k in DIAG_KEYS},
        "summary": s, "rows": sorted(rows, key=lambda x: x["i"]),
    }
    if a.out:
        try:
            pth = Path(a.out)
            pth.parent.mkdir(parents=True, exist_ok=True)
            with pth.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception as e:
            print(f"UYARI: --out yazilamadi ({e})", file=sys.stderr)
    if a.json:
        print(json.dumps(rec, ensure_ascii=False, indent=2))
    elif not a.quiet:
        _print_human({"mode": mode, "provider": provider, "model_actual": model_actual,
                      "model_requested": model_req, "total_keys": stats.get("total_keys"),
                      "active_keys": stats.get("active_keys"), "concurrency": conc,
                      "timeout": a.timeout, "no_tools": a.no_tools},
                     s, rows, diag_b, diag_a)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="bench.py",
                                description="ox-gateway gecikme/guvenilirlik olcumu")
    p.add_argument("--model", help="olculecek model (varsayilan: /api/conn)")
    p.add_argument("--mode", choices=["1", "2"], help="1=Atria 2=OpenRouter")
    p.add_argument("--n", type=int, default=5, help="istek sayisi")
    p.add_argument("--concurrency", type=int, default=3, help="es zamanli istek")
    p.add_argument("--prompt", default=DEFAULT_PROMPT)
    p.add_argument("--no-tools", action="store_true")
    p.add_argument("--timeout", type=float, default=90.0, help="istek basina sert sn")
    p.add_argument("--json", action="store_true", help="makine-okunur cikti")
    p.add_argument("--out", help="JSON satiri ekle (trend biriktirme)")
    p.add_argument("--quiet", action="store_true", help="insan ciktisini bastirma")
    p.add_argument("--base", default=DEFAULT_BASE, help=argparse.SUPPRESS)
    a = p.parse_args()
    if a.timeout <= 0:
        p.error("--timeout 0'dan buyuk olmali")
    try:
        return asyncio.run(_amain(a))
    except KeyboardInterrupt:
        print("durduruldu", file=sys.stderr)
        return 130
    except Exception as e:  # traceback siz, anlasilir mesaj
        print(f"HATA: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
