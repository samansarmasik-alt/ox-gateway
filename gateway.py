# -*- coding: utf-8 -*-
"""
ox-gateway: Birden fazla OpenRouter API key'ini tek bir local API proxy'de birlestirir.
- Tum istekler tek modele (varsayilan: ox-alpha) gider.
- Round-robin key dagitimi + hata/429 durumunda otomatik failover.
- OpenRouter key'leri Fernet ile SIFRELI olarak vault.json'da saklanir (secret.key anahtari).
- Sabit proxy baglantisi: base_url = http://127.0.0.1:8756/v1  + gateway api key
  (api key yalnizca dashboard'daki "Yeni Uret" butonuna basilirsa degisir)
- Her key icin istek sayisi ve yanit suresi (ms) istatistigi tutulur.
- Web dashboard: http://127.0.0.1:8756

Calistirma:  py -3 -m uvicorn gateway:app --port 8756  (veya start.bat)
"""
import asyncio
import itertools
import json
import os
import secrets
import shutil
import time
from pathlib import Path

import httpx
from cryptography.fernet import Fernet
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
DASHBOARD = BASE_DIR / "static" / "index.html"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
START_TIME = time.time()

# Sifreli kasa AppData'da: proje GitHub'a itilse bile keyler asla gitmez
APPDATA_DIR = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming")) / "ox-gateway"
APPDATA_DIR.mkdir(parents=True, exist_ok=True)
VAULT_PATH = APPDATA_DIR / "vault.json"
SECRET_PATH = APPDATA_DIR / "secret.key"

# Eski konumdaki kasa varsa AppData'ya tasimigrate et
_old_vault = BASE_DIR / "vault.json"
_old_secret = BASE_DIR / "secret.key"
if _old_vault.exists() and not VAULT_PATH.exists():
    shutil.move(str(_old_vault), str(VAULT_PATH))
if _old_secret.exists() and not SECRET_PATH.exists():
    shutil.move(str(_old_secret), str(SECRET_PATH))


def load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def save_config(cfg: dict):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


# --------------------------------------------------------------------------
# Sifreli key kasasi (Fernet)
# --------------------------------------------------------------------------
def _fernet() -> Fernet:
    """secret.key yoksa uretir; varsa okur."""
    if not SECRET_PATH.exists():
        SECRET_PATH.write_bytes(Fernet.generate_key())
    return Fernet(SECRET_PATH.read_bytes())


_FERNET = _fernet()


def load_vault() -> list[str]:
    """Sifreli kasadan key'leri okur; ilk calistirmada duz keyleri migrate eder."""
    if VAULT_PATH.exists():
        try:
            blob = json.loads(VAULT_PATH.read_text(encoding="utf-8"))
            return json.loads(_FERNET.decrypt(blob["data"].encode()).decode())
        except Exception:
            return []
    # migrate: config.json icindeki duz keyleri kasaya tasi
    keys = list(CONFIG.get("api_keys") or [])
    if keys:
        save_vault(keys)
        cfg = load_config()
        cfg["api_keys"] = []
        save_config(cfg)
    return keys


def save_vault(keys: list[str]):
    """Key'leri Fernet ile sifreleyip vault.json'a yazar."""
    data = _FERNET.encrypt(json.dumps(keys).encode()).decode()
    VAULT_PATH.write_text(json.dumps({"data": data}, indent=2), encoding="utf-8")


CONFIG = load_config()

# Sabit gateway api key: ilk acilista uretilir, sonra ASLA otomatik degismez.
if not CONFIG.get("gateway_api_key"):
    CONFIG["gateway_api_key"] = "ox-" + secrets.token_urlsafe(32)
    save_config(CONFIG)

# Protokol tercihi (openai | anthropic) - dashboard'daki tek tusla degisir
if not CONFIG.get("protocol"):
    CONFIG["protocol"] = "openai"
    save_config(CONFIG)

GATEWAY_KEY: str = CONFIG["gateway_api_key"]

