# -*- coding: utf-8 -*-
"""ox-gateway doctor - tek komutlu kendi kendine teşhis.

Bu repo dışarıdan görünmez şekillerde kırılıyor: gateway sessizce düşüyor,
aktif modun key havuzu boş kalıyor, opencode.json geride kalıyor, model adı
yanlış yazılıyor. doctor.py "ne bozuk ve ne yazacağım" sorusunu tek komutta
yanitlar.

    py -3 doctor.py                # tam rapor
    py -3 doctor.py --quiet        # sadece verdict + FAIL'ler
    py -3 doctor.py --json         # makine-okur (Diger araclar icin)
    py -3 doctor.py --no-network   # gateway/upstream cagrisi yapmadan
    py -3 doctor.py --timeout 10   # ag timeout'u (varsayilan 5 sn)

Cikis kodu: 0 = OK veya sadece WARN, 1 = en az bir FAIL (CI/preflight icin).

GIZLI: bu dosya hicbir sirri okumaz/yazmaz. vault.json ve secret.key HICC
acilmaz. config.json yalnizca TEKIL alanlar okunup basilir, key havuzu
yalnizca SAYIM olarak raporlanir (gateway'in `full` alani asla basilmaz).
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys

BASE = pathlib.Path(__file__).resolve().parent
CONFIG_PATH = BASE / "config.json"
LOG_DIR = BASE / "logs"
LOG_PATH = LOG_DIR / "gateway.log"
PID_PATH = LOG_DIR / "gateway.pid"

HOST = "127.0.0.1"
PORT = 8756
GATEWAY = f"http://{HOST}:{PORT}"

DEPS = ("httpx", "fastapi", "uvicorn", "cryptography")
# Bu imzalar gateway.py / supervisor.py / launcher.py'da gercekten var.
GATEWAY_SIGS = ("uvicorn gateway:app", "supervisor.py", "launcher.py")
GATEWAY_NAMES = ("python.exe", "pythonw.exe")
PROV_NAME = {"1": "atria", "2": "openrouter"}   # gateway.py:210 PROVIDERS ile ayni
OPENCODE_PATH = pathlib.Path.home() / ".config" / "opencode" / "opencode.json"
CLAUDE_PATH = pathlib.Path.home() / ".claude" / "settings.json"

# (kimlik, eslesme, okunabilir ad). gateway.py/supervisor.py print'lerinden.
LOG_SIGS = (
    ("bos_stream", "BOS STREAM", "bos stream (model icerik uretmedi)"),
    ("kapatis_hatasi", "graceful kapanis", "stream hatasi / graceful kapanis"),
    ("stream_acilamadi", "stream acilamadi", "stream acilamadi, model degisti"),
    ("limit_429", "gunluk limite takildi", "gunlik limit (429)"),
    ("istek_reddi", "istegi reddetti", "istek reddi (429/5xx)"),
    ("zincir_tukendi", "zincir tukendi", "key zinciri tukendi (tum havuz cooldown)"),
    ("model_yok", "hicbir model yanit vermedi", "hicbir model yanit vermedi"),
    ("kirpildi", "cevabi kirpildi", "cevap kirpildi (max_tokens)"),
    ("atria_yok", "atria yanit vermedi", "atria yanit vermedi"),
    ("duzeltildi", "istek kendi icinde duzeltildi", "istek kendi icinde duzeltildi"),
    ("bozuk_tur", "bozuk tur -> reasoning", "bozuk tur -> reasoning tekrar"),
    ("kasa_sorunu", "KASA SORUNU", "vault/kasa sorunu"),
    ("havuz_bos", "KEY HAVUZU BOS", "key havuzu bos basladi"),
    ("bind_hatasi", "address already in use", "port bind hatasi"),
    ("port_dolu", "port 8756 zaten dolu", "supervisor: port dolu (cift instance)"),
    ("port_yabanci", "port 8756 baska bir surece ait", "supervisor: port yabanci"),
    ("hizli_cokus", "hizli cokus", "supervisor: ardisik hizli cokus"),
    ("traceback", "Traceback", "python traceback"),
    ("http_500", '" 500 ', "HTTP 500"),
)

# supervisor.py:36-37 ile ayni rotasyon sabitleri. Rotasyon yalniz uvicorn
# kapaliyken olur; bu yuzden aktif log .1..N nesilleriyle birlikte yasar.
ROTATE_BYTES = 8 * 1024 * 1024
ROTATE_KEEP = 3
ROTATE_NEAR = 0.9            # aktif logun bu oranini gecmesi = "dondurulmeye yakin"

PASS, WARN, FAIL, SKIP = "PASS", "WARN", "FAIL", "SKIP"
CHECKS: list[dict] = []


# ---------------------------------------------------------------------------
# yardimcilar
# ---------------------------------------------------------------------------
def add(cid: str, name: str, status: str, details, fix: str = "") -> dict:
    if isinstance(details, str):
        details = [details]
    row = {"id": cid, "name": name, "status": status,
           "details": [d for d in details if d], "fix": fix}
    CHECKS.append(row)
    return row


def mask(secret: str) -> str:
    """Anahtarin yalnizca parmak izi: basinda 8, sonda 4 karakter."""
    s = str(secret or "")
    if not s:
        return "(bos)"
    if len(s) > 20:
        return s[:8] + "..." + s[-4:]
    return s[:2] + "*" * max(4, len(s) - 2)


def read_json(path: pathlib.Path) -> tuple[dict | None, str]:
    """JSON oku. Hata mesaji doner, istisna atmaz."""
    try:
        with path.open("r", encoding="utf-8-sig") as fh:
            return json.load(fh), ""
    except FileNotFoundError:
        return None, "dosya yok"
    except (OSError, ValueError) as e:
        return None, f"okunamadi/bozuk JSON: {e}"


def net_get(path: str, timeout: float, no_network: bool) -> tuple[dict | None, str]:
    """Gateway'den JSON GET. Hicbir durumda istisna atmaz."""
    if no_network:
        return None, "atlandi (--no-network)"
    try:
        import httpx
    except Exception as e:  # pragma: no cover - httpx yoksa 1. kontrol zaten FAIL verir
        return None, f"httpx import edilemedi: {e}"
    try:
        r = httpx.get(f"{GATEWAY}{path}", timeout=timeout)
    except Exception as e:
        return None, f"baglanilamadi: {type(e).__name__}"
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}"
    try:
        return r.json(), ""
    except Exception:
        return None, "JSON parse edilemedi"


