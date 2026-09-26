# -*- coding: utf-8 -*-
"""
ox-gateway: Birden fazla API key'ini tek bir local API proxy'de birlestirir.
- Mod 1 (Atria) ve Mod 2 (OpenRouter) ayri key havuzlarina gider.
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
import threading
import time
from pathlib import Path
from typing import Any

import httpx
from cryptography.fernet import Fernet
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
DASHBOARD = BASE_DIR / "static" / "index.html"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
ATRIA_URL = "https://api.atria-asi.ai/v1/messages"
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
    """Eski tek-havuz okuma (geriye uyumluluk); aktif modun keylerini dondurur."""
    d = load_vault_dict()
    return list(d.get(str(CONFIG.get("active_mode", "1")), []))


def save_vault(keys: list[str]):
    """Eski tek-havuz yazma (geriye uyumluluk); aktif moda yazar."""
    save_vault_mode(str(CONFIG.get("active_mode", "1")), keys)


_VAULT_LOCK = threading.RLock()


class VaultError(RuntimeError):
    """Kasa okunamadi/ cozulemedi. ANAHTAR KAYBI riski olan durum."""


def _vault_backup_path() -> Path:
    return VAULT_PATH.with_suffix(".json.corrupt")


def load_vault_dict() -> dict:
    """Sifreli kasadan mod bazli key'leri okur: {"1": [...], "2": [...]}.

    Eski format (duz liste) gorulurse: tum keyler 2. moda (OpenRouter)
    tasinir, 1. mod (Atria) bos kalir. Eski keyler ASLA silinmez.

    ONEMLI: decrypt/cozme hatasi ARTIK sessizce bos liste dondurmuyor.
    Onceki surum `return {"1": [], "2": []}` yapip bir sonraki yazmada gercek
    kasayi UZERINE YAZIYORDU -> secret.key bozulunca tum key'ler kalici gidiyordu.
    Artik hata yukari firlatilir ve kasa .corrupt yedeklenir."""
    if VAULT_PATH.exists():
        blob = None
        try:
            blob = json.loads(VAULT_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            # okunamadi: yedekle ve yukari firlat
            _quarantine_vault(f"okunamadi: {e}")
            raise VaultError(f"vault.json okunamadi ({e}); yedek: {_vault_backup_path()}")
        try:
            raw = json.loads(_FERNET.decrypt(blob["data"].encode()).decode())
        except Exception as e:
            _quarantine_vault(f"cozulemedi: {e}")
            raise VaultError(
                "vault.json COZULEMEDI. secret.key degismis olabilir. "
                f"Kasa kaybolmadi, yedeklendi: {_vault_backup_path()}. "
                "Eski secret.key'i geri koyup tekrar dene.")
        if isinstance(raw, dict):
            return {
                "1": list(raw.get("1") or []),
                "2": list(raw.get("2") or []),
            }
        if isinstance(raw, list):
            migrated = {"1": [], "2": list(raw)}
            save_vault_dict(migrated)
            return migrated
    # migrate: config.json icindeki duz keyleri 2. moda tasi
    keys = list(CONFIG.get("api_keys") or [])
    out = {"1": [], "2": keys}
    if keys:
        save_vault_dict(out)
        cfg = load_config()
        cfg["api_keys"] = []
        save_config(cfg)
    return out


def _quarantine_vault(reason: str) -> None:
    """Bozuk kasayi .corrupt olarak yedekle; ASLA silme/uzerine yazma."""
    try:
        if VAULT_PATH.exists():
            bak = _vault_backup_path()
            bak.write_bytes(VAULT_PATH.read_bytes())
            print(f"[ox-gateway] !! KASA SORUNU ({reason}) -> yedeklendi: {bak}")
    except Exception as e:
        print(f"[ox-gateway] !! kasa yedeklenemedi: {e}")


def save_vault_dict(vault: dict) -> None:
    """Mod bazli key'leri Fernet ile sifreleyip vault.json'a yazar (atomik)."""
    clean = {"1": list(vault.get("1") or []), "2": list(vault.get("2") or [])}
    data = _FERNET.encrypt(json.dumps(clean).encode()).decode()
    payload = json.dumps({"data": data}, indent=2)
    # yarisma (race) ve yarim yazimayi onlemek icin: ayni dosyaya yazip atomik degistir
    with _VAULT_LOCK:
        tmp = VAULT_PATH.with_suffix(".json.tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(str(tmp), str(VAULT_PATH))


def save_vault_mode(mode: str, keys: list[str]):
    """Tek modun keylerini gunceller, diger modun keylerine dokunmaz.

    Lock'lu: eski surumde load->modify->save yarismasi vardi; Mod 1 ve Mod 2'ye
    es zamanli /keys/add cagrisinda biri digerini eziyordu."""
    with _VAULT_LOCK:
        d = load_vault_dict()
        d[str(mode)] = list(keys)
        save_vault_dict(d)


CONFIG = load_config()

# Sabit gateway api key: ilk acilista uretilir, sonra ASLA otomatik degismez.
if not CONFIG.get("gateway_api_key"):
    CONFIG["gateway_api_key"] = "ox-" + secrets.token_urlsafe(32)
    save_config(CONFIG)

# Protokol tercihi (openai | anthropic) - dashboard'daki tek tusla degisir
if not CONFIG.get("protocol"):
    CONFIG["protocol"] = "openai"
    save_config(CONFIG)

# Provider/mod tercihi ("1" = Atria, "2" = OpenRouter) + mod bazli modeller
_changed = False
if str(CONFIG.get("active_mode", "")) not in ("1", "2"):
    CONFIG["active_mode"] = "1"
    _changed = True
if not isinstance(CONFIG.get("provider_models"), dict):
    CONFIG["provider_models"] = {}
    _changed = True
if not CONFIG["provider_models"].get("2"):
    CONFIG["provider_models"]["2"] = CONFIG.get("model") or "stealth/space-bunny-alpha"
    _changed = True
if not CONFIG["provider_models"].get("1"):
    CONFIG["provider_models"]["1"] = CONFIG.get("model") or "Atria-Dawn-Preview"
    _changed = True
if _changed:
    save_config(CONFIG)

GATEWAY_KEY: str = CONFIG["gateway_api_key"]

# Provider/mod tanimlari: 1. mod Atria (/v1/messages, Anthropic format),
# 2. mod OpenRouter (/v1/chat/completions, OpenAI format). Her modun
# key havuzu ayridir; eski keyler 2. modda durur.
PROVIDERS: dict = {
    "1": {"name": "atria", "url": ATRIA_URL, "kind": "anthropic"},
    "2": {"name": "openrouter", "url": OPENROUTER_URL, "kind": "openai"},
}


def get_active_mode() -> str:
    m = str(CONFIG.get("active_mode", "1"))
    return m if m in ("1", "2") else "1"


def get_provider(mode: str | None = None) -> dict:
    return PROVIDERS.get(str(mode or get_active_mode()), PROVIDERS["1"])


def get_active_model() -> str:
    pm = CONFIG.get("provider_models") or {}
    return pm.get(get_active_mode()) or CONFIG.get("model") or "Atria-Dawn-Preview"


def _is_atria_model(model: str | None) -> bool:
    """Model adi Atria'yI ima ediyorsa Atria provider'a yonlendir (mode'dan bagimsiz)."""
    if not model:
        return False
    m = model.lower()
    return "atria" in m


def _should_route_atria(model: str | None) -> bool:
    """Aktif mod 1 ise hep Atria; degilse model adi Atria ise yine Atria."""
    if get_active_mode() == "1":
        return True
    return _is_atria_model(model)

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


MAX_COOLDOWN_S = 3.0  # normal failed/rest cooldown'lar icin cap (hizli failover)
MAX_RATE_LIMIT_COOLDOWN_S = 3600.0  # 429 rate-limit (ozellikle gunluk limit) icin cap — 1 saate kadar


class KeyPool:
    """Round-robin key havuzu; hatali keyleri cooldown'a atar, istatistik tutar.
    Ayrica her key 'cooldown_every' istekte bir kisa dinlenmeye girer (yuk dagilimi).
    Tum cooldown/rest sureleri MAX_COOLDOWN_S ile sinirlidir."""

    def __init__(self, keys: list[str], cooldown: float,
                 cooldown_every: int = 3, rest_seconds: float = 3.0,
                 on_change=None):
        self._on_change = on_change
        self._keys = list(dict.fromkeys(keys))
        self._cooldown = min(max(0.0, cooldown), MAX_COOLDOWN_S)
        self._every = max(1, int(cooldown_every))
        self._rest = min(max(0.0, rest_seconds), MAX_COOLDOWN_S)
        self._cooldown_until: dict[str, float] = {}
        self._stats: dict[str, dict] = {k: _new_stats() for k in self._keys}
        self._rr = itertools.cycle(range(len(self._keys))) if self._keys else None
        self._lock = asyncio.Lock()

    @property
    def keys(self) -> list[str]:
        return list(self._keys)

    def _persist(self):
        if self._on_change is not None:
            try:
                self._on_change(list(self._keys))
            except Exception:
                pass

    def add(self, key: str) -> bool:
        key = key.strip()
        if not key or key in self._keys:
            return False
        self._keys.append(key)
        self._stats[key] = _new_stats()
        self._rr = itertools.cycle(range(len(self._keys)))
        self._persist()
        return True

    def remove(self, key: str) -> bool:
        key = key.strip()
        if key in self._keys:
            self._keys.remove(key)
            self._stats.pop(key, None)
            self._rr = itertools.cycle(range(len(self._keys))) if self._keys else None
            self._persist()
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

    def mark_rate_limited(self, key: str, seconds: float):
        """429 gibi durumlarda; gunluk limit icin MAX_RATE_LIMIT_COOLDOWN_S'a kadar tutar."""
        self._cooldown_until[key] = time.monotonic() + min(
            max(self._cooldown, seconds), MAX_RATE_LIMIT_COOLDOWN_S)

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


try:
    _VAULT = load_vault_dict()
    VAULT_ERROR: str | None = None
except VaultError as e:
    # Sunucu yine ayaga kalsin ama kasa UZERINE YAZILMASIN.
    _VAULT = {"1": [], "2": []}
    VAULT_ERROR = str(e)
    print("[ox-gateway] !! BASLATILDI AMA KEY HAVUZU BOS: " + str(e))

POOLS: dict[str, KeyPool] = {}
for _m in ("1", "2"):
    _mode = _m  # closure icin sabitle

    def _make_saver(m: str):
        def _saver(keys: list[str], _m=m):
            save_vault_mode(_m, keys)
        return _saver

    POOLS[_m] = KeyPool(
        _VAULT.get(_m) or [],
        CONFIG.get("cooldown_seconds", 3),
        cooldown_every=CONFIG.get("cooldown_every", 3),
        rest_seconds=CONFIG.get("rest_seconds", 3),
        on_change=_make_saver(_m),
    )


def active_pool() -> KeyPool:
    """Aktif modun key havuzu (1 = Atria, 2 = OpenRouter)."""
    return POOLS[get_active_mode()]


def pool_for(mode: str | None) -> KeyPool:
    m = str(mode or get_active_mode())
    return POOLS.get(m, POOLS[get_active_mode()])


# Geriye uyumluluk: eski tek POOL referanslari aktif havuza gider.
POOL = POOLS[get_active_mode()]


def _timeouts() -> httpx.Timeout:
    """Ayri timeout'lar: baglanti hizli kurulsun, okuma icin config siniri olsun."""
    return httpx.Timeout(
        connect=10.0,
        write=30.0,
        read=float(CONFIG.get("request_timeout", 120)),
        pool=10.0,
    )


def _handle_upstream_failure(key: str, resp: httpx.Response, ms: float, pool=None):
    """Upstream hata durumunu havuza isler; 429'da reset suresine bakilir
    ve gunluk limitlerde MAX_RATE_LIMIT_COOLDOWN_S (1 saat) kadar cooldown uygulanir."""
    p = pool or active_pool()
    p.record(key, ms, ok=False)
    if resp.status_code == 429:
        seconds = _rate_limit_reset_seconds(resp)
        if seconds is not None:
            p.mark_rate_limited(key, min(seconds, MAX_RATE_LIMIT_COOLDOWN_S))
            return
        # 429 ama reset header yoksa — en az 60 sn dinlendir (free tier daily limit gibi)
        p.mark_rate_limited(key, 60.0)
        return
    p.mark_failed(key)


def _rate_limit_reset_seconds(resp: httpx.Response) -> float | None:
    """X-RateLimit-Reset header'ini saniye cinsine cevirir.
    OpenRouter absolute epoch-ms veya kalan-ms gonderebilir; ikisini de destekler."""
    reset = resp.headers.get("x-ratelimit-reset")
    if not reset:
        return None
    try:
        v = float(reset)
    except ValueError:
        return None
    now_ms = time.time() * 1000
    if v > now_ms:  # absolute epoch (ms)
        return max(0.0, (v - now_ms) / 1000.0)
    return max(0.0, v / 1000.0)  # kalan sure (ms)


def _openai_content_to_text(content) -> str:
    """OpenAI content alanini duz metne cevirir (str veya multimodal dizi)."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict):
                t = p.get("text")
                if t:
                    parts.append(str(t))
            elif p:
                parts.append(str(p))
        return " ".join(parts)
    return str(content)


def _openai_images(content) -> list[dict]:
    """OpenAI content listesindeki image_url parcalarini ham halde toplar."""
    out: list[dict] = []
    if not isinstance(content, list):
        return out
    for p in content:
        if isinstance(p, dict) and p.get("type") == "image_url":
            out.append(p)
    return out


def _openai_images_to_anthropic(parts: list[dict]) -> list[dict]:
    """image_url parcalarini Anthropic image bloguna cevirir."""
    out: list[dict] = []
    for p in parts:
        conv = _openai_part_to_anthropic(p)
        if conv:
            out.append(conv)
    return out


def _openai_part_to_anthropic(p: dict) -> dict | None:
    """OpenAI content parcasini Anthropic bloguna cevirir (image_url dahil).

    None donerse parca Anthropic formatinda tasinamiyor (sessizce dusurulur)."""
    if not isinstance(p, dict):
        return None
    t = p.get("type")
    if t == "text":
        txt = p.get("text")
        return {"type": "text", "text": txt} if txt else None
    if t == "image_url":
        iu = p.get("image_url")
        url = iu.get("url") if isinstance(iu, dict) else (iu if isinstance(iu, str) else None)
        if not url:
            return None
        # data URI -> base64 kaynak
        if url.startswith("data:"):
            head, _, data = url.partition(",")
            if not data:
                return None
            media = head[5:].split(";")[0] or "image/png"
            return {"type": "image",
                    "source": {"type": "base64", "media_type": media, "data": data}}
        return {"type": "image", "source": {"type": "url", "url": url}}
    return None


def _openai_content_to_anthropic(content) -> Any:
    """OpenAI content alanini Anthropic content'ine cevirir; gorselleri KORUR."""
    if content is None or isinstance(content, str):
        return content or ""
    if isinstance(content, list):
        blocks: list[dict] = []
        for p in content:
            conv = _openai_part_to_anthropic(p)
            if conv:
                blocks.append(conv)
            elif isinstance(p, dict) and p.get("text"):
                blocks.append({"type": "text", "text": p["text"]})
        return blocks or ""
    return str(content)


def _openai_to_anthropic(payload_openai: dict, model: str) -> dict:
    """OpenAI chat payload'ini Atria (/v1/messages, Anthropic format) icin cevirir.

    Cok turluk tool dongusu desteklenir:
      - assistant tool_calls  -> content icindeki tool_use bloklari
      - role:"tool" mesaji    -> user icindeki tool_result blogu (tool_use_id ile)
    Bunlar once tamamen atiliyordu; model gecmisi kaybediyordu."""
    msgs = payload_openai.get("messages") or []
    system_parts: list[str] = []
    conv: list[dict] = []

    for m in msgs:
        role = (m.get("role") or "user").strip().lower()
        raw_content = m.get("content")
        text = _openai_content_to_text(raw_content)
        # gorsel tasiyicisi: content listesindeki image_url parcalari
        raw_images = _openai_images(raw_content)

        if role in ("system", "developer"):
            if text:
                system_parts.append(text)
            continue

        # ---- tool sonucu mesaji -> tool_result blogu ----
        if role == "tool":
            tid = m.get("tool_call_id") or ""
            rc: dict = {"type": "tool_result", "tool_use_id": tid, "content": text}
            if raw_images:
                conv.append({"role": "user",
                             "content": [rc] + _openai_images_to_anthropic(raw_images)})
            else:
                conv.append({"role": "user", "content": [rc]})
            continue

        if role == "assistant":
            blocks: list[dict] = []
            if text:
                blocks.append({"type": "text", "text": text})
            if raw_images:
                blocks.extend(_openai_images_to_anthropic(raw_images))
            for tc in m.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") or {}
                args = fn.get("arguments")
                if isinstance(args, str):
                    try:
                        args = json.loads(args) if args.strip() else {}
                    except (ValueError, TypeError):
                        args = {"_raw": args}
                elif not isinstance(args, dict):
                    args = {}
                blocks.append({
                    "type": "tool_use",
                    "id": tc.get("id") or f"toolu_{len(conv)}",
                    "name": fn.get("name", ""),
                    "input": args,
                })
            if blocks:
                conv.append({"role": "assistant", "content": blocks})
            elif text:
                conv.append({"role": "assistant", "content": text})
            continue

        # ---- user (ve digerleri) ----
        if raw_images:
            blocks_u: list[dict] = []
            if text:
                blocks_u.append({"type": "text", "text": text})
            blocks_u.extend(_openai_images_to_anthropic(raw_images))
            conv.append({"role": "user", "content": blocks_u})
        else:
            conv.append({"role": "user", "content": text})

    if not conv:
        conv = [{"role": "user", "content": ""}]
    out: dict = {
        "model": model,
        "max_tokens": payload_openai.get("max_tokens") or 1024,
        "messages": conv,
    }
    if system_parts:
        out["system"] = "\n".join(system_parts)
    if payload_openai.get("temperature") is not None:
        out["temperature"] = payload_openai["temperature"]
    if payload_openai.get("top_p") is not None:
        out["top_p"] = payload_openai["top_p"]
    if payload_openai.get("stop") is not None:
        out["stop_sequences"] = ([payload_openai["stop"]] if isinstance(
            payload_openai["stop"], str) else list(payload_openai["stop"]))
    tools = payload_openai.get("tools")
    if isinstance(tools, list):
        a_tools = []
        for t in tools:
            if not isinstance(t, dict):
                continue
            fn = t.get("function", t) if isinstance(t.get("function", t), dict) else {}
            name = fn.get("name", t.get("name", ""))
            if not name:
                continue
            a_tools.append({
                "name": name,
                "description": fn.get("description", t.get("description", "")),
                "input_schema": fn.get("parameters", t.get("input_schema", {})),
            })
        if a_tools:
            out["tools"] = a_tools
    tc = payload_openai.get("tool_choice")
    if isinstance(tc, str):
        if tc in ("required", "any"):
            out["tool_choice"] = {"type": "any"}
        elif tc in ("auto", "none"):
            out["tool_choice"] = {"type": tc}
        elif tc == "required":
            out["tool_choice"] = {"type": "any"}
    elif isinstance(tc, dict):
        fn = tc.get("function") or {}
        nm = fn.get("name") or tc.get("name")
        if nm:
            out["tool_choice"] = {"type": "tool", "name": nm}
        elif tc.get("type"):
            out["tool_choice"] = {"type": tc["type"]}
    return out


def _anthropic_response_to_openai(anth: dict, model: str) -> dict:
    """Atria (/v1/messages) cevabini OpenAI chat cevabina cevirir."""
    blocks = anth.get("content") or []
    texts: list[str] = []
    tool_calls: list[dict] = []
    for b in blocks:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text" and b.get("text"):
            texts.append(b["text"])
        elif b.get("type") == "thinking" and b.get("thinking"):
            # Atria thinking bloklari bazi kucuk max_tokens isteklerinde tek icerik olabiliyor
            texts.append(b["thinking"])
        elif b.get("type") == "tool_use":
            try:
                args = json.dumps(b.get("input", {}), ensure_ascii=False)
            except Exception:
                args = "{}"
            tool_calls.append({
                "id": b.get("id", ""),
                "type": "function",
                "function": {"name": b.get("name", ""), "arguments": args},
            })
    msg: dict = {"role": "assistant", "content": "\n".join(texts)}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    u = anth.get("usage") or {}
    pt = u.get("input_tokens", 0)
    ct = u.get("output_tokens", 0)
    finish = "stop"
    sr = anth.get("stop_reason")
    if sr == "tool_use":
        finish = "tool_calls"
    elif sr == "max_tokens":
        finish = "length"
    return {
        "id": anth.get("id", "chatcmpl-atria"),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
        "usage": {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct},
    }


def _atria_headers(key: str) -> dict:
    """Atria hem x-api-key hem Bearer kabul edebilsin diye ikisini de yollar."""
    return {
        "x-api-key": key,
        "Authorization": f"Bearer {key}",
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }


# --------------------------------------------------------------------------
# Kirpilmis (truncated) tur tespiti + max_tokens tirmanmasi
#
# Bazi upstream modeller (stealth/space-bunny-alpha, Atria) tum token
# bütcesini reasoning'e harcir ve max_tokens dolduğunda tool_call URETMEDEN
# kesilir: content=None, finish_reason="length". Istemci (Claude Code, agent)
# bos bir tur gorup "is bitti" sanir ve durur. Gateway bunu yakalayip
# max_tokens'i katlayarak ayni istegi tekrar gonderir.
# --------------------------------------------------------------------------
def _openai_turn_is_empty(result: dict) -> bool:
    """OpenAI formatinda cevap kirpildiysa True (finish_reason == "length").

    Agent istemcilerde max_tokens'a takilmis bir tur, icerik uretmis olsa bile
    "yarim kaldi" sayilir: Claude Code / opencode o turda bekleyi kesip durur.
    Bu yuzden bos mu dolu mu farki yok, tek sart finish_reason == 'length'."""
    try:
        ch = result["choices"][0]
    except (KeyError, IndexError, TypeError):
        return False
    return ch.get("finish_reason") == "length"


def _anthropic_turn_is_empty(anth: dict) -> bool:
    """Anthropic formatinda cevap kirpildiysa True (stop_reason == "max_tokens")."""
    return anth.get("stop_reason") == "max_tokens"


def _next_token_budget(current: int | None) -> int:
    """Kirpilmada max_tokens'i kademeli artirir (katli, tavanda)."""
    floor = int(CONFIG.get("min_token_budget", 1024))
    cap = int(CONFIG.get("max_token_budget", 32768))
    step = max(512, int(CONFIG.get("token_budget_step", 2048)))
    base = int(current or 0)
    nxt = max(base * 2, base + step, floor)
    return min(nxt, cap)


def _token_budget_tries() -> int:
    return max(1, int(CONFIG.get("token_budget_tries", 3)))


def _reasoning_budget(payload: dict) -> int:
    cur = payload.get("reasoning")
    if isinstance(cur, dict):
        try:
            return int(cur.get("max_tokens") or 0)
        except (TypeError, ValueError):
            return 0
    return 0


def _openai_turn_is_degenerate(result: dict, had_tools: bool) -> bool:
    """Arac sunulmusken uretilen neredeyse bos tur.

    UYARI: bu tespit varsayilan olarak KAPALI. Deneyim gosterdi ki model
    kisa/normal metin turlari uretir (orn. 3448 karakter), gateway bunu
    "bozuk" sayip 3 kez tekrar deniyor, reasoning butcesini 3072 -> 6144 ->
    12288'e cikarip turu 4 kat uuzatiyor, kota yiyor ve tool cagrisi yine
    uretilmiyor. Bu bir regresyondu; varsayilan kapali.
    `degenerate_retry: true` ile geri acilabilir (ozellikle kucuk modelerde)."""
    if not CONFIG.get("degenerate_retry", False):
        return False
    if not had_tools:
        return False
    try:
        ch = result["choices"][0]
    except (KeyError, IndexError, TypeError):
        return False
    if ch.get("finish_reason") not in (None, "stop"):
        return False
    if ch.get("tool_calls"):
        return False
    try:
        out = int((result.get("usage") or {}).get("completion_tokens") or 0)
    except (TypeError, ValueError):
        out = 0
    text = ((ch.get("message") or {}).get("content") or "")
    if out > int(CONFIG.get("degenerate_token_floor", 64)):
        return False
    return len(text.strip()) < int(CONFIG.get("degenerate_char_ceiling", 400))


def _apply_budget_floor(payload: dict) -> dict:
    """Streaming icin token butce tabani: reasoning + tool_call bir arada sigsin.

    Istemci cok kucuk max_tokens isteyince model butceyi reasoning'e harcar,
    tool_call uretmeden kesilir ve agent bos tur gorup durur."""
    floor = int(CONFIG.get("min_token_budget", 1024))
    cur = int(payload.get("max_tokens") or 0)
    if 0 < cur < floor:
        payload = {**payload, "max_tokens": floor}
    return payload


# --------------------------------------------------------------------------
# Tur teshisi: istemciye (Claude Code / opencode) ne dondurdugumuzu kanitlar.
# "Agent neden duruyor" sorusunun cevabi buradan okunur: GET /api/diag
# --------------------------------------------------------------------------
DIAG: dict = {"last": None, "turns": 0, "empty_turns": 0, "truncated_turns": 0}


def _diag(turn: dict):
    """Bir turu kaydet; tur bitisinde /api/diag uzerinden okunur."""
    DIAG["turns"] = DIAG["turns"] + 1
    DIAG["last"] = turn
    if not turn.get("has_text") and not turn.get("has_tool_use"):
        DIAG["empty_turns"] = DIAG["empty_turns"] + 1
    if turn.get("stop_reason") in ("max_tokens", "length"):
        DIAG["truncated_turns"] = DIAG["truncated_turns"] + 1
    print("[ox-gateway] turn: " + json.dumps(turn, ensure_ascii=False))



async def _await_truncation_retry(delay: float = 0.4):
    """Kisa bekleyip tur bitim noktasindan devam edilecek sinyal uretir."""
    await asyncio.sleep(delay)



async def _candidate_models(model: str | None) -> list[str]:
    """Birincil model + 429 durumunda denenecek ucretsiz yedek modeller.
    Yedekler once config'deki 'fallback_models'dan, sonra OpenRouter'un
    ucretsiz listesinden dinamik cekilir. Atria modunda (1) yedek uretilmez."""
    primary = model or get_active_model()
    if get_active_mode() == "1":
        return [primary]
    cands = [primary]
    if not CONFIG.get("auto_model_fallback", True):
        return cands
    limit = max(1, int(CONFIG.get("max_model_fallbacks", 4)))
    extra: list[str] = [m for m in (CONFIG.get("fallback_models") or [])]
    try:
        raw = await fetch_openrouter_models()
        extra += [m.get("id", "") for m in raw if _is_free(m)]
    except Exception:
        pass
    seen = {primary}
    for m in extra:
        m = (m or "").strip()
        if m and m != primary and m not in seen:
            cands.append(m)
            seen.add(m)
        if len(cands) >= limit:
            break
    return cands


GRACEFUL_TEXT = ("[ox-gateway] Su anda tum modeller gunluk limite takilmis durumda; "
                 "istek gateway icinde birden fazla kez otomatik olarak yeniden denendi. "
                 "Lutfen kisa bir sure sonra tekrar dene.")


def _payload_variants(payload: dict) -> list[dict]:
    """Oz-duzeltme varyantlari: model bir parametreyi desteklemiyorsa
    (400/404/422) isteği sadelestirerek tekrar denemek icin.
    Sira: orijinal -> reasoning'siz -> ayrica tools'suz."""
    out = [payload]
    slim = {k: v for k, v in payload.items() if k != "reasoning"}
    if slim != payload:
        out.append(slim)
    bare = {k: v for k, v in slim.items() if k not in ("tools", "tool_choice")}
    if bare != slim:
        out.append(bare)
    return out


def _graceful_result(model: str) -> dict:
    """Tum modeller basarisiz olursa agent'e hata yerine gecen gecerli cevap."""
    msg = {"role": "assistant", "content": GRACEFUL_TEXT}
    return {
        "id": "gen-graceful",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": msg, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _anthropic_graceful_events() -> list[bytes]:
    """Upstream hic acilmazsa: hataya yerin gecerli minimal Anthropic SSE akisi."""
    return [
        _sse_event("message_start", {"type": "message_start", "message": {
            "id": "msg_graceful", "type": "message", "role": "assistant",
            "model": "ox-gateway", "content": [],
            "usage": {"input_tokens": 0, "output_tokens": 0}}}),
        _sse_event("content_block_start", {"type": "content_block_start", "index": 0,
                                           "content_block": {"type": "text", "text": ""}}),
        _sse_event("content_block_delta", {"type": "content_block_delta", "index": 0,
                                           "delta": {"type": "text_delta", "text": GRACEFUL_TEXT}}),
        _sse_event("content_block_stop", {"type": "content_block_stop", "index": 0}),
        _sse_event("message_delta", {"type": "message_delta",
                                     "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                                     "usage": {"output_tokens": len(GRACEFUL_TEXT) // 4}}),
        _sse_event("message_stop", {"type": "message_stop"}),
    ]


def _openai_graceful_chunks() -> list[bytes]:
    """Upstream hic acilmazsa: hataya yerin gecerli minimal OpenAI SSE akisi."""
    chunk = {
        "id": "chatcmpl-graceful", "object": "chat.completion.chunk",
        "created": int(time.time()), "model": "ox-gateway",
        "choices": [{"index": 0, "delta": {"content": GRACEFUL_TEXT}, "finish_reason": "stop"}],
    }
    return [("data: " + json.dumps(chunk) + "\n\n").encode()]


async def call_atria_anthropic(anth_payload: dict, model: str | None = None) -> dict:
    """Anthropic-format istegi 1. mod havuzuyla Atria'ya yollar (failover'li).

    Cevap kirpilirsa (sadece thinking + stop_reason=max_tokens) max_tokens
    katlanarak tekrar denenir; boylece tool_call uretilir."""
    primary = model or get_active_model()
    pool = POOLS["1"]
    body = dict(anth_payload)
    body["model"] = primary
    if not body.get("max_tokens"):
        body["max_tokens"] = 1024
    max_retries = CONFIG.get("max_retries", 3)
    rounds = max(1, int(CONFIG.get("heal_retries", 2)))
    last_err = None
    budget_tries = _token_budget_tries()
    for rnd in range(rounds):
        tried: set[str] = set()
        for _ in range(max_retries):
            key = await pool.acquire(skip=tried)
            if key is None:
                break
            tried.add(key)
            await THROTTLE.wait()
            t0 = time.perf_counter()
            try:
                async with httpx.AsyncClient(timeout=_timeouts()) as client:
                    resp = await client.post(ATRIA_URL, json=body, headers=_atria_headers(key))
                ms = (time.perf_counter() - t0) * 1000
                if resp.status_code == 200:
                    data = resp.json()
                    pool.mark_ok(key)
                    pool.record(key, ms, ok=True)
                    if _anthropic_turn_is_empty(data) and budget_tries > 0:
                        budget_tries -= 1
                        old = int(body.get("max_tokens") or 0)
                        body["max_tokens"] = _next_token_budget(old)
                        print(f"[ox-gateway] atria cevabi kirpildi (max_tokens={old}, "
                              f"tool_call yok) -> {body['max_tokens']} ile tekrar deneniyor")
                        continue
                    return data
                last_err = f"atria:{primary} -> HTTP {resp.status_code}: {resp.text[:300]}"
                _handle_upstream_failure(key, resp, ms, pool)
                if 400 <= resp.status_code < 500 and resp.status_code != 429:
                    break
            except httpx.HTTPError as e:
                ms = (time.perf_counter() - t0) * 1000
                last_err = f"atria:{primary} -> Network error: {e}"
                pool.mark_failed(key)
                pool.record(key, ms, ok=False)
        if rnd < rounds - 1:
            await asyncio.sleep(MAX_COOLDOWN_S)
    if CONFIG.get("graceful_degradation", True):
        print("[ox-gateway] atria yanit vermedi; graceful cevap donduruluyor")
        return {
            "id": "msg_graceful", "type": "message", "role": "assistant",
            "model": primary, "content": [{"type": "text", "text": GRACEFUL_TEXT}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }
    raise HTTPException(status_code=502, detail=f"Atria basarisiz. Son hata: {last_err}")


async def open_atria_stream(anth_payload: dict, model: str | None = None):
    """Atria'ya streaming baglantisi acar. Donus: (client, resp, line_iter, ilk_satir)."""
    primary = model or get_active_model()
    pool = POOLS["1"]
    body = dict(anth_payload)
    body["model"] = primary
    if not body.get("max_tokens"):
        body["max_tokens"] = 1024
    body = _apply_budget_floor(body)
    body["stream"] = True
    max_retries = CONFIG.get("max_retries", 3)
    first_token_s = max(1.0, float(CONFIG.get("first_token_ms", 20000)) / 1000.0)
    last_err = None
    tried: set[str] = set()
    for _ in range(max_retries):
        key = await pool.acquire(skip=tried)
        if key is None:
            break
        tried.add(key)
        await THROTTLE.wait()
        t0 = time.perf_counter()
        client = httpx.AsyncClient(timeout=_timeouts())
        try:
            req = client.build_request("POST", ATRIA_URL, json=body, headers=_atria_headers(key))
            resp = await client.send(req, stream=True)
            if resp.status_code != 200:
                ms = (time.perf_counter() - t0) * 1000
                last_err = f"atria:{primary} -> HTTP {resp.status_code}: {(await resp.aread()).decode()[:300]}"
                await resp.aclose()
                await client.aclose()
                _handle_upstream_failure(key, resp, ms, pool)
                if 400 <= resp.status_code < 500 and resp.status_code != 429:
                    break
                continue
            line_iter = resp.aiter_lines()
            try:
                first_line = await asyncio.wait_for(line_iter.__anext__(), timeout=first_token_s)
            except asyncio.TimeoutError:
                ms = (time.perf_counter() - t0) * 1000
                last_err = f"atria:{primary} -> ilk token {first_token_s:.0f}s icinde gelmedi"
                await resp.aclose()
                await client.aclose()
                pool.mark_failed(key)
                pool.record(key, ms, ok=False)
                continue
            ms = (time.perf_counter() - t0) * 1000
            pool.mark_ok(key)
            pool.record(key, ms, ok=True)
            return client, resp, line_iter, first_line
        except httpx.HTTPError as e:
            try:
                await client.aclose()
            except Exception:
                pass
            last_err = f"atria:{primary} -> Network error: {e}"
            pool.mark_failed(key)
    raise HTTPException(status_code=502, detail=f"Atria stream basarisiz. Son hata: {last_err}")


async def call_openrouter(payload: dict, model: str | None = None) -> dict:
    """Istegi havuzdan key alarak OpenRouter'a yollar.
    Hata verirse agente hata gondermeden KENDI ICINDE duzeltir:
    1) Key failover, 2) model fallback (429), 3) parametre sadelestirme (4xx),
    4) kisa bekleme sonrasi tum zinciri tekrar (heal_retries).
    Hicbir sey tutmazsa hata yerine gecerli bir cevap dondurur.
    Aktif mod 1 ise Atria'ya gider (Anthropic format), 2 ise OpenRouter'a.
    Model adi Atria iceriyorsa aktif mod ne olursa olsun Atria'ya gider (union vs dashboard tutarli)."""
    effective = model or get_active_model()
    if _should_route_atria(effective):
        primary_a = effective
        anth = _openai_to_anthropic({**payload, "model": primary_a}, primary_a)
        anth.pop("reasoning", None)
        result_a = await call_atria_anthropic(anth, primary_a)
        return _anthropic_response_to_openai(result_a, primary_a)
    primary = effective
    pool = POOLS["2"]
    base_payload = {k: v for k, v in payload.items() if k != "model"}
    max_retries = CONFIG.get("max_retries", 3)
    rounds = max(1, int(CONFIG.get("heal_retries", 2)))
    variants = _payload_variants(base_payload)
    budget_tries = _token_budget_tries()
    last_err = None

    for rnd in range(rounds):
        for vbase in variants:
            for m in await _candidate_models(primary):
                # Kirpilan turda ayni modeli, katlanmis token butcesiyle tekrar dene
                cur_base = vbase
                degenerate = False
                for _attempt in range(budget_tries + 1):
                    payload_m = {**cur_base, "model": m}
                    tried: set[str] = set()
                    bad_payload = False
                    truncated = False
                    degenerate = False
                    for _ in range(max_retries):
                        key = await pool.acquire(skip=tried)
                        if key is None:
                            break
                        tried.add(key)
                        await THROTTLE.wait()
                        headers = {"Authorization": f"Bearer {key}",
                                   "Content-Type": "application/json"}
                        t0 = time.perf_counter()
                        try:
                            async with httpx.AsyncClient(timeout=_timeouts()) as client:
                                resp = await client.post(OPENROUTER_URL, json=payload_m,
                                                         headers=headers)
                            ms = (time.perf_counter() - t0) * 1000
                            if resp.status_code == 200:
                                pool.mark_ok(key)
                                pool.record(key, ms, ok=True)
                                data = resp.json()
                                if _openai_turn_is_empty(data):
                                    truncated = True
                                    break
                                # Bozuk/neredeyse-bos tur: arac sunulmus ama model
                                # 2 kelimeyle turu kapatti -> yeniden dene
                                if (not truncated
                                        and _attempt < budget_tries
                                        and _openai_turn_is_degenerate(
                                            data, bool(payload_m.get("tools")))):
                                    degenerate = True
                                    DIAG["degenerate_turns"] = DIAG.get("degenerate_turns", 0) + 1
                                    break
                                if m != primary or cur_base is not variants[0]:
                                    print(f"[ox-gateway] istek kendi icinde duzeltildi -> "
                                          f"model='{m}', varyant={variants.index(vbase) + 1}"
                                          f"/{len(variants)}")
                                return data
                            last_err = f"{m} -> HTTP {resp.status_code}: {resp.text[:300]}"
                            _handle_upstream_failure(key, resp, ms, pool)
                            if resp.status_code == 429:
                                # Bu modelin gunluk limiti dolu -> sonraki modele gec
                                print(f"[ox-gateway] '{m}' gunluk limite takildi, "
                                      f"yedek modele geciliyor")
                                break
                            if 400 <= resp.status_code < 500:
                                # Istek icerigi bu model icin gecersiz -> sadelestirip dene
                                print(f"[ox-gateway] '{m}' istegi reddetti ({resp.status_code}), "
                                      f"parametreler sadelestiriliyor")
                                bad_payload = True
                                break
                            # 5xx -> ayni modelde diger key
                        except httpx.HTTPError as e:
                            ms = (time.perf_counter() - t0) * 1000
                            last_err = f"{m} -> Network error: {e}"
                            pool.mark_failed(key)
                            pool.record(key, ms, ok=False)
                    if bad_payload:
                        break  # sonraki payload varyanti
                    if degenerate:
                        # arac sunulmus ama model neredeyse bos tur dondurdu
                        # -> sadece acikca istenirse ve OLCULU bir tavanla
                        # (varsayilan kapali; bkz. _openai_turn_is_degenerate)
                        if _attempt < budget_tries:
                            base_r = _reasoning_budget(cur_base)
                            nb = _next_token_budget(base_r)
                            cap = int(CONFIG.get("max_reasoning_budget", 2048))
                            nb = min(nb, max(base_r, cap))
                            cur_base = {**cur_base, "reasoning": {"max_tokens": nb}}
                            DIAG["last_retry"] = {"model": m, "reasoning": nb}
                            print(f"[ox-gateway] '{m}' bozuk tur -> reasoning {nb} ile "
                                  f"tekrar (deneme {_attempt + 2}/{budget_tries + 1})")
                            continue
                        break  # deneme hakki bitti, normal cevabi kabul et
                    if truncated:
                        # token butcesi reasoning'e gitti, tool_call uretilmedi
                        # -> ayni modeli daha genis butceyle tekrar dene
                        if _attempt < budget_tries:
                            old = int(payload_m.get("max_tokens") or 0)
                            cur_base = {**cur_base, "max_tokens": _next_token_budget(old)}
                            print(f"[ox-gateway] '{m}' cevabi kirpildi (max_tokens={old}, "
                                  f"tool_call yok) -> {cur_base['max_tokens']} ile tekrar "
                                  f"deneniyor (deneme {_attempt + 2}/{budget_tries + 1})")
                            continue
                        print(f"[ox-gateway] '{m}' kirpilan cevap verildi; graceful metne dusuluyor")
                        return _graceful_result(m)
                    break  # bu modelde normal cevap alindi ya da basarisiz
        if rnd < rounds - 1:
            print(f"[ox-gateway] zincir tukendi, {MAX_COOLDOWN_S:.0f}s sonra tekrar denenecek "
                  f"(tur {rnd + 2}/{rounds})")
            await asyncio.sleep(MAX_COOLDOWN_S)

    if CONFIG.get("graceful_degradation", True):
        print("[ox-gateway] hicbir model yanit vermedi; agente hata yerine gecerli cevap donduruluyor")
        return _graceful_result(primary)
    raise HTTPException(status_code=502, detail=f"Tum keyler/modeller basarisiz. Son hata: {last_err}")


async def open_stream(payload: dict, model: str | None = None, pool=None):
    """Streaming icin upstream baglantisi acar.
    Hata verirse agente hata akitmamak icin kendi icinde duzeltir:
    key failover -> model fallback (429) -> parametre sadelestirme (4xx)
    -> kisa bekleyip tekrar (heal_retries).
    Donus: (client, resp, line_iter, ilk_satir) — cagiran kapatir."""
    primary = model or get_active_model()
    pool = pool or POOLS["2"]
    base_payload = {k: v for k, v in payload.items() if k != "model"}
    base_payload = _apply_budget_floor(base_payload)
    max_retries = CONFIG.get("max_retries", 3)
    first_token_s = max(1.0, float(CONFIG.get("first_token_ms", 20000)) / 1000.0)
    rounds = max(1, int(CONFIG.get("heal_retries", 2)))
    variants = _payload_variants(base_payload)
    last_err = None

    for rnd in range(rounds):
        for vbase in variants:
            for m in await _candidate_models(primary):
                payload_m = {**vbase, "model": m}
                tried: set[str] = set()
                bad_payload = False
                for _ in range(max_retries):
                    key = await pool.acquire(skip=tried)
                    if key is None:
                        break
                    tried.add(key)
                    await THROTTLE.wait()
                    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
                    t0 = time.perf_counter()
                    client = httpx.AsyncClient(timeout=_timeouts())
                    try:
                        req = client.build_request("POST", OPENROUTER_URL, json=payload_m, headers=headers)
                        resp = await client.send(req, stream=True)
                        if resp.status_code != 200:
                            ms = (time.perf_counter() - t0) * 1000
                            last_err = f"{m} -> HTTP {resp.status_code}: {(await resp.aread()).decode()[:300]}"
                            await resp.aclose()
                            await client.aclose()
                            _handle_upstream_failure(key, resp, ms, pool)
                            if resp.status_code == 429:
                                print(f"[ox-gateway] stream: '{m}' gunluk limite takildi, yedek modele geciliyor")
                                break
                            if 400 <= resp.status_code < 500:
                                print(f"[ox-gateway] stream: '{m}' istegi reddetti ({resp.status_code}), "
                                      f"parametreler sadelestiriliyor")
                                bad_payload = True
                                break
                            continue

                        # Ilk token bekleniyor; gec kalirsa bu key yavas -> failover
                        line_iter = resp.aiter_lines()
                        try:
                            first_line = await asyncio.wait_for(line_iter.__anext__(), timeout=first_token_s)
                        except asyncio.TimeoutError:
                            ms = (time.perf_counter() - t0) * 1000
                            last_err = f"{m} -> ilk token {first_token_s:.0f}s icinde gelmedi"
                            await resp.aclose()
                            await client.aclose()
                            pool.mark_failed(key)
                            pool.record(key, ms, ok=False)
                            continue

                        ms = (time.perf_counter() - t0) * 1000
                        pool.mark_ok(key)
                        pool.record(key, ms, ok=True)  # time-to-first-byte
                        if m != primary or vbase is not variants[0]:
                            print(f"[ox-gateway] stream: istek kendi icinde duzeltildi -> "
                                  f"model='{m}', varyant={variants.index(vbase) + 1}/{len(variants)}")
                        return client, resp, line_iter, first_line
                    except httpx.HTTPError as e:
                        try:
                            await client.aclose()
                        except Exception:
                            pass
                        last_err = f"{m} -> Network error: {e}"
                        pool.mark_failed(key)
                if bad_payload:
                    break  # sonraki payload varyanti
        if rnd < rounds - 1:
            print(f"[ox-gateway] stream: zincir tukendi, {MAX_COOLDOWN_S:.0f}s sonra tekrar denenecek "
                  f"(tur {rnd + 2}/{rounds})")
            await asyncio.sleep(MAX_COOLDOWN_S)

    raise HTTPException(status_code=502, detail=f"Tum keyler/modeller basarisiz. Son hata: {last_err}")


app = FastAPI(title="ox-gateway", version="1.2")


class Message(BaseModel):
    role: str
    # str veya multimodal dizi (OpenAI content parts) olabilir
    content: Any = ""
    # role:"tool" sonucu icin:
    tool_call_id: str | None = None
    name: str | None = None
    # multimodal parcalar icin gerekebilir
    tool_calls: list[dict] | None = None


class ChatRequest(BaseModel):
    messages: list[Message]
    model: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    stream: bool = False
    # --- asagidakiler once tamamen yoktu; tool kullanan istemciler 422 aliyordu ---
    tools: list[dict] | None = None
    tool_choice: Any = None
    parallel_tool_calls: bool | None = None
    stop: Any = None
    top_p: float | None = None
    seed: int | None = None
    response_format: dict | None = None
    reasoning_effort: str | None = None


def _openai_messages_to_payload(msgs: list[Message]) -> list[dict]:
    """Message modellerini OpenAI formatina cevirir; content dizi olarak kalir."""
    out: list[dict] = []
    for m in msgs:
        d = m.model_dump(exclude_none=True)
        out.append(d)
    return out


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
    return {"service": "ox-gateway", "model": get_active_model(), "keys": len(active_pool().keys)}


@app.get("/api/stats")
async def api_stats(mode: str | None = None):
    """Dashboard'un canlı dinlediği özet (aktif mod + tum modlarin ozeti)."""
    m = str(mode or get_active_mode())
    if m not in ("1", "2"):
        m = get_active_mode()
    ks = pool_for(m).status()
    modes = {}
    for mid in ("1", "2"):
        kss = POOLS[mid].status()
        modes[mid] = {
            "provider": PROVIDERS[mid]["name"],
            "url": PROVIDERS[mid]["url"],
            "model": (CONFIG.get("provider_models") or {}).get(mid),
            "total_keys": len(kss),
            "active_keys": sum(1 for k in kss if k["state"] == "active"),
            "total_requests": sum(k["requests"] for k in kss),
            "total_fail": sum(k["fail"] for k in kss),
        }
    return {
        "model": get_active_model(),
        "active_mode": get_active_mode(),
        "mode": m,
        "provider": PROVIDERS[m]["name"],
        "provider_url": PROVIDERS[m]["url"],
        "uptime_s": round(time.time() - START_TIME),
        "total_keys": len(ks),
        "active_keys": sum(1 for k in ks if k["state"] == "active"),
        "total_requests": sum(k["requests"] for k in ks),
        "total_fail": sum(k["fail"] for k in ks),
        "keys": ks,
        "modes": modes,
    }


# --------------------------------------------------------------------------
# Proxy auth: sadece /v1/* proxy endpoint'leri gateway key ister.
# Dashboard ve yonetim endpoint'leri localhost'ta aciktir.
# --------------------------------------------------------------------------
def check_auth(request: Request):
    """Gateway anahtari kontrolu.

    ONCEKI SURUM `?api_key=` query parametresini de kabul ediyordu; anahtar
    boylece access loglarina, tarayici gecmisine ve proxy loglarina duserdi.
    Artik SADECE header kabul edilir. Bir anahtar URL'de gorulurse istek
    reddedilir ve uyari verilir."""
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if not token:
        token = request.headers.get("x-api-key", "")
    if not token and request.query_params.get("api_key"):
        print("[ox-gateway] UYARI: anahtar query string ile gonderilmeye calisildi "
              "(reddedildi) — URL'ler loglanir, header kullan")
        raise HTTPException(401, "Anahtar query string ile gonderilemez; header kullanin")
    if not token or token != GATEWAY_KEY:
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
    am = get_active_mode()
    return {
        "protocol": proto,
        "base_url": "http://127.0.0.1:8756/v1",
        "anthropic_base_url": "http://127.0.0.1:8756",
        "gateway_api_key": GATEWAY_KEY,
        "model": get_active_model(),
        "active_mode": am,
        "provider": PROVIDERS[am]["name"],
        "provider_url": PROVIDERS[am]["url"],
        "providers": PROVIDERS,
        "provider_models": CONFIG.get("provider_models") or {},
    }


class ModeSetRequest(BaseModel):
    mode: str


@app.get("/api/diag")
async def diag():
    """Son tur teşhisi: istemciye ne döndük? (Claude Code neden duruyor sorusunun kaniti)"""
    return {
        "active_mode": get_active_mode(),
        "provider": get_provider()["name"],
        "model": get_active_model(),
        "turns": DIAG["turns"],
        "empty_turns": DIAG["empty_turns"],
        "truncated_turns": DIAG["truncated_turns"],
        "last": DIAG["last"],
    }


@app.post("/api/diag/reset")
async def diag_reset():
    DIAG["turns"] = 0
    DIAG["empty_turns"] = 0
    DIAG["truncated_turns"] = 0
    DIAG["last"] = None
    return {"reset": True}


@app.get("/api/providers")
async def providers_info():
    """Iki provider/modun durumu: 1 = Atria, 2 = OpenRouter."""
    out = {}
    for mid in ("1", "2"):
        kss = POOLS[mid].status()
        out[mid] = {
            "provider": PROVIDERS[mid]["name"],
            "url": PROVIDERS[mid]["url"],
            "kind": PROVIDERS[mid]["kind"],
            "model": (CONFIG.get("provider_models") or {}).get(mid),
            "active": mid == get_active_mode(),
            "total_keys": len(kss),
        }
    return {"active_mode": get_active_mode(), "modes": out}


@app.post("/mode/set")
@app.post("/provider/set")
async def set_mode(req: ModeSetRequest):
    """Aktif provider/modu degistirir; kalici kaydedilir. Eski keyler korunur."""
    m = str(req.mode).strip()
    if m not in ("1", "2"):
        raise HTTPException(400, "mod 1 veya 2 olmali (1=Atria, 2=OpenRouter)")
    cfg = load_config()
    cfg["active_mode"] = m
    # aktif model alanini da senkron tut (eski istemciler icin)
    pm = cfg.get("provider_models") or {}
    if pm.get(m):
        cfg["model"] = pm[m]
    save_config(cfg)
    CONFIG["active_mode"] = m
    if pm.get(m):
        CONFIG["model"] = pm[m]
    global POOL
    POOL = POOLS[m]
    return {"set": True, "active_mode": m, "provider": PROVIDERS[m]["name"],
            "provider_url": PROVIDERS[m]["url"], "model": get_active_model()}


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
async def keys_status(mode: str | None = None):
    m = str(mode or get_active_mode())
    if m not in ("1", "2"):
        m = get_active_mode()
    p = pool_for(m)
    return {"model": get_active_model(), "active_mode": get_active_mode(), "mode": m,
            "provider": PROVIDERS[m]["name"], "total": len(p.keys), "keys": p.status()}


# --------------------------------------------------------------------------
# Anthropic Messages API uyumlulugu (/v1/messages)
# --------------------------------------------------------------------------
def _block_text(b: dict) -> str:
    """tool_result icerigindeki METNI ceker (liste veya string).

    DIKKAT: burada sadece text alinir; tool_result icindeki 'image' bloklari
    BASKA bir fonksiyonla (_block_images) toplanir. MCP screenshot gibi
    gorsel donduren tool'lar bu yoldan gecerdi ve gorsel sessizce dusepdi."""
    rc = b.get("content", "")
    if isinstance(rc, list):
        parts = []
        for x in rc:
            if isinstance(x, dict):
                if x.get("type") == "text":
                    parts.append(x.get("text", "") or "")
            elif x:
                parts.append(str(x))
        return "\n".join(p for p in parts if p)
    return str(rc or "")


def _block_images(b: dict) -> list[dict]:
    """tool_result icerigindeki GORSEL bloklarini OpenAI image_url'e cevirir.

    Claude Code MCP ekran goruntulerini tool_result icinde image blogu olarak
    gonderir. Onceki surumde bunlar _block_text ile atiliyordu; model
    'screenshot bos donuyor' diyordu."""
    rc = b.get("content", "")
    if not isinstance(rc, list):
        return []
    out: list[dict] = []
    for x in rc:
        if not isinstance(x, dict) or x.get("type") != "image":
            continue
        src = x.get("source") or {}
        st = src.get("type")
        if st == "base64" and src.get("data"):
            mt = src.get("media_type") or "image/png"
            out.append({"type": "image_url",
                        "image_url": {"url": f"data:{mt};base64,{src['data']}"}})
        elif st == "url" and src.get("url"):
            out.append({"type": "image_url", "image_url": {"url": src["url"]}})
    return out


def _anthropic_to_openai(body: dict) -> dict:
    """Anthropic istek formatini OpenAI formatina cevirir.

    tool_use / tool_result DONUSUMU ONEMLI:
      - assistant icindeki tool_use bloklari OpenAI tool_calls mesajina cevrilir
      - user icindeki tool_result bloklari role:"tool" + tool_call_id mesajina cevrilir
    Onceki surumde bunlar atiliyordu / sahte metne donusturuluyordu; boylece
    model ilk tool cagrisindan sonra konusma gecmisini kaybediyor, tek turluk
    calisiyor ve cok turlu agent akisinda anlatima kaciyordu."""
    msgs: list[dict] = []
    sys = body.get("system")
    if sys:
        if isinstance(sys, list):
            sys = " ".join(b.get("text", "") for b in sys if isinstance(b, dict))
        msgs.append({"role": "system", "content": sys})

    for m in body.get("messages", []):
        role = m.get("role", "user")
        c = m.get("content")

        if not isinstance(c, list):
            msgs.append({"role": role, "content": c if c is not None else ""})
            continue

        texts: list[str] = []
        thinking: list[str] = []
        tool_calls: list[dict] = []
        tool_results: list[tuple[str, str]] = []
        images: list[dict] = []

        for b in c:
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if t == "text":
                if b.get("text"):
                    texts.append(b["text"])
            elif t == "thinking":
                if b.get("thinking"):
                    thinking.append(b["thinking"])
            elif t == "image":
                # GORSEL: Anthropic image blogu -> OpenAI image_url
                # (base64 -> data URI, url -> dogrudan). Onceki surumde
                # SESSIZCE ATILIYORDU; model 'resim yok' diyordu.
                src = b.get("source") or {}
                st = src.get("type")
                if st == "base64" and src.get("data"):
                    mt = src.get("media_type") or "image/png"
                    images.append({"type": "image_url",
                                   "image_url": {"url": f"data:{mt};base64,{src['data']}"}})
                elif st == "url" and src.get("url"):
                    images.append({"type": "image_url",
                                   "image_url": {"url": src["url"]}})
            elif t == "tool_use":
                try:
                    args = json.dumps(b.get("input", {}), ensure_ascii=False)
                except (TypeError, ValueError):
                    args = "{}"
                tool_calls.append({
                    "id": b.get("id", ""),
                    "type": "function",
                    "function": {"name": b.get("name", ""), "arguments": args},
                })
            elif t == "tool_result":
                tool_results.append((b.get("tool_use_id", ""), _block_text(b),
                                     _block_images(b)))

        # tool_result'lar ONCE gonderilir: OpenAI sirasi tool sonucu -> asistan
        if tool_results:
            for tid, txt, imgs in tool_results:
                if imgs:
                    # MCP screenshot gibi gorsel donduren tool sonuclari:
                    # metin + image_url parcalarini birlikte ilet
                    parts_t: list[dict] = []
                    if txt:
                        parts_t.append({"type": "text", "text": txt})
                    parts_t.extend(imgs)
                    msgs.append({"role": "tool", "tool_call_id": tid, "content": parts_t})
                else:
                    msgs.append({"role": "tool", "tool_call_id": tid, "content": txt})
        if images and not texts and not tool_calls:
            # sadece gorsel geldi (metin tool_result icinde olabilir)
            if not any(mm.get("content") for mm in msgs[-len(tool_results or [1]):]):
                msgs.append({"role": role, "content": images})
        if texts or tool_calls:
            # tool_calls her zaman assistant'a ait; OpenAI role:"user" + tool_calls kabul etmez
            if images and not tool_calls:
                # gorsel + metin birlikte: content dizisi olarak gonder
                parts_msg: list[dict] = []
                for tx in texts:
                    parts_msg.append({"type": "text", "text": tx})
                parts_msg.extend(images)
                msgs.append({"role": role or "user", "content": parts_msg})
            else:
                assistant: dict = {"role": "assistant" if tool_calls else (role or "user"),
                                   "content": "\n".join(texts) or None}
                if tool_calls:
                    assistant["tool_calls"] = tool_calls
                msgs.append(assistant)
        elif tool_results:
            pass  # sadece tool_result vardi, yukarida eklendi
        elif thinking:
            # thinking-only tur: reasoning olarak koru, icerik uretmeden bos mesaj atma
            msgs.append({"role": role, "content": ""})
        elif images:
            msgs.append({"role": role, "content": images})

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
    tc = body.get("tool_choice")
    if isinstance(tc, dict):
        kind = tc.get("type")
        if kind == "any":
            out["tool_choice"] = "required"
        elif kind == "tool" and tc.get("name"):
            out["tool_choice"] = {"type": "function",
                                  "function": {"name": tc["name"]}}
        elif kind in ("auto", "none"):
            out["tool_choice"] = kind
    return out


def _reasoning_config(body: dict) -> dict | None:
    """Anthropic 'thinking' alanini OpenRouter reasoning'e cevirir.

    Dusunme butcesi 'reasoning_max_tokens' ile ayarlanir (varsayilan 1024).
    Eskiden 64'e zorlaniyordu; olcum gosterdi ki model arac cagrisi uretmek
    yerine anlatim metnine yaziliyordu (tool_uses=0, 16k karakter salt metin).
    Butceyi kisitlamak planlama yapmasini engelliyordu. 0 verilirse
    reasoning tamamen kapatilir.
    Istemci kendi thinking budget'u gonderdiyse ona dokunulmaz."""
    client_budget = 0
    th = body.get("thinking")
    if isinstance(th, dict):
        try:
            client_budget = int(th.get("budget_tokens") or 0)
        except (TypeError, ValueError):
            client_budget = 0
    if client_budget > 0:
        return None  # istemci kendi butcesini yonetiyor
    budget = int(CONFIG.get("reasoning_max_tokens", 1024))
    if budget <= 0:
        return None
    return {"max_tokens": budget}


_ANTHROPIC_STOP = {"stop": "end_turn", "length": "max_tokens", "tool_calls": "tool_use",
                   "stop_sequence": "stop_sequence", "content_filter": "end_turn"}

# Anthropic stop_reason -> OpenAI finish_reason
_ANTHROPIC_TO_OPENAI_FINISH = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
    "pause_turn": "stop",
    "refusal": "content_filter",
}


def _openai_finish(stop_reason: str | None) -> str:
    return _ANTHROPIC_TO_OPENAI_FINISH.get(stop_reason or "", "stop")


def _anthropic_stop(finish: str | None) -> str:
    # "error" bilincli olarak end_turn DEGIL: upstream hata bildirince sahte
    # basarili tur uretmek, istemciye (Claude Code) hata yerine bos/bitmis
    # cevap gosteriyordu. end_turn'a eslenirse agent hatayi fark edemiyor.
    if finish in (None, "", "error"):
        return "end_turn"
    return _ANTHROPIC_STOP.get(finish, "end_turn")


def _openai_finish_is_error(finish: str | None) -> bool:
    """Upstream streaming'te finish_reason olarak 'error' verdiyse hata budur."""
    return finish == "error"


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


def _estimate_tokens(text: str) -> int:
    """Kaba token tahmini: ~4 karakter/token (ASCII) / ~2.5 (unicode agirlikli)."""
    if not text:
        return 0
    ascii_chars = sum(1 for c in text if ord(c) < 128)
    return max(1, int(ascii_chars / 4 + (len(text) - ascii_chars) / 2.5))


def _count_request_tokens(body: dict) -> int:
    """Anthropic count_tokens icin girdi boyutunu tahmin eder.

    Upstream'e istek atmaz (hem yavas hem kotada maliyetli). Amac sadece
    SDK'nin "kac token kullandim" gostergesini doldurmak; +/-%5 sapma sorun degil."""
    total = 0
    sysv = body.get("system")
    if isinstance(sysv, list):
        for b in sysv:
            total += _estimate_tokens(str((b or {}).get("text", "")))
    elif sysv:
        total += _estimate_tokens(str(sysv))
    for m in body.get("messages") or []:
        if not isinstance(m, dict):
            continue
        c = m.get("content")
        if isinstance(c, str):
            total += _estimate_tokens(c)
        elif isinstance(c, list):
            for b in c:
                if not isinstance(b, dict):
                    continue
                t = b.get("type")
                if t == "text":
                    total += _estimate_tokens(str(b.get("text", "")))
                elif t == "thinking":
                    total += _estimate_tokens(str(b.get("thinking", "")))
                elif t == "tool_use":
                    total += _estimate_tokens(str(b.get("name", "")))
                    total += _estimate_tokens(json.dumps(b.get("input", {}), ensure_ascii=False))
                elif t == "tool_result":
                    total += _estimate_tokens(_block_text(b))
                elif t == "image":
                    # gorsel ~1.5k token (Claude Code gorsel ekledigi icin onemli)
                    total += 1600
        for tc in m.get("tool_calls") or []:
            fn = (tc or {}).get("function") or {}
            total += _estimate_tokens(str(fn.get("name", "")))
            total += _estimate_tokens(str(fn.get("arguments", "")))
    for t in body.get("tools") or []:
        if isinstance(t, dict):
            total += _estimate_tokens(str(t.get("name", "")))
            total += _estimate_tokens(str(t.get("description", "")))
            total += _estimate_tokens(json.dumps(t.get("input_schema", {}), ensure_ascii=False))
    # konusma basina rol/ayrac eklenir
    total += 4 * (len(body.get("messages") or []) + 1)
    return total


# Claude Code / Anthropic SDK: base_url olarak sadece http://127.0.0.1:8756
# verilir. SDK kendisi /v1/messages ekler; biz de geriye donukluk icin /messages
# yolunu destekliyoruz (ikisi de calisir).
@app.post("/messages")
async def anthropic_messages_root(request: Request):
    return await anthropic_messages(request)


@app.post("/messages/count_tokens")
@app.post("/v1/messages/count_tokens")
async def anthropic_count_tokens(request: Request):
    """Anthropic token sayim endpoint'i.

    ONCEKI SURUMDE YOKTI -> 404. @ai-sdk/anthropic (opencode) ve Claude Code
    bu endpoint'i cagirir; 404 alinca SDK tum istegi hata sayiyordu ve
    istemci HIC CEVAP ALAMIYORDU. Dashboard cagirmadigi icin calisiyordu."""
    check_auth(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Gecersiz JSON")
    if not isinstance(body, dict):
        raise HTTPException(400, "Gecersiz JSON")
    return {"input_tokens": _count_request_tokens(body)}


@app.post("/v1/messages")
async def anthropic_messages(request: Request):
    """Anthropic Messages API uyumlu proxy noktasi.
    Header: x-api-key <gateway-key> (veya Bearer). stream destekler."""
    check_auth(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Gecersiz JSON")

    model = body.get("model") or get_active_model()
    # ---- Atria: Anthropic format dogrudan gider, ceviri yok (aktif mod 1 veya model Atria ise) ----
    if _should_route_atria(model):
        stall_s = max(5.0, float(CONFIG.get("stall_timeout", 45)))
        upstream_body = dict(body)
        upstream_body["model"] = model
        if not upstream_body.get("max_tokens"):
            upstream_body["max_tokens"] = 1024
        if body.get("stream"):
            async def sse_atria_passthrough():
                try:
                    client, resp, line_iter, first_line = await open_atria_stream(upstream_body, model)
                except Exception:
                    for chunk in _anthropic_graceful_events():
                        yield chunk
                    return
                try:
                    line = first_line
                    while True:
                        if line:
                            yield (line + "\n").encode() if line.endswith("\n") else (line + "\n").encode()
                        try:
                            line = await asyncio.wait_for(line_iter.__anext__(), timeout=stall_s)
                        except StopAsyncIteration:
                            break
                        except asyncio.TimeoutError:
                            yield _sse_event("error", {"type": "error", "error": {
                                "type": "timeout_error",
                                "message": f"stream {stall_s:.0f}s ver gelmeyince kapatildi"}})
                            break
                except Exception as e:
                    print(f"[ox-gateway] atria stream hatasi, graceful kapanis: {e}")
                    for chunk in _anthropic_graceful_events():
                        yield chunk
                finally:
                    await resp.aclose()
                    await client.aclose()

            return StreamingResponse(
                sse_atria_passthrough(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        result = await call_atria_anthropic(upstream_body, model)
        # model alanini istenen modelle senkron tut
        result["model"] = model
        return result

    payload = _anthropic_to_openai(body)
    rc = _reasoning_config(body)
    if rc:
        payload["reasoning"] = rc
    stall_s = max(5.0, float(CONFIG.get("stall_timeout", 45)))

    # ---- Streaming: OpenAI chunk'larini Anthropic eventlerine cevir ----
    if body.get("stream"):
        payload["stream"] = True

        async def sse_anthropic():
            # Response baslamadan once hata cikabilir; response basladiktan
            # SONRA HTTPException firlatmak ASGI'yi cokertir. Bunun yerine
            # hatayi Anthropic-uyumlu SSE error event'i olarak akiyoruz.
            # Streaming bos/error cevap verirse (freemodel bazen 0 icerikle
            # finish_reason=error donuyor) BIR SONRAKI modele gec.
            # message_start ertelendigi icik bu noktada guvenli: istemciye
            # hicbir sey gonderilmemis oluyor.
            cands = await _candidate_models(model)
            try_idx = 0
            while True:
                try:
                    cand_model = cands[try_idx] if try_idx < len(cands) else model
                    client, resp, line_iter, first_line = await open_stream(
                        {**payload, "model": cand_model}, cand_model)
                    break
                except Exception:
                    try_idx += 1
                    if try_idx < len(cands):
                        print(f"[ox-gateway] stream acilamadi, sonraki modele gecildi "
                              f"(deneme {try_idx + 1}/{len(cands)})")
                        continue
                    # Hata asla agante iletilmez: gecerli minimal bir basarili akis uretilir
                    for chunk in _anthropic_graceful_events():
                        yield chunk
                    return
            # thinking ve text icin AYRI bloklar; her tool_call da kendi blogunda
            block_type: str | None = None   # None | "thinking" | "text" | "tool_use"
            block_index = -1
            tool_block_index: int | None = None  # arguman delta'lari buraya yazilir
            cur_tool = None                 # su an akilan upstream tool kimligi (id/index)
            stop_reason = "end_turn"
            usage_out = 0
            finish_reason = None
            text_chars = 0
            think_chars = 0
            tool_count = 0
            msg_id = "msg_" + str(int(time.time() * 1000))

            # message_start, ilk GERCEK icerik gelene kadar ertelenir.
            # Boylece upstream hicbir sey uretmezse sessizce bos "basarili" tur
            # gondermek yerine temiz bir 'error' event'i yolluyoruz; aksi halde
            # Claude Code ekranda hicbir sey gormeden Esc'e basana kadar bekliyordu.
            started = False

            async def start_msg():
                nonlocal started
                if started:
                    return
                started = True
                yield _sse_event("message_start", {
                    "type": "message_start",
                    "message": {
                        "id": msg_id, "type": "message", "role": "assistant",
                        "model": model, "content": [],
                        "usage": {"input_tokens": 0, "output_tokens": 0},
                    },
                })

            async def emit_block(kind: str, delta_txt: str, tool_name: str = "", tool_id: str = "", force: bool = False):
                nonlocal block_type, block_index, tool_block_index
                # ilk gercek icerik: onceden message_start'i gonder
                async for ev in start_msg():
                    yield ev
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
                        tool_block_index = block_index
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
                    if line and line.startswith("data: ") and "[DONE]" not in line:
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
                                    tool_count += 1
                                    force_new = (tkey != cur_tool) or (block_type != "tool_use")
                                    async for chunk in emit_block(
                                        "tool_use", "",
                                        tool_name=fn["name"],
                                        tool_id=tc.get("id", ""),
                                        force=force_new,
                                    ):
                                        yield chunk
                                    cur_tool = tkey
                                # Argumanlar SADECE aktif tool blogunun indeksine
                                # yazilir. Onceki kod `block_type == "tool_use"`
                                # sartini koyuyordu; araya metin girince
                                # (block_type == "text") tool argumanlari
                                # sessizce kayboluyordu.
                                if fn.get("arguments") and tool_block_index is not None:
                                    yield _sse_event("content_block_delta", {
                                        "type": "content_block_delta",
                                        "index": tool_block_index,
                                        "delta": {"type": "input_json_delta",
                                                  "partial_json": fn["arguments"]},
                                    })
                            fr = ch.get("finish_reason")
                            if fr:
                                finish_reason = fr
                                stop_reason = _anthropic_stop(fr)
                            if ct:
                                text_chars += len(ct)
                            if rt:
                                think_chars += len(rt)
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
                if not started:
                    # Upstream HIC BIR SEY uretmedi (finish_reason=null,
                    # output_tokens=0). Once sessizce bos "basarili" tur
                    # gonderiyorduk; Claude Code ekranda hicbir sey
                    # gormeden Esc'e basana kadar bekliyordu.
                    # Artik retry edilebilir bir hata donuyoruz.
                    DIAG["empty_stream_turns"] = DIAG.get("empty_stream_turns", 0) + 1
                    print(f"[ox-gateway] BOS STREAM: '{model}' hicbir icerik uretmedi "
                          f"(finish_reason={finish_reason}) -> error event")
                    yield _sse_event("error", {
                        "type": "error",
                        "error": {
                            "type": "api_error",
                            "message": ("upstream bos cevap dondurdu; "
                                        "lutfen tekrar deneyin"),
                        },
                    })
                    return
                if block_type is not None:
                    yield _sse_event("content_block_stop", {
                        "type": "content_block_stop", "index": block_index})
                yield _sse_event("message_delta", {
                    "type": "message_delta",
                    "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                    "usage": {"output_tokens": usage_out},
                })
                yield _sse_event("message_stop", {"type": "message_stop"})
                _diag({
                    "path": "/v1/messages",
                    "mode": get_active_mode(),
                    "provider": get_provider()["name"],
                    "model": model,
                    "client_max_tokens": body.get("max_tokens"),
                    "sent_max_tokens": payload.get("max_tokens"),
                    "finish_reason": finish_reason,
                    "stop_reason": stop_reason,
                    "text_chars": text_chars,
                    "think_chars": think_chars,
                    "tool_uses": tool_count,
                    "output_tokens": usage_out,
                    "has_text": text_chars > 0,
                    "has_tool_use": tool_count > 0,
                })
            except Exception as e:
                # Akis ORTASINDA koptu (icerik baslamisti) -> temiz graceful kapanis
                print(f"[ox-gateway] stream ortasinda hata, graceful kapanis: {e}")
                _diag({
                    "path": "/v1/messages", "mode": get_active_mode(),
                    "provider": get_provider()["name"], "model": cand_model,
                    "finish_reason": finish_reason, "stop_reason": stop_reason,
                    "client_max_tokens": body.get("max_tokens"),
                    "sent_max_tokens": payload.get("max_tokens"),
                    "text_chars": text_chars, "think_chars": think_chars,
                    "tool_uses": tool_count, "output_tokens": usage_out,
                    "has_text": text_chars > 0, "has_tool_use": tool_count > 0,
                    "mid_stream_error": str(e)[:200],
                })
                try:
                    if block_type is None:
                        block_index += 1
                        block_type = "text"
                        yield _sse_event("content_block_start", {
                            "type": "content_block_start", "index": block_index,
                            "content_block": {"type": "text", "text": ""}})
                    yield _sse_event("content_block_delta", {
                        "type": "content_block_delta", "index": block_index,
                        "delta": {"type": "text_delta", "text": "\n" + GRACEFUL_TEXT}})
                    yield _sse_event("content_block_stop", {
                        "type": "content_block_stop", "index": block_index})
                    yield _sse_event("message_delta", {
                        "type": "message_delta",
                        "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                        "usage": {"output_tokens": usage_out}})
                    yield _sse_event("message_stop", {"type": "message_stop"})
                except Exception:
                    pass
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
    mode: str | None = None


@app.post("/keys/add")
async def keys_add(req: KeyAddRequest):
    if VAULT_ERROR:
        raise HTTPException(503, f"Kasa okunamadi, yazma yapilmadi: {VAULT_ERROR}")
    m = str(req.mode or get_active_mode())
    if m not in ("1", "2"):
        raise HTTPException(400, "mod 1 veya 2 olmali")
    p = pool_for(m)
    return {"added": p.add(req.key), "total": len(p.keys), "mode": m, "provider": PROVIDERS[m]["name"]}


class KeyRemoveRequest(BaseModel):
    key: str
    mode: str | None = None


@app.post("/keys/remove")
async def keys_remove(req: KeyRemoveRequest):
    if VAULT_ERROR:
        raise HTTPException(503, f"Kasa okunamadi, yazma yapilmadi: {VAULT_ERROR}")
    m = str(req.mode or get_active_mode())
    if m not in ("1", "2"):
        raise HTTPException(400, "mod 1 veya 2 olmali")
    p = pool_for(m)
    return {"removed": p.remove(req.key), "total": len(p.keys), "mode": m, "provider": PROVIDERS[m]["name"]}


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
    default = get_active_model()
    # 1. mod (Atria): model listesi bilinmiyor, varsayilani dondur
    if get_active_mode() == "1":
        return {"default": default, "total": 1, "active_mode": "1",
                "provider": "atria", "provider_url": ATRIA_URL,
                "models": [{"id": default, "name": f"⭐ {default} (varsayılan)",
                            "free": False, "default": True}]}
    raw = await fetch_openrouter_models()
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
    mode: str | None = None


@app.post("/model/set")
async def set_model(req: ModelSetRequest):
    m = str(req.mode or get_active_mode())
    if m not in ("1", "2"):
        raise HTTPException(400, "mod 1 veya 2 olmali")
    val = req.model.strip()
    if not val:
        raise HTTPException(400, "model bos olamaz")
    cfg = load_config()
    pm = cfg.get("provider_models") or {}
    pm[m] = val
    cfg["provider_models"] = pm
    # aktif modun modeli degistiyse eski 'model' alanini da senkron tut
    if m == str(cfg.get("active_mode", "1")):
        cfg["model"] = val
    save_config(cfg)
    CONFIG["provider_models"] = pm
    if m == get_active_mode():
        CONFIG["model"] = val
    MODELS_CACHE["ts"] = 0  # siralamayi tazele
    return {"set": True, "model": val, "mode": m, "provider": PROVIDERS[m]["name"]}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request, req: ChatRequest):
    """OpenAI-uyumlu proxy noktasi; gateway api key ister. stream:true -> SSE."""
    check_auth(request)
    payload: dict = {"messages": _openai_messages_to_payload(req.messages)}
    if req.temperature is not None:
        payload["temperature"] = req.temperature
    if req.max_tokens is not None:
        payload["max_tokens"] = req.max_tokens
    if req.tools:
        payload["tools"] = req.tools
    if req.tool_choice is not None:
        payload["tool_choice"] = req.tool_choice
    if req.parallel_tool_calls is not None:
        payload["parallel_tool_calls"] = req.parallel_tool_calls
    if req.stop is not None:
        payload["stop"] = req.stop
    if req.top_p is not None:
        payload["top_p"] = req.top_p
    if req.seed is not None:
        payload["seed"] = req.seed
    if req.response_format is not None:
        payload["response_format"] = req.response_format

    # ---- Streaming (SSE) ----
    if req.stream:
        # Atria SSE -> OpenAI SSE cevirisi (aktif mod 1 veya model Atria ise)
        if _should_route_atria(req.model or get_active_model()):
            model_a = req.model or get_active_model()
            anth_body = _openai_to_anthropic({**payload, "model": model_a}, model_a)
            stall_a = max(5.0, float(CONFIG.get("stall_timeout", 45)))

            async def sse_atria_openai():
                try:
                    client, resp, line_iter, first_line = await open_atria_stream(anth_body, model_a)
                except Exception:
                    for chunk in _openai_graceful_chunks():
                        yield chunk
                    yield b"data: [DONE]\n\n"
                    return
                seen_tools: dict = {}
                thinking_open = False
                final_reason = None

                def _ochunk(delta: dict, finish=None):
                    return {
                        "id": "chatcmpl-atria", "object": "chat.completion.chunk",
                        "created": int(time.time()), "model": model_a,
                        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                    }

                try:
                    line = first_line
                    while True:
                        s = (line or "").strip()
                        if s.startswith("data:"):
                            try:
                                j = json.loads(s[5:].strip())
                            except ValueError:
                                j = None
                            if isinstance(j, dict):
                                t = j.get("type")
                                if t == "content_block_start":
                                    cb = j.get("content_block") or {}
                                    if cb.get("type") == "tool_use":
                                        idx = len(seen_tools)
                                        seen_tools[cb.get("index", idx)] = cb.get("name", "")
                                        yield ("data: " + json.dumps(_ochunk({
                                            "tool_calls": [{
                                                "index": idx,
                                                "id": cb.get("id") or f"call_{idx}",
                                                "type": "function",
                                                "function": {"name": cb.get("name", ""),
                                                             "arguments": ""},
                                            }]
                                        })) + "\n\n").encode()
                                    elif cb.get("type") == "thinking":
                                        thinking_open = True
                                elif t == "content_block_delta":
                                    d = j.get("delta") or {}
                                    if d.get("type") == "thinking_delta" and d.get("thinking"):
                                        yield ("data: " + json.dumps(_ochunk({
                                            "reasoning": d["thinking"]})) + "\n\n").encode()
                                    elif d.get("type") == "input_json_delta":
                                        yield ("data: " + json.dumps(_ochunk({
                                            "tool_calls": [{
                                                "index": 0,
                                                "function": {"arguments":
                                                             d.get("partial_json", "")},
                                            }]
                                        })) + "\n\n").encode()
                                    elif d.get("text"):
                                        yield ("data: " + json.dumps(_ochunk({
                                            "content": d["text"]})) + "\n\n").encode()
                                elif t == "message_delta":
                                    sr = (j.get("delta") or {}).get("stop_reason")
                                    if sr:
                                        final_reason = _openai_finish(sr)
                                elif t == "message_stop":
                                    final_reason = final_reason or "stop"
                        try:
                            line = await asyncio.wait_for(line_iter.__anext__(),
                                                           timeout=stall_a)
                        except StopAsyncIteration:
                            break
                        except asyncio.TimeoutError:
                            err = {"error": {"message":
                                             f"stream {stall_a:.0f}s ver gelmeyince kapatildi"}}
                            yield ("data: " + json.dumps(err) + "\n\n").encode()
                            break
                    yield ("data: " + json.dumps(_ochunk({}, final_reason or "stop"))
                           + "\n\n").encode()
                    yield b"data: [DONE]\n\n"
                except Exception as e:
                    print(f"[ox-gateway] atria->openai stream hatasi: {e}")
                    for chunk in _openai_graceful_chunks():
                        yield chunk
                    yield b"data: [DONE]\n\n"
                finally:
                    await resp.aclose()
                    await client.aclose()

            return StreamingResponse(
                sse_atria_openai(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "Connection": "keep-alive",
                         "X-Accel-Buffering": "no"},
            )
        payload["stream"] = True
        stall_s = max(5.0, float(CONFIG.get("stall_timeout", 45)))

        async def sse():
            # Response baslamadan once hata cikabilir; HTTPException firlatmak
            # yerine OpenAI-uyumlu error chunk'i + [DONE] akiyoruz.
            try:
                client, resp, line_iter, first_line = await open_stream(payload, req.model)
            except Exception:
                # Hata asla istemciye hata olarak iletilmez: gecerli minimal akis
                for chunk in _openai_graceful_chunks():
                    yield chunk
                yield b"data: [DONE]\n\n"
                return
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
            except Exception as e:
                # Akis ortinda upstream koptu -> hata yerine graceful metin + [DONE]
                print(f"[ox-gateway] stream ortasinda hata, graceful kapanis: {e}")
                try:
                    chunk = {
                        "id": "chatcmpl-graceful", "object": "chat.completion.chunk",
                        "created": int(time.time()), "model": "ox-gateway",
                        "choices": [{"index": 0,
                                     "delta": {"content": "\n" + GRACEFUL_TEXT},
                                     "finish_reason": "stop"}]}
                    yield ("data: " + json.dumps(chunk) + "\n\n").encode()
                    yield b"data: [DONE]\n\n"
                except Exception:
                    pass
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
    return {"model": req.model or get_active_model(), "output": text, "usage": result.get("usage", {})}


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
    return {"model": get_active_model(), "active_mode": get_active_mode(),
            "total": len(results), "results": list(results)}