# --------------------------------------------------------------------------
# Pacing: upstream cagrilarina arada bekleme koyar; bekleyis ASLA pace_ms'i
# gecmez (varsayilan 100ms). Amac: key'lere yumusak davranip rate-limit
# yememek, ama gecikmeyi hissedilir etmemek.
# --------------------------------------------------------------------------
class Throttle:
    def __init__(self, pace_ms: float):
        self.pace = max(0.0, pace_ms / 1000.0)
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def wait(self):
        if self.pace <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            delay = min(self.pace - (now - self._last), self.pace)
            if delay > 0:
                await asyncio.sleep(delay)
            self._last = time.monotonic()


THROTTLE = Throttle(CONFIG.get("pace_ms", 100))


def _new_stats() -> dict:
    return {"requests": 0, "ok": 0, "fail": 0, "last_ms": None, "avg_ms": None, "_total_ms": 0}


class KeyPool:
    """Round-robin key havuzu; hatali keyleri cooldown'a atar, istatistik tutar.
    Ayrica her key 'cooldown_every' istekte bir kisa dinlenmeye girer (yuk dagilimi)."""

    def __init__(self, keys: list[str], cooldown: float,
                 cooldown_every: int = 3, rest_seconds: float = 8.0):
        self._keys = list(dict.fromkeys(keys))
        self._cooldown = cooldown
        self._every = max(1, int(cooldown_every))
        self._rest = max(0.0, rest_seconds)
        self._cooldown_until: dict[str, float] = {}
        self._stats: dict[str, dict] = {k: _new_stats() for k in self._keys}
        self._rr = itertools.cycle(range(len(self._keys))) if self._keys else None
        self._lock = asyncio.Lock()

    @property
    def keys(self) -> list[str]:
        return list(self._keys)

    def add(self, key: str) -> bool:
        key = key.strip()
        if not key or key in self._keys:
            return False
        self._keys.append(key)
        self._stats[key] = _new_stats()
        self._rr = itertools.cycle(range(len(self._keys)))
        save_vault(self._keys)
        return True

    def remove(self, key: str) -> bool:
        key = key.strip()
        if key in self._keys:
            self._keys.remove(key)
            self._stats.pop(key, None)
            self._rr = itertools.cycle(range(len(self._keys))) if self._keys else None
            save_vault(self._keys)
            return True
        return False

    def _available(self) -> list[str]:
        now = time.monotonic()
        avail = [k for k in self._keys if self._cooldown_until.get(k, 0) <= now]
        return avail or list(self._keys)

    def mark_ok(self, key: str):
        self._cooldown_until.pop(key, None)

    def mark_failed(self, key: str):
        self._cooldown_until[key] = time.monotonic() + self._cooldown

    def record(self, key: str, ms: float, ok: bool):
        s = self._stats.setdefault(key, _new_stats())
        s["requests"] += 1
        s["ok" if ok else "fail"] += 1
        s["last_ms"] = round(ms, 1)
        s["_total_ms"] += ms
        s["avg_ms"] = round(s["_total_ms"] / s["requests"], 1)
        # Her N istekte bir kisa dinlenme: yuk diger keylere otomatik döner
        s["uses_since_rest"] = s.get("uses_since_rest", 0) + 1
        if self._rest > 0 and s["uses_since_rest"] >= self._every:
            s["uses_since_rest"] = 0
            self._cooldown_until[key] = time.monotonic() + self._rest

    async def acquire(self, skip: set[str] | None = None) -> str | None:
        """Musait tum keylere ESIT round-robin dagitim; failover icin skip kullanilir."""
        async with self._lock:
            skip = skip or set()
            cands = [k for k in self._available() if k not in skip]
            if not cands:
                return None
            if self._rr is None:
                return cands[0]
            for _ in range(len(self._keys)):
                idx = next(self._rr)
                if self._keys[idx] in cands:
                    return self._keys[idx]
            return cands[0]

    def status(self) -> list[dict]:
        now = time.monotonic()
        out = []
        for k in self._keys:
            s = self._stats.get(k, _new_stats())
            cd = max(0, round(self._cooldown_until.get(k, 0) - now, 1))
            st = {x: s[x] for x in ("requests", "ok", "fail", "last_ms", "avg_ms")}
            st.update({
                "key": k[:10] + "..." + k[-4:] if len(k) > 18 else k,
                "full": k,
                "state": "active" if cd <= 0 else "cooldown",
                "cooldown_remaining": cd,
            })
            out.append(st)
        return out