def pid_alive(pid: int) -> bool:
    """supervisor.py'deki alive() ile ayni yontem."""
    if os.name != "nt" or pid <= 0:
        return False
    try:
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if h:
            ctypes.windll.kernel32.CloseHandle(h)
            return True
    except Exception:
        pass
    return False


def port_owner_pid(port: int) -> int | None:
    """netstat'tan LISTENING satirinin sahibi (supervisor.py ile ayni)."""
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True,
                             text=True, errors="replace", timeout=25).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        if f":{port}" in line and "LISTENING" in line:
            parts = line.split()
            if parts and parts[-1].isdigit():
                return int(parts[-1])
    return None


def proc_info(pid: int) -> dict:
    """PID -> {'name':..., 'cmdline':...}. Erisim yoksa bos dict."""
    if pid <= 0:
        return {}
    ps = ('Get-CimInstance Win32_Process -Filter "ProcessId=%d" | '
          'ForEach-Object { $_.Name + "||" + $_.CommandLine }' % int(pid))
    try:
        res = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                             capture_output=True, text=True, errors="replace", timeout=30)
    except (OSError, subprocess.SubprocessError):
        return {}
    out = (res.stdout or "").strip()
    if "||" not in out:
        return {}
    name, _, cmd = out.partition("||")
    return {"name": name.strip(), "cmdline": cmd.strip()}


def tail_lines(path: pathlib.Path, max_lines: int = 2000,
               max_bytes: int = 600_000) -> list[str]:
    """Dosyanin SONU: tum dosyayi bellege almadan son satirlari getir."""
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
            raw = fh.read()
    except OSError:
        return []
    return raw.decode("utf-8", errors="replace").splitlines()[-max_lines:]


def _int(val, default: int = 0) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def human(num: int) -> str:
    """Bayt -> okunur. Negatif/mirasiz girdi 0 sayilir."""
    n = max(0, _int(num))
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"


def log_generations() -> list[tuple[int, int]]:
    """gateway.log.1 .. .N -> [(n, byte)], n ascending. Hata durumunda bos."""
    out: list[tuple[int, int]] = []
    try:
        found = list(LOG_DIR.glob(LOG_PATH.name + ".*"))
    except OSError:
        return out
    for p in found:
        tail = p.name.rsplit(".", 1)[-1]
        if not tail.isdigit():
            continue
        try:
            out.append((int(tail), p.stat().st_size))
        except OSError:
            continue
    return sorted(out)


def scrub(text) -> str:
    """Upstream hata metni gibi dis kaynakli stringlerdeki olasi sir maskesi."""
    t = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9_\-.]{8,}", r"\1***", str(text))
    return re.sub(r"\b(sk-[A-Za-z0-9_\-]{6,})", "sk-***", t)


def cfg_set(key: str, value) -> str:
    """config.json'a tek alan yazan kopyala-yapistir komut (dosyayi okumaz)."""
    val = json.dumps(value, ensure_ascii=False).replace("'", "")
    return (f'py -3 -c "import json,pathlib;p=pathlib.Path(\'config.json\');'
            f'd=json.loads(p.read_text(encoding=\'utf-8\'));d[\'{key}\']={val};'
            f'p.write_text(json.dumps(d,indent=2,ensure_ascii=False))"')


# ---------------------------------------------------------------------------
# kontroller
# ---------------------------------------------------------------------------
def check_deps() -> None:
    import importlib
    import platform
    ok, bad, vers = [], [], []
    for mod in DEPS:
        try:
            importlib.import_module(mod)
        except Exception as e:
            bad.append(f"{mod} ({type(e).__name__})")
            continue
        ok.append(mod)
        try:
            from importlib.metadata import version
            vers.append(f"{mod} {version(mod)}")
        except Exception:
            vers.append(mod)
    det = [f"python {platform.python_version()} · " + ", ".join(vers)]
    if bad:
        add("deps", "python + paketler", FAIL, det + ["eksik: " + ", ".join(bad)],
            "pip install -r requirements.txt")
    else:
        add("deps", "python + paketler", PASS, det)