POOL = KeyPool(
    load_vault(),
    CONFIG.get("cooldown_seconds", 30),
    cooldown_every=CONFIG.get("cooldown_every", 3),
    rest_seconds=CONFIG.get("rest_seconds", 8),
)


def _timeouts() -> httpx.Timeout:
    """Ayri timeout'lar: baglanti hizli kurulsun, okuma icin config siniri olsun."""
    return httpx.Timeout(
        connect=10.0,
        write=30.0,
        read=float(CONFIG.get("request_timeout", 120)),
        pool=10.0,
    )


async def call_openrouter(payload: dict, model: str | None = None) -> dict:
    """Istegi havuzdan key alarak OpenRouter'a yollar; basarisizsa diger keyi dener.
    OpenRouter'in OpenAI-uyumlu ham cevabini oldugu gibi dondurur."""
    payload = {**payload, "model": model or CONFIG["model"]}
    max_retries = CONFIG.get("max_retries", 3)
    tried: set[str] = set()
    last_err = None

    for _ in range(max_retries):
        key = await POOL.acquire(skip=tried)
        if key is None:
            break
        tried.add(key)
        await THROTTLE.wait()
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        t0 = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=_timeouts()) as client:
                resp = await client.post(OPENROUTER_URL, json=payload, headers=headers)
            ms = (time.perf_counter() - t0) * 1000
            if resp.status_code == 200:
                POOL.mark_ok(key)
                POOL.record(key, ms, ok=True)
                return resp.json()
            last_err = f"HTTP {resp.status_code}: {resp.text[:300]}"
            POOL.mark_failed(key)
            POOL.record(key, ms, ok=False)
        except httpx.HTTPError as e:
            ms = (time.perf_counter() - t0) * 1000
            last_err = f"Network error: {e}"
            POOL.mark_failed(key)
            POOL.record(key, ms, ok=False)

    raise HTTPException(status_code=502, detail=f"Tum keyler basarisiz. Son hata: {last_err}")


async def open_stream(payload: dict, model: str | None = None):
    """Streaming icin upstream baglantisi acar.
    - Acilmadan once failover yapar.
    - Ilk token first_token_ms icinde gelmezse key'i cooldown'a atip diger keyi dener.
    Donus: (client, resp, line_iter, ilk_satir) — cagiran kapatir."""
    payload = {**payload, "model": model or CONFIG["model"]}
    max_retries = CONFIG.get("max_retries", 3)
    first_token_s = max(1.0, float(CONFIG.get("first_token_ms", 20000)) / 1000.0)
    tried: set[str] = set()
    last_err = None

    for _ in range(max_retries):
        key = await POOL.acquire(skip=tried)
        if key is None:
            break
        tried.add(key)
        await THROTTLE.wait()
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        t0 = time.perf_counter()
        client = httpx.AsyncClient(timeout=_timeouts())
        try:
            req = client.build_request("POST", OPENROUTER_URL, json=payload, headers=headers)
            resp = await client.send(req, stream=True)
            if resp.status_code != 200:
                ms = (time.perf_counter() - t0) * 1000
                last_err = f"HTTP {resp.status_code}: {(await resp.aread()).decode()[:300]}"
                await resp.aclose()
                await client.aclose()
                POOL.mark_failed(key)
                POOL.record(key, ms, ok=False)
                continue

            # Ilk token bekleniyor; gec kalirsa bu key yavas -> failover
            line_iter = resp.aiter_lines()
            try:
                first_line = await asyncio.wait_for(line_iter.__anext__(), timeout=first_token_s)
            except asyncio.TimeoutError:
                ms = (time.perf_counter() - t0) * 1000
                last_err = f"ilk token {first_token_s:.0f}s icinde gelmedi"
                await resp.aclose()
                await client.aclose()
                POOL.mark_failed(key)
                POOL.record(key, ms, ok=False)
                continue

            ms = (time.perf_counter() - t0) * 1000
            POOL.mark_ok(key)
            POOL.record(key, ms, ok=True)  # time-to-first-byte
            return client, resp, line_iter, first_line
        except httpx.HTTPError as e:
            try:
                await client.aclose()
            except Exception:
                pass
            last_err = f"Network error: {e}"
            POOL.mark_failed(key)

    raise HTTPException(status_code=502, detail=f"Tum keyler basarisiz. Son hata: {last_err}")


app = FastAPI(title="ox-gateway", version="1.1")


class Message(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: list[Message]
    model: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    stream: bool = False


class AgentRequest(BaseModel):
    system: str = "You are a helpful sub-agent."
    task: str
    model: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None


class ParallelAgentRequest(BaseModel):
    agents: list[AgentRequest] = Field(..., min_length=1)
    max_concurrency: int = 8


@app.get("/")
async def dashboard():
    """Web arayuzu."""
    if DASHBOARD.exists():
        return FileResponse(DASHBOARD)
    return {"service": "ox-gateway", "model": CONFIG["model"], "keys": len(POOL.keys)}


@app.get("/api/stats")
async def api_stats():
    """Dashboard'un canlı dinlediği özet."""
    ks = POOL.status()
    return {
        "model": CONFIG["model"],
        "uptime_s": round(time.time() - START_TIME),
        "total_keys": len(ks),
        "active_keys": sum(1 for k in ks if k["state"] == "active"),
        "total_requests": sum(k["requests"] for k in ks),
        "total_fail": sum(k["fail"] for k in ks),
        "keys": ks,
    }


# --------------------------------------------------------------------------
# Proxy auth: sadece /v1/* proxy endpoint'leri gateway key ister.
# Dashboard ve yonetim endpoint'leri localhost'ta aciktir.
# --------------------------------------------------------------------------
def check_auth(request: Request):
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if not token:
        token = request.headers.get("x-api-key", "") or request.query_params.get("api_key", "")
    if token != GATEWAY_KEY:
        raise HTTPException(401, "Gecersiz gateway API key")


def get_gateway_key() -> str:
    return GATEWAY_KEY


@app.post("/api/rotate")
async def rotate_gateway_key():
    """SADECE bu cagri ile gateway api key yenilenir; aksi halde sabit kalir."""
    global GATEWAY_KEY
    GATEWAY_KEY = "ox-" + secrets.token_urlsafe(32)
    cfg = load_config()
    cfg["gateway_api_key"] = GATEWAY_KEY
    save_config(cfg)
    return {"rotated": True, "gateway_api_key": GATEWAY_KEY}


@app.get("/api/conn")
async def conn_info():
    """Dashboard'un gosterdigi baglanti bilgisi (secili protokole gore)."""
    proto = CONFIG.get("protocol", "openai")
    return {
        "protocol": proto,
        "base_url": "http://127.0.0.1:8756/v1",
        "anthropic_base_url": "http://127.0.0.1:8756",
        "gateway_api_key": GATEWAY_KEY,
        "model": CONFIG["model"],
    }


class ProtocolSetRequest(BaseModel):
    protocol: str


@app.post("/protocol/set")
async def set_protocol(req: ProtocolSetRequest):
    """Tek tusla protokol degistirimi; kalici kaydedilir."""
    p = req.protocol.strip().lower()
    if p not in ("openai", "anthropic"):
        raise HTTPException(400, "protokol openai veya anthropic olmali")
    cfg = load_config()
    cfg["protocol"] = p
    save_config(cfg)
    CONFIG["protocol"] = p
    return {"set": True, "protocol": p}


@app.get("/keys")
async def keys_status():
    return {"model": CONFIG["model"], "total": len(POOL.keys), "keys": POOL.status()}


# --------------------------------------------------------------------------
# Anthropic Messages API uyumlulugu (/v1/messages)
# --------------------------------------------------------------------------
def _anthropic_to_openai(body: dict) -> dict:
    """Anthropic istek formatini OpenAI formatina cevirir."""
    msgs: list[dict] = []
    sys = body.get("system")
    if sys:
        if isinstance(sys, list):
            sys = " ".join(b.get("text", "") for b in sys if isinstance(b, dict))
        msgs.append({"role": "system", "content": sys})
    for m in body.get("messages", []):
        c = m.get("content")
        if isinstance(c, list):
            parts: list[str] = []
            for b in c:
                if not isinstance(b, dict):
                    continue
                t = b.get("type")
                if t == "text":
                    parts.append(b.get("text", ""))
                elif t == "tool_result":
                    rc = b.get("content", "")
                    if isinstance(rc, list):
                        rc = " ".join(x.get("text", "") for x in rc if isinstance(x, dict))
                    parts.append(f"[tool_result] {rc}")
                elif t == "thinking":
                    parts.append(b.get("thinking", ""))
            c = "\n".join(parts)
        msgs.append({"role": m.get("role", "user"), "content": c or ""})
    out: dict = {"messages": msgs, "max_tokens": body.get("max_tokens") or 1024}
    if body.get("temperature") is not None:
        out["temperature"] = body["temperature"]
    # tool tanimlarini cevir (Claude Code agentic calissin diye)
    tools = body.get("tools")
    if isinstance(tools, list):
        out["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": t.get("name", ""),
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema", {}),
                },
            }
            for t in tools if isinstance(t, dict) and t.get("name")
        ]
    return out