def check_config() -> dict:
    cfg, err = read_json(CONFIG_PATH)
    if cfg is None:
        add("config", "config.json", FAIL, [f"{CONFIG_PATH.name}: {err}"],
            f"Copy-Item config.example.json {CONFIG_PATH.name}")
        return {}
    mode = str(cfg.get("active_mode", ""))
    pm = cfg.get("provider_models") or {}
    det = [f"aktif mod {mode!r} · model {cfg.get('model')!r} · "
           f"protokol {cfg.get('protocol', 'openai')!r}"]
    warns: list[str] = []
    if mode not in ("1", "2"):
        warns.append(f"active_mode {mode!r} gecersiz (1=Atria, 2=OpenRouter olmali)")
    for mid in ("1", "2"):
        if not pm.get(mid):
            warns.append(f"provider_models[{mid}] bos")
        det.append(f"mod {mid} -> {pm.get(mid) or '(yok)'}")
    # provider bloklari: gateway.py:210 PROVIDERS sabitini canli gateway ile karsilastir
    prov, _ = net_get("/api/providers", NET["timeout"], NET["no_network"])
    for mid, blk in ((prov or {}).get("modes") or {}).items():
        if not blk.get("url") or not blk.get("kind"):
            warns.append(f"provider {mid} url/kind eksik")
        det.append(f"{mid}:{blk.get('provider')} kind={blk.get('kind')}")
    if not prov:
        det.append("provider bloklari canli gateway'den dogrulanamadi")
    det.append(f"aktif saglayici: {PROV_NAME.get(mode, '?')} -> "
               f"{pm.get(mode) or cfg.get('model')}")
    add("config", "config.json", WARN if warns else PASS, det,
        cfg_set("active_mode", "1") if warns else "")
    return cfg


def check_port() -> int | None:
    pid = port_owner_pid(PORT)
    hello, err = net_get("/api/hello", NET["timeout"], NET["no_network"])
    det = []
    if pid is None:
        det.append(f"{PORT} uzerinde LISTENING yok")
        add("port", "port + /api/hello", FAIL, det,
            "py -3 launcher.py")
        return None
    det.append(f"{GATEWAY} LISTENING · PID {pid}")
    if hello is not None and hello.get("ok"):
        det.append(f"/api/hello 200 · servis {hello.get('service')} · mod {hello.get('mode')}")
        add("port", "port + /api/hello", PASS, det)
    elif NET["no_network"]:
        det.append("/api/hello atlandi (--no-network)")
        add("port", "port + /api/hello", PASS, det)
    else:
        det.append(f"/api/hello basarisiz: {err}")
        add("port", "port + /api/hello", FAIL, det,
            "py -3 supervisor.py --stop; py -3 launcher.py")
    return pid


def check_owner(pid: int | None) -> None:
    if pid is None:
        add("owner", "port sahibi biz miyiz", SKIP, ["port dinlenmiyor, bakilamadi"])
        return
    info = proc_info(pid)
    if not info:
        add("owner", "port sahibi biz miyiz", WARN,
            [f"PID {pid} surec bilgisi okunamadi (yetki/erisim)"],
            "Get-CimInstance Win32_Process -Filter \"ProcessId=%d\" | ft Name,CommandLine" % pid)
        return
    cmd = info.get("cmdline") or ""
    hit = next((s for s in GATEWAY_SIGS if s in cmd), None)
    det = [f"PID {pid} · {info.get('name')} · {cmd[:150]}"]
    if hit:
        add("owner", "port sahibi biz miyiz", PASS, det + [f"imza eslesti: {hit}"])
    else:
        add("owner", "port sahibi biz miyiz", FAIL,
            det + ["bu bizim surecimiz degil; ozgun gateway hic baslamamis olabilir"],
            f"taskkill /PID {pid} /T /F; py -3 launcher.py")


def check_supervisor() -> None:
    RESTART, NAME = "py -3 supervisor.py --stop; py -3 launcher.py", "supervisor canliligi"
    if not PID_PATH.exists():
        add("supervisor", NAME, WARN, [f"{PID_PATH.name} yok · supervisor'siz baslamis olabilir"],
            "py -3 launcher.py")
        return
    try:
        pid = _int(PID_PATH.read_text(encoding="utf-8", errors="replace").strip(), -1)
    except OSError as e:
        add("supervisor", NAME, FAIL, [f"pid dosyasi okunamadi: {e}"], RESTART)
        return
    if not pid_alive(pid):
        add("supervisor", NAME, FAIL, [f"pid dosyasinda {pid} ama bu PID yasamiyor (bayat pid)"], RESTART)
        return
    info = proc_info(pid)
    cmd, det = (info.get("cmdline") or ""), [f"PID {pid} yasiyor · {info.get('name', '?')}"]
    if info and "supervisor.py" not in cmd:
        add("supervisor", NAME, FAIL, det + [f"bu supervisor.py degil: {cmd[:120]}"], RESTART)
    elif info and (info.get("name") or "").lower() not in GATEWAY_NAMES:
        add("supervisor", NAME, WARN, det + [f"beklenen pythonw.exe degil: {info.get('name')}"], RESTART)
    else:
        add("supervisor", NAME, PASS, det + ["supervisor.py imzasi dogru"])


def check_api() -> dict:
    conn, e1 = net_get("/api/conn", NET["timeout"], NET["no_network"])
    stats, e2 = net_get("/api/stats", NET["timeout"], NET["no_network"])
    diag, e3 = net_get("/api/diag", NET["timeout"], NET["no_network"])
    if conn is None and NET["no_network"]:
        add("api", "api uclari", SKIP, ["--no-network: /api/conn, /api/stats, /api/diag atlandi"])
        return {}
    det, warns, fails = [], [], []
    if conn is None:
        fails.append(f"/api/conn: {e1}")
    else:
        det.append(f"conn · mod {conn.get('active_mode')} · {conn.get('provider')} · "
                   f"{conn.get('model')} · protokol {conn.get('protocol')}")
        det.append(f"base_url {conn.get('base_url')} · anthropic {conn.get('anthropic_base_url')}")
    if stats is None:
        fails.append(f"/api/stats: {e2}")
    else:
        det.append(f"stats · uptime {stats.get('uptime_s')}sn · istek {stats.get('total_requests')}"
                   f" · hata {stats.get('total_fail')}")
    if diag is None:
        warns.append(f"/api/diag: {e3}")
    else:
        turns, empty = _int(diag.get("turns")), _int(diag.get("empty_turns"))
        trunc, last = _int(diag.get("truncated_turns")), diag.get("last") or {}
        est = _int(diag.get("empty_stream_turns"))
        det.append(f"diag · tur {turns} · bos {empty} · bos akis {est} · kirpilan {trunc}")
        if last:
            det.append(f"son tur · {last.get('path')} · {last.get('stop_reason')} · "
                       f"metin {last.get('text_chars')} · think {last.get('think_chars')}"
                       f" · token {last.get('output_tokens')}")
        if turns and empty / turns >= 0.5:
            warns.append(f"turnlarin %{round(empty / turns * 100)}'i bos cevap")
        elif empty:
            warns.append(f"{empty} bos cevap var")
        if trunc:
            warns.append(f"{trunc} tur max_tokens'ta kirpildi")
    st = FAIL if fails else (WARN if warns else PASS)
    add("api", "api uclari", st, det + fails + warns,
        "py -3 supervisor.py --stop; py -3 launcher.py" if fails
        else ("Get-Content logs\\gateway.log -Tail 40" if warns else ""))
    return {"conn": conn, "stats": stats, "diag": diag}


def check_stream_health(api: dict) -> None:
    """/api/diag bos-akis sayaci: flakiness'nin en erken belirtisi.

    `empty_stream_turns` eskiden hep 0 idi (bos-akis yolu hicbir sey saymadan
    donuyordu). Artik guvenilir: bir tur bos akiste kaldiginda zincir ya baska
    modele dustu ya da istemci `event: error` / 502 aldi. Bu kontrol yalnizca
    WARN verir: sayaclar surec basindan birikiyor, payda 1-2 turken oran
    anlamsiz oldugu icin FAIL esigi buradan sagli kiyi amak.
    """
    NAME, MEASURE = "bos akis sayaci", "py -3 bench.py"
    diag = api.get("diag")
    if diag is None:
        add("stream", NAME, SKIP, ["/api/diag okunamadi, sayac alinamadi"])
        return
    if "empty_stream_turns" not in diag:
        add("stream", NAME, SKIP, ["bu gateway surumunde empty_stream_turns yok (eski build)"])
        return
    turns = _int(diag.get("turns"))
    est, emp = _int(diag.get("empty_stream_turns")), _int(diag.get("empty_turns"))
    det = [f"bos akis {est} turda · bos cevap {emp} · toplam {turns}"]
    warns: list[str] = []
    if est:
        det.append(f"son bos akis: empty_stream={bool((diag.get('last') or {}).get('empty_stream'))}")
        symptom = ("zincir diger modele dustu (sessiz fallback)"
                   if len((diag.get("last") or {}).get("models_tried") or []) > 1
                   else "zincir tukendi, istemci 'event: error' / 502 aldi")
        warns.append(f"{est} tur bos akiste kaldi -> {symptom}")
        if turns:
            warns.append(f"bos akis orani %{round(est / turns * 100)} ({est}/{turns}) · "
                         f"olc: {MEASURE}")
    if emp:
        det.append(f"bos cevap orani %{round(emp / turns * 100)}" if turns else "bos cevap orani: (tur yok)")
        if turns >= 4 and emp / turns >= 0.25:
            warns.append(f"turlerin %{round(emp / turns * 100)}'i bos cevapla bitti "
                         f"({emp}/{turns}) · olc: {MEASURE}")
    if not est and not emp:
        det.append("bos akis/bos cevap yok: fallback modeli su an stabil")
    add("stream", NAME, WARN if warns else PASS, det + warns, MEASURE if warns else "")