def _reasoning_config(body: dict) -> dict | None:
    """Anthropic 'thinking' alanini OpenRouter reasoning'e cevirir.
    Agentic araclar dusunme metniyle calisamadigi icin reasoning'i minimal butceye
    indir (hizli, sade cevap, output ile karismaz)."""
    return {"max_tokens": 64}


_ANTHROPIC_STOP = {"stop": "end_turn", "length": "max_tokens", "tool_calls": "tool_use",
                   "stop_sequence": "stop_sequence", "content_filter": "end_turn"}


def _anthropic_stop(finish: str | None) -> str:
    return _ANTHROPIC_STOP.get(finish or "stop", "end_turn")


def _openai_to_anthropic_blocks(message: dict) -> list[dict]:
    """OpenAI cevap mesajini Anthropic content bloklarina cevirir."""
    blocks: list[dict] = []
    if message.get("content"):
        blocks.append({"type": "text", "text": message["content"]})
    for tc in message.get("tool_calls") or []:
        fn = tc.get("function", {})
        blocks.append({
            "type": "tool_use",
            "id": tc.get("id", ""),
            "name": fn.get("name", ""),
            "input": json.loads(fn.get("arguments", "{}")) if fn.get("arguments") else {},
        })
    return blocks or [{"type": "text", "text": ""}]