def check_silent_fallback(api: dict) -> None:
    """Istedigin model mi yanit verdi? `last.requested_model` vs `last.model`."""
    NAME, MEASURE = "sessiz model degisimi", "py -3 bench.py"
    last = (api.get("diag") or {}).get("last") or {}
    if not last:
        add("silent_fallback", NAME, SKIP, ["/api/diag yok ya da henuz tur islenmedi"])
        return
    req, used = last.get("requested_model"), last.get("model")
    tried = [str(m) for m in (last.get("models_tried") or []) if m]
    if not req and not tried:
        # last.model tek basina yetmez: kimin istedigini bilmeden "degisim yok"
        # demek utopik olur. Eski build'lerde bu alanlar yok.
        add("silent_fallback", NAME, SKIP,
            ["bu gateway surumunde last.requested_model yok (eski build); "
             "sessiz fallback olculemez"])
        return
    det = [f"istediğin: {req or '(bilinmiyor)'} · yanit veren: {used or '(model yok)'}"]
    if tried:
        det.append(f"denenen zincir ({len(tried)}): " + ", ".join(tried[:6]))
    if last.get("empty_stream"):
        det.append("son tur bos akiste bitti (empty_stream=True)")
    if last.get("upstream_error"):
        det.append(f"upstream hatasi: {scrub(last['upstream_error'])[:120]}")
    warns: list[str] = []
    if req and used and str(req) != str(used):
        warns.append(f"sessiz fallback: istedin {req}, yanit veren {used}")
    if len(tried) > 1:
        warns.append(f"zincir {len(tried)} model denedi: {tried[0]} calismadi, "
                     f"istenen modele ulasilmadi · olc: {MEASURE}")
    if last.get("empty_stream") and not used:
        warns.append("hicbir model yanit vermedi; istemci hata aldi")
    if not warns:
        det.append("istenen model ile yanitlayan model ayni: sessiz degisim yok")
    add("silent_fallback", NAME, WARN if warns else PASS, det + warns, MEASURE if warns else "")


def check_keys(api: dict) -> None:
    stats = api.get("stats")
    conn = api.get("conn")
    if stats is None:
        add("keys", "key havuzu", SKIP, ["/api/stats yok, sayim yapilamadi"])
        return
    active = str(conn.get("active_mode") if conn else "1")
    det, warns, fails = [], [], []
    modes = stats.get("modes") or {}
    for mid in ("1", "2"):
        info = modes.get(mid) or {}
        total = _int(info.get("total_keys"))
        usable = _int(info.get("active_keys"))
        det.append(f"mod {mid} ({info.get('provider')}): toplam {total} · kullanilabilir {usable} "
                   f"· cooldown {max(0, total - usable)}")
        if total == 0:
            if mid == active:
                fails.append(f"AKTIF mod {mid} icin HICBIR key yok")
            else:
                warns.append(f"mod {mid} icin key yok (aktif degil)")
    if stats.get("keys"):
        fp = mask((stats["keys"][0] or {}).get("key", ""))
        det.append(f"ilk parmak izi {fp} (yalnizca gorunur kismi)")
    det.append(f"aktif havuz toplam {stats.get('total_keys')} · kullanilabilir {stats.get('active_keys')}")
    if _int(stats.get("total_keys")) == 0:
        fails.append("aktif havuz bos")
    add("keys", "key havuzu", FAIL if fails else (WARN if warns else PASS), det + warns,
        f"Invoke-RestMethod -Uri {GATEWAY}/keys/add -Method Post -ContentType 'application/json' -Body '{{\u0022key\u0022:\u0022<KEY>\u0022,\u0022mode\u0022:\u0022{active}\u0022}}'"
        if fails or warns else "")


def check_fallbacks(cfg: dict) -> list[str]:
    fb = cfg.get("fallback_models") or []
    if not isinstance(fb, list):
        add("fallbacks", "free yedek zinciri", FAIL, ["fallback_models liste degil"])
        return []
    if not fb:
        add("fallbacks", "free yedek zinciri", WARN, ["fallback_models bos"],
            cfg_set("fallback_models", ["dots-studio/dots-3-note-preview:free"]))
        return []
    det, warns, actionable, seen = [], [], False, set()
    for mid in fb:
        if not isinstance(mid, str) or not re.fullmatch(r"[\w.\-]+/[\w.\-:]+", mid or ""):
            warns.append(f"gecersiz OpenRouter id: {mid!r}")
            actionable = True
            continue
        model = mid.split("/", 1)[1]
        free = model.endswith(":free")
        det.append(f"{mid} ({'ucretsiz' if free else 'UCRETLI'})")
        if mid in seen:
            warns.append(f"tekrar eden kayit: {mid}")
            actionable = True
        seen.add(mid)
        if not free:
            warns.append(f"{mid} :free degil -> yedek zamani kredi harcar")
            actionable = True
        # Sezgisel: model adindaki parametre boyutu. 4B alti arac kodunda zayif.
        mb = re.search(r"(\d+(?:\.\d+)?)\s*b(?![a-z])", model.lower())
        if mb and float(mb.group(1)) < 4:
            warns.append(f"{mid}: {mb.group(1)}B model (kucuk-model sezgisi, arac kodunda zayif)")
    add("fallbacks", "free yedek zinciri", WARN if warns else PASS, det + warns,
        cfg_set("fallback_models", list(dict.fromkeys(fb))) if actionable else "")
    return [m for m in fb if isinstance(m, str)]


def check_opencode(cfg: dict, api: dict, fallbacks: list[str]) -> None:
    NAME, SYNC = "opencode.json (ox)", "py -3 sync_opencode.py"
    data, err = read_json(OPENCODE_PATH)
    if data is None:
        add("opencode", NAME, FAIL, [f"{OPENCODE_PATH}: {err}"], SYNC)
        return
    ox = (data.get("provider") or {}).get("ox") or {}
    if not ox:
        add("opencode", NAME, FAIL, ["provider.ox yok"], SYNC)
        return
    base = (ox.get("options") or {}).get("baseURL") or ""
    models = ox.get("models") or {}
    conn = api.get("conn") or {}
    want = set((conn.get("provider_models") or cfg.get("provider_models") or {}).values())
    want |= set(fallbacks)
    want.discard(None)
    det = [f"baseURL {base or '(yok)'} · {len(models)} model"]
    det += [f"  {m}  tool_call={(models[m] or {}).get('tool_call')}" for m in sorted(models)[:8]]
    warns, fails = [], []
    if not base:
        fails.append("options.baseURL yok")
    elif str(PORT) not in base or HOST not in base:
        fails.append(f"baseURL bu gateway'i gostermiyor: {base}")
    notool = [m for m, e in models.items() if not (e or {}).get("tool_call")]
    if notool:
        warns.append(f"tool_call:true olmayan {len(notool)} model: " + ", ".join(list(notool)[:4]))
    missing = sorted(m for m in want if m not in models)
    if missing:
        warns.append("eksik model: " + ", ".join(missing))
    extra = sorted(set(models) - want)
    if extra:
        det.append(f"fazladan (eski) model: {', '.join(extra)}")
    st = FAIL if fails else (WARN if warns else PASS)
    add("opencode", NAME, st, det + fails + warns, SYNC if fails or warns else "")


CLAUDE_VARS = ("ANTHROPIC_MODEL", "OPUS_MODEL", "SONNET_MODEL", "HAIKU_MODEL",
              "ANTHROPIC_SMALL_FAST_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL")