def _sse_event(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


# Claude Code / Anthropic SDK: base_url olarak sadece http://127.0.0.1:8756
# verilir. SDK kendisi /v1/messages ekler; biz de geriye donukluk icin /messages
# yolunu destekliyoruz (ikisi de calisir).
@app.post("/messages")
async def anthropic_messages_root(request: Request):
    return await anthropic_messages(request)


@app.post("/v1/messages")
async def anthropic_messages(request: Request):
    """Anthropic Messages API uyumlu proxy noktasi.
    Header: x-api-key <gateway-key> (veya Bearer). stream destekler."""
    check_auth(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Gecersiz JSON")

    model = body.get("model") or CONFIG["model"]
    payload = _anthropic_to_openai(body)
    rc = _reasoning_config(body)
    if rc:
        payload["reasoning"] = rc
    stall_s = max(5.0, float(CONFIG.get("stall_timeout", 45)))

    # ---- Streaming: OpenAI chunk'larini Anthropic eventlerine cevir ----
    if body.get("stream"):
        payload["stream"] = True

        async def sse_anthropic():
            client, resp, line_iter, first_line = await open_stream(payload, model)
            # thinking ve text icin AYRI bloklar; her tool_call da kendi blogunda
            block_type: str | None = None   # None | "thinking" | "text" | "tool_use"
            block_index = -1
            cur_tool = None                 # su an akilan upstream tool kimligi (id/index)
            stop_reason = "end_turn"
            usage_out = 0
            msg_id = "msg_" + str(int(time.time() * 1000))

            yield _sse_event("message_start", {
                "type": "message_start",
                "message": {
                    "id": msg_id, "type": "message", "role": "assistant",
                    "model": model, "content": [],
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            })

            async def emit_block(kind: str, delta_txt: str, tool_name: str = "", tool_id: str = "", force: bool = False):
                nonlocal block_type, block_index
                if block_type != kind or force:
                    if block_type is not None:
                        yield _sse_event("content_block_stop", {
                            "type": "content_block_stop", "index": block_index})
                    block_index += 1
                    block_type = kind
                    if kind == "thinking":
                        yield _sse_event("content_block_start", {
                            "type": "content_block_start", "index": block_index,
                            "content_block": {"type": "thinking", "thinking": ""},
                        })
                    elif kind == "tool_use":
                        yield _sse_event("content_block_start", {
                            "type": "content_block_start", "index": block_index,
                            "content_block": {
                                "type": "tool_use",
                                "id": tool_id or f"toolu_{block_index}",
                                "name": tool_name,
                                "input": {},
                            },
                        })
                    else:
                        yield _sse_event("content_block_start", {
                            "type": "content_block_start", "index": block_index,
                            "content_block": {"type": "text", "text": ""},
                        })
                if kind == "thinking":
                    yield _sse_event("content_block_delta", {
                        "type": "content_block_delta", "index": block_index,
                        "delta": {"type": "thinking_delta", "thinking": delta_txt},
                    })
                elif kind == "tool_use":
                    pass  # argumanlar ayri input_json_delta ile akar
                else:
                    yield _sse_event("content_block_delta", {
                        "type": "content_block_delta", "index": block_index,
                        "delta": {"type": "text_delta", "text": delta_txt},
                    })

            try:
                line = first_line
                while True:
                    if line.startswith("data: ") and "[DONE]" not in line:
                        try:
                            j = json.loads(line[6:])
                        except ValueError:
                            j = None
                        if j is not None:
                            ch = (j.get("choices") or [{}])[0]
                            d = ch.get("delta") or {}
                            rt = d.get("reasoning")
                            ct = d.get("content")
                            if rt:
                                async for chunk in emit_block("thinking", rt):
                                    yield chunk
                            if ct:
                                async for chunk in emit_block("text", ct):
                                    yield chunk
                            # tool_calls stream: HER cagri ayri blok (kimlik: id/index),
                            # yoksa argumanlar tek bloga yapisip JSON bozuluyor
                            for ti, tc in enumerate(d.get("tool_calls") or []):
                                fn = tc.get("function", {})
                                tkey = str(tc.get("id") or tc.get("index", ti))
                                if fn.get("name"):
                                    force_new = (tkey != cur_tool) or (block_type != "tool_use")
                                    async for chunk in emit_block(
                                        "tool_use", "",
                                        tool_name=fn["name"],
                                        tool_id=tc.get("id", ""),
                                        force=force_new,
                                    ):
                                        yield chunk
                                    cur_tool = tkey
                                if fn.get("arguments") and block_type == "tool_use":
                                    yield _sse_event("content_block_delta", {
                                        "type": "content_block_delta", "index": block_index,
                                        "delta": {"type": "input_json_delta",
                                                  "partial_json": fn["arguments"]},
                                    })
                            fr = ch.get("finish_reason")
                            if fr:
                                stop_reason = _anthropic_stop(fr)
                            if j.get("usage"):
                                usage_out = j["usage"].get("completion_tokens", usage_out)
                    elif line.startswith("data: [DONE]"):
                        break
                    try:
                        line = await asyncio.wait_for(line_iter.__anext__(), timeout=stall_s)
                    except StopAsyncIteration:
                        break
                    except asyncio.TimeoutError:
                        yield _sse_event("error", {
                            "type": "error",
                            "error": {"type": "timeout_error",
                                      "message": f"stream {stall_s:.0f}s ver gelmeyince kapatildi"},
                        })
                        break
                if block_type is not None:
                    yield _sse_event("content_block_stop", {
                        "type": "content_block_stop", "index": block_index})
                yield _sse_event("message_delta", {
                    "type": "message_delta",
                    "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                    "usage": {"output_tokens": usage_out},
                })
                yield _sse_event("message_stop", {"type": "message_stop"})
            finally:
                await resp.aclose()
                await client.aclose()

        return StreamingResponse(
            sse_anthropic(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ---- Normal ----
    result = await call_openrouter(payload, model)
    msg = result["choices"][0]["message"]
    blocks = _openai_to_anthropic_blocks(msg)
    u = result.get("usage", {})
    finish = result["choices"][0].get("finish_reason")
    return {
        "id": "msg_" + str(result.get("id", "")),
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": blocks,
        "stop_reason": _anthropic_stop(finish),
        "usage": {
            "input_tokens": u.get("prompt_tokens", 0),
            "output_tokens": u.get("completion_tokens", 0),
        },
    }


class KeyAddRequest(BaseModel):
    key: str


@app.post("/keys/add")
async def keys_add(req: KeyAddRequest):
    return {"added": POOL.add(req.key), "total": len(POOL.keys)}


class KeyRemoveRequest(BaseModel):
    key: str


@app.post("/keys/remove")
async def keys_remove(req: KeyRemoveRequest):
    return {"removed": POOL.remove(req.key), "total": len(POOL.keys)}


@app.get("/v1/models")
async def list_models(request: Request):
    """OpenAI-uyumlu model listesi: varsayilan en ustte, sonra ucretsizler."""
    check_auth(request)
    return await models_payload()


# --------------------------------------------------------------------------
# Model yonetimi: OpenRouter'dan otomatik cekilir, default + free onde.
# --------------------------------------------------------------------------
MODELS_CACHE: dict = {"data": None, "ts": 0.0}


async def fetch_openrouter_models() -> list[dict]:
    if MODELS_CACHE["data"] and time.time() - MODELS_CACHE["ts"] < 600:
        return MODELS_CACHE["data"]
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get("https://openrouter.ai/api/v1/models")
        data = r.json().get("data", [])
        MODELS_CACHE["data"] = data
        MODELS_CACHE["ts"] = time.time()
        return data
    except httpx.HTTPError:
        return MODELS_CACHE["data"] or []


def _is_free(m: dict) -> bool:
    p = m.get("pricing") or {}
    try:
        return float(p.get("prompt", 1)) == 0 and float(p.get("completion", 1)) == 0
    except (TypeError, ValueError):
        return False


async def models_payload() -> dict:
    raw = await fetch_openrouter_models()
    default = CONFIG["model"]
    items = [
        {
            "id": m["id"],
            "name": m.get("name", m["id"]),
            "free": _is_free(m),
            "context": m.get("context_length"),
        }
        for m in raw
        if isinstance(m, dict) and m.get("id")
    ]
    items.sort(key=lambda x: x["name"].lower())
    free_first = [m for m in items if m["free"]] + [m for m in items if not m["free"]]
    ordered = [{"id": default, "name": f"⭐ {default} (varsayılan)", "free": False, "default": True}] + \
              [m for m in free_first if m["id"] != default]
    return {"default": default, "total": len(ordered), "models": ordered}


@app.get("/api/models")
async def api_models():
    """Dashboard icin: varsayilan + free once olacak sekilde sirali liste."""
    return await models_payload()


class ModelSetRequest(BaseModel):
    model: str


@app.post("/model/set")
async def set_model(req: ModelSetRequest):
    cfg = load_config()
    cfg["model"] = req.model.strip()
    save_config(cfg)
    CONFIG["model"] = cfg["model"]
    MODELS_CACHE["ts"] = 0  # siralamayi tazele
    return {"set": True, "model": CONFIG["model"]}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request, req: ChatRequest):
    """OpenAI-uyumlu proxy noktasi; gateway api key ister. stream:true -> SSE."""
    check_auth(request)
    payload: dict = {"messages": [m.model_dump() for m in req.messages]}
    if req.temperature is not None:
        payload["temperature"] = req.temperature
    if req.max_tokens is not None:
        payload["max_tokens"] = req.max_tokens

    # ---- Streaming (SSE) ----
    if req.stream:
        payload["stream"] = True
        stall_s = max(5.0, float(CONFIG.get("stall_timeout", 45)))

        async def sse():
            client, resp, line_iter, first_line = await open_stream(payload, req.model)
            done_sent = False
            try:
                line = first_line
                while True:
                    line = (line or "").strip()
                    if line:
                        if "[DONE]" in line:
                            if not done_sent:
                                done_sent = True
                                yield ("data: [DONE]\n\n").encode()
                        else:
                            yield (line + "\n\n").encode()
                    if done_sent:
                        break
                    try:
                        line = await asyncio.wait_for(line_iter.__anext__(), timeout=stall_s)
                    except StopAsyncIteration:
                        break
                    except asyncio.TimeoutError:
                        # akis takildi -> istemciyi bilgilendirip kes
                        err = {"error": {"message": f"stream {stall_s:.0f}s ver gelmeyince kapatildi"}}
                        yield ("data: " + json.dumps(err) + "\n\n").encode()
                        yield b"data: [DONE]\n\n"
                        break
                if not done_sent:
                    yield b"data: [DONE]\n\n"
            finally:
                await resp.aclose()
                await client.aclose()

        return StreamingResponse(
            sse(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # ---- Normal (OpenRouter'in OpenAI-uyumlu cevabi aynen gecer) ----
    result = await call_openrouter(payload, req.model)
    return result


@app.post("/agent/run")
async def agent_run(req: AgentRequest):
    """Tek sub-agent: system + task -> cevap metni."""
    payload: dict = {
        "messages": [
            {"role": "system", "content": req.system},
            {"role": "user", "content": req.task},
        ]
    }
    if req.temperature is not None:
        payload["temperature"] = req.temperature
    if req.max_tokens is not None:
        payload["max_tokens"] = req.max_tokens
    result = await call_openrouter(payload, req.model)
    try:
        msg = result["choices"][0]["message"]
        text = msg.get("content") or msg.get("reasoning") or ""
    except (KeyError, IndexError):
        text = json.dumps(result)[:500]
    return {"model": CONFIG["model"], "output": text, "usage": result.get("usage", {})}


@app.post("/agent/parallel")
async def agent_parallel(req: ParallelAgentRequest):
    """N sub-agent'i paralel calistirir; round-robin ile keylere dagilir."""
    sem = asyncio.Semaphore(req.max_concurrency)

    async def one(i: int, agent: AgentRequest) -> dict:
        async with sem:
            payload: dict = {
                "messages": [
                    {"role": "system", "content": agent.system},
                    {"role": "user", "content": agent.task},
                ]
            }
            if agent.temperature is not None:
                payload["temperature"] = agent.temperature
            if agent.max_tokens is not None:
                payload["max_tokens"] = agent.max_tokens
            try:
                result = await call_openrouter(payload, agent.model)
                msg = result["choices"][0]["message"]
                text = msg.get("content") or msg.get("reasoning") or ""
                return {
                    "index": i,
                    "ok": True,
                    "output": text,
                    "usage": result.get("usage", {}),
                }
            except HTTPException as e:
                return {"index": i, "ok": False, "error": e.detail}

    results = await asyncio.gather(*(one(i, a) for i, a in enumerate(req.agents)))
    return {"model": CONFIG["model"], "total": len(results), "results": list(results)}