def check_claude(api: dict, cfg: dict) -> None:
    NAME = "claude settings"
    data, err = read_json(CLAUDE_PATH)
    if data is None:
        add("claude", NAME, WARN, [f"{CLAUDE_PATH}: {err} (opsiyonel)"])
        return
    env = data.get("env") or {}
    base = env.get("ANTHROPIC_BASE_URL") or ""
    # Config tek kaynak: canli gateway yoksa aktif model config'den okunur.
    active_model = (api.get("conn") or {}).get("model") or str(
        (cfg.get("provider_models") or {}).get(str(cfg.get("active_mode", "1")), "") or "")
    det = [f"ANTHROPIC_BASE_URL {base or '(yok)'}"]
    det += [f"  {t}: " + (f"ayarli ({mask(str(env[t]))})" if env.get(t) else "bos")
            for t in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY")]
    warns, fails = [], []
    if not base:
        warns.append("ANTHROPIC_BASE_URL yok -> Claude Code bu gateway'i kullanmiyor")
    elif f"{HOST}:{PORT}" not in base:
        fails.append(f"ANTHROPIC_BASE_URL gateway'i gostermiyor: {base}")
    drift = []
    for var in CLAUDE_VARS:
        # Ayni ayarin iki yazimi var: ANTHROPIC_OPUS_MODEL / ANTHROPIC_DEFAULT_OPUS_MODEL
        names = [var, f"ANTHROPIC_{var}", f"ANTHROPIC_DEFAULT_{var}"]
        found = next((n for n in names if env.get(n)), "")
        if not found:
            warns.append(f"{names[0]} bos")
            det.append(f"  {names[0]}: (bos)")
            continue
        val = str(env[found])
        det.append(f"  {found}: {val}"
                   + (f"  (aktif: {active_model})" if active_model and val != active_model else ""))
        if active_model and val != active_model:
            drift.append(found)
    if drift:
        warns.append(f"{len(drift)} model degiskeni aktif modelden farkli ({active_model}); "
                     f"_should_route_atria bunlari zaten mod 1'e yonlendirir")
    st = FAIL if fails else (WARN if warns else PASS)
    # Kalici dosya olan settings.json icin uretilen tek satirlik onarim.
    head = ("py -3 -c \"import json,pathlib;p=pathlib.Path.home()/'.claude'/'settings.json';"
            "d=json.loads(p.read_text(encoding='utf-8'));e=d.setdefault('env',{});")
    tail = "p.write_text(json.dumps(d,indent=2,ensure_ascii=False))\""
    body = ("".join(f"e[{json.dumps(k)}]={json.dumps(active_model)};" for k in drift)
            if drift and active_model
            else f"e['ANTHROPIC_BASE_URL']={json.dumps(GATEWAY)};")
    add("claude", NAME, st, det + fails + warns, head + body + tail)


def check_reasoning(cfg: dict) -> None:
    if not cfg:
        add("reasoning", "reasoning ayarlari", SKIP, ["config okunamadi"])
        return
    drm, budget = _int(cfg.get("degenerate_retry")), _int(cfg.get("max_reasoning_budget"))
    reason, cap = _int(cfg.get("reasoning_max_tokens")), _int(cfg.get("max_token_budget"))
    det = [f"degenerate_retry={bool(drm)} · max_reasoning_budget={budget} · "
           f"reasoning_max_tokens={reason} · max_token_budget={cap}"]
    warns = []
    if drm:
        warns.append("degenerate_retry ACIK -> 12288 token'a kadar runaway reasoning dongusu / bos cevap")
    if cap and budget > cap:
        warns.append(f"max_reasoning_budget ({budget}) max_token_budget'u ({cap}) asiyor")
    if cap and reason > cap:
        warns.append(f"reasoning_max_tokens ({reason}) max_token_budget'u ({cap}) asiyor")
    add("reasoning", "reasoning ayarlari", WARN if warns else PASS, det + warns,
        cfg_set("degenerate_retry", False) if drm else "")



def newest_log() -> tuple[pathlib.Path, str]:
    """Imza taramasi icin okunacak dosya: aktif log, yoksa en yeni nesil.

    supervisor.py rotasyonu aktif logu bos birakir (rotate_log yeni dosyayi
    append ile acar). O bos dosyada imza yoktur; icerik gateway.log.1'e
    tasinmistir. Rotasyon sonrasi tarama cokmemeli.
    """
    try:
        if LOG_PATH.exists() and LOG_PATH.stat().st_size:
            return LOG_PATH, ""
    except OSError:
        pass
    gens = log_generations()
    if gens:
        src = LOG_DIR / f"{LOG_PATH.name}.{gens[0][0]}"   # kucuk numara = en yeni
        return src, f"aktif log bos/yok -> imza taramasi {src.name} uzerinde"
    return LOG_PATH, ""


def check_logs() -> None:
    src, note = newest_log()
    if not src.exists():
        add("logs", "log sagligi", WARN, [f"{LOG_PATH.name} yok (supervisor ile baslatilmamis?)"],
            "py -3 launcher.py")
        return
    lines = tail_lines(src)
    if not lines:
        add("logs", "log sagligi", WARN, ["log dosyasi bos"])
        return
    text = "\n".join(lines)
    recent = "\n".join(lines[-200:])   # su anki kosu; eski olaylar yaniltmasin
    named = {cid: label for cid, _, label in LOG_SIGS}
    counts = {cid: text.lower().count(needle.lower()) for cid, needle, _ in LOG_SIGS}
    now = {cid: recent.lower().count(needle.lower()) for cid, needle, _ in LOG_SIGS}
    det = [f"son {len(lines)} satir ({len(lines[-200:])} su anki kosu) · {src.name}"]
    if note:
        det.append(note)
    det += [f"  {named[cid]}: {n}" + ("  <-- su anki kosuda" if now.get(cid) else "")
            for cid, n in sorted(counts.items(), key=lambda kv: -kv[1]) if n]
    det.append(f"  HTTP 4xx access: {len(re.findall(r'\" 4\d\d ', text))} · "
               f"5xx access: {len(re.findall(r'\" 5\d\d ', text))}")
    FATAL = ("bind_hatasi", "hizli_cokus", "traceback", "kasa_sorunu", "havuz_bos")
    RECUR = ("bos_stream", "limit_429", "istek_reddi", "zincir_tukendi", "model_yok")
    # FAIL yalnizca SU ANKI kosuda gorulen felaketler icin; gecmis olaylar WARN.
    fatal = sum(now[c] for c in FATAL)
    rec = sum(counts[c] for c in RECUR)
    st = FAIL if fatal else (WARN if (rec or sum(counts[c] for c in FATAL)) else PASS)
    add("logs", "log sagligi", st, det,
        "Get-Content logs\\gateway.log -Tail 60" if st in (FAIL, WARN) else "")


def check_log_rotation() -> None:
    """supervisor.py rotasyonu: aktif log + gateway.log.1..N ayak izi.

    rotate_log yalniz uvicorn kapaliyken calistigi icin aktif log esigi assa bile
    cocuk durana kadar buyumeye devam eder; bu yuzden "aktif log buyuk" tek
    basina hata degil, onemsiz bilgidir. .1 var olmasi da tek basina FAIL
    degildir: normalde beklenen durumdur.
    """
    NAME = "log rotasyonu"
    gens = log_generations()
    gen_total = sum(s for _, s in gens)
    try:
        active: int | None = LOG_PATH.stat().st_size
    except FileNotFoundError:
        active = None
    except OSError as e:
        add("logrotate", NAME, WARN, [f"{LOG_PATH.name} okunamadi: {e}"])
        return
    cap = ROTATE_BYTES * (ROTATE_KEEP + 1)     # 3 nesil + 1 aktif = tasarim tavantisi
    det = []
    if active is None:
        det.append(f"{LOG_PATH.name} yok"
                   + (f" ama {len(gens)} nesil arsiv var (supervisor yazmiyor olabilir)"
                      if gens else " (supervisor ile baslatilmamis?)"))
    else:
        det.append(f"aktif {LOG_PATH.name} {human(active)} · dondurme esigi "
                   f"{human(ROTATE_BYTES)} (%{round(active / ROTATE_BYTES * 100)})")
    if gens:
        det.append(f"donmus nesil {len(gens)} (supervisor {ROTATE_KEEP} tutar) · toplam "
                   f"{human(gen_total)} · " + ", ".join(f".{n}={human(s)}" for n, s in gens))
    else:
        det.append("donmus nesil yok (henuz rotasyon olmadi)")
    det.append(f"toplam ayak izi {human((active or 0) + gen_total)} · tasarim tavantisi {human(cap)}")
    warns: list[str] = []
    if active is None and not gens:
        warns.append(f"{LOG_PATH.name} yok: supervisor loglamiyor ya da hic baslamadi")
    if active is not None and active >= ROTATE_BYTES * ROTATE_NEAR:
        warns.append(f"aktif log dondurme esigine yakin/uzerinde: supervisor uvicorn "
                     f"durdurunce dondurecek ({human(ROTATE_BYTES)} esigi)")
    if (active or 0) + gen_total > cap:
        warns.append(f"toplam log {human((active or 0) + gen_total)} > {human(cap)}: "
                     f"disk/okuma maliyeti buyuyor")
    if len(gens) > ROTATE_KEEP:
        warns.append(f"{len(gens)} nesil var ama rotate_log sadece {ROTATE_KEEP} tutuyor; "
                     f"fazlalikler kendiliginden silinmez")
    add("logrotate", NAME, WARN if warns else PASS, det + warns,
        f"Remove-Item {LOG_DIR.name}\\{LOG_PATH.name}.{ROTATE_KEEP + 1}"
        if len(gens) > ROTATE_KEEP else "")


# ---------------------------------------------------------------------------
# cikti
# ---------------------------------------------------------------------------
NET: dict = {"no_network": False, "timeout": 5.0}


def verdict() -> str:
    nf = sum(1 for c in CHECKS if c["status"] == FAIL)
    nw = sum(1 for c in CHECKS if c["status"] == WARN)
    return f"BROKEN ({nf})" if nf else (f"WARNINGS ({nw})" if nw else "OK")


def top_fixes() -> list[str]:
    return list(dict.fromkeys(c["fix"] for c in CHECKS
                              if c["fix"] and c["status"] in (FAIL, WARN)))[:3]


def render() -> str:
    head = (f"ox-gateway doctor · --no-network (ag cagrisi yok)" if NET["no_network"]
            else f"ox-gateway doctor · gateway {GATEWAY} · timeout {NET['timeout']:g}s")
    out = [head, "-" * 78]
    width = max((len(c["name"]) for c in CHECKS), default=8)
    for i, c in enumerate(CHECKS, 1):
        out.append(f"[{c['status']:4}] {i:2}. {c['name']:<{width}}")
        out += [f"          {d}" for d in c["details"]]
        if c["fix"] and c["status"] in (FAIL, WARN):
            out.append(f"          fix: {c['fix']}")
        out.append("")
    out.append("=" * 78)
    out.append(f"VERDICT: {verdict()}")
    fixes = top_fixes()
    if fixes:
        out.append("")
        out.append("ilk 3 onarim (kopyala-yapistir):")
        out += [f"  {f}" for f in fixes]
    return "\n".join(out)


def render_quiet() -> str:
    out = [f"VERDICT: {verdict()}"]
    for c in CHECKS:
        if c["status"] != FAIL:
            continue
        out.append(f"[FAIL] {c['name']}: " + ("; ".join(c["details"]) or "detay yok")[:300])
        if c["fix"]:
            out.append(f"       fix: {c['fix']}")
    return "\n".join(out)


def as_json() -> str:
    return json.dumps({
        "ok": not any(c["status"] == FAIL for c in CHECKS),
        "verdict": verdict(), "gateway": GATEWAY,
        "counts": {s: sum(1 for c in CHECKS if c["status"] == s) for s in (PASS, WARN, FAIL, SKIP)},
        "fixes": top_fixes(), "checks": CHECKS,
    }, ensure_ascii=False, indent=2)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="doctor.py", description="ox-gateway tek komutlu teşhis")
    ap.add_argument("--json", action="store_true", help="makine-okur cikti")
    ap.add_argument("--quiet", action="store_true", help="sadece verdict + FAIL'ler")
    ap.add_argument("--no-network", action="store_true", help="ag cagrisi yapma")
    ap.add_argument("--timeout", type=float, default=5.0, help="ag timeout saniye")
    args = ap.parse_args(argv)
    NET["no_network"] = bool(args.no_network)
    NET["timeout"] = max(0.5, float(args.timeout))
    os.chdir(BASE)

    # Her kontrol kendi icinde yutuyor; beklenmedik hatada da rapor bozulmaz.
    def run(cid: str, name: str, fn, *a) -> object:
        try:
            return fn(*a)
        except Exception as e:  # noqa: BLE001 - tani araci asla cokmemeli
            add(cid, name, WARN, [f"kontrol hatasi: {type(e).__name__}: {e}"])
            return None

    cfg = run("config", "config.json", check_config) or {}
    pid = run("port", "port + /api/hello", check_port)
    run("owner", "port sahibi biz miyiz", check_owner, pid)
    run("supervisor", "supervisor canliligi", check_supervisor)
    api = run("api", "api uclari", check_api) or {}
    run("stream", "bos akis sayaci", check_stream_health, api)
    run("silent_fallback", "sessiz model degisimi", check_silent_fallback, api)
    run("keys", "key havuzu", check_keys, api)
    fb = run("fallbacks", "free yedek zinciri", check_fallbacks, cfg) or []
    run("opencode", "opencode.json (ox)", check_opencode, cfg, api, fb)
    run("claude", "claude settings", check_claude, api, cfg)
    run("reasoning", "reasoning ayarlari", check_reasoning, cfg)
    run("logs", "log sagligi", check_logs)
    run("logrotate", "log rotasyonu", check_log_rotation)


    if args.json:
        print(as_json())
    elif args.quiet:
        print(render_quiet())
    else:
        print(render())
    return 1 if any(c["status"] == FAIL for c in CHECKS) else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:  # noqa: BLE001 - son emniyet agi
        print(f"doctor kendi hatasi: {type(e).__name__}: {e}")
        sys.exit(2)
