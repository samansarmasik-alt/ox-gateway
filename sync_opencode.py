# -*- coding: utf-8 -*-
"""Gateway'in aktif modellerini OpenCode config'ine otomatik sync eder.

Kullanim:
    py -3 sync_opencode.py               # varsayilan: ~/.config/opencode/opencode.json
    py -3 sync_opencode.py --dry-run     # sadece goster, yazma
    py -3 sync_opencode.py --reset       # ox provider modellerini once temizle

Ne yapar:
  1) Calisan gateway'den GET /api/conn okur (aktif mod + provider_models + api key)
  2) Mevcut opencode.json'u yedekler
  3) provider.ox altina gateway modellerini tool_call:true ile ekler/gunceller
  4) config.json'daki free fallback modellerini de yedek olarak ekler
  5) Eski modelleri SILMEZ (--reset sadece ox provider'ini bosaltir)
"""
import argparse
import datetime
import json
import pathlib
import sys

GATEWAY = "http://127.0.0.1:8756"
CONFIG_LOCAL = pathlib.Path(__file__).parent / "config.json"
CONFIG_EXAMPLE = pathlib.Path(__file__).parent / "config.example.json"


def fetch_conn(gateway: str = GATEWAY) -> dict:
    import httpx
    r = httpx.get(f"{gateway}/api/conn", timeout=5)
    r.raise_for_status()
    return r.json()


def build_models(conn: dict) -> dict:
    """Gateway model + free fallback'larini {model_id: display_name} olarak dondurur."""
    pm = conn.get("provider_models") or {}
    m1 = pm.get("1") or "Atria-Dawn-Preview"
    m2 = pm.get("2") or "stealth/space-bunny-alpha"
    models = {
        m1: "Atria Dawn Preview (Mod 1, atria-asi.ai)",
        m2: "Space Bunny Alpha (Mod 2, stealth)",
    }
    for fid, name in _fallbacks():
        models.setdefault(fid, f"{name} (free fallback)")
    return models


def _fallbacks() -> list[tuple[str, str]]:
    """config.json'daki fallback_models listesini [(id, kisa_ad)] olarak cikarir."""
    for cfg in (CONFIG_LOCAL, CONFIG_EXAMPLE):
        if not cfg.exists():
            continue
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
        except Exception:
            continue
        out = []
        for mid in data.get("fallback_models") or []:
            short = mid.split("/")[-1].replace(":free", "").replace("-", " ")
            out.append((mid, short))
        if out:
            return out
    return []


def target_path() -> pathlib.Path:
    env = __import__("os").environ.get("OPENCODE_CONFIG")
    if env:
        return pathlib.Path(env)
    return pathlib.Path.home() / ".config" / "opencode" / "opencode.json"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gateway", default=GATEWAY)
    ap.add_argument("--config", default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--reset", action="store_true",
                    help="ox provider'ini sifirlayip sadece aktif modelleri yaz")
    args = ap.parse_args()

    try:
        conn = fetch_conn(args.gateway)
    except Exception as e:
        print(f"HATA: gateway'e baglanilamadi ({args.gateway}): {e}")
        print("Once gateway'i baslat: start.bat  (ya da: py -3 -m uvicorn gateway:app --port 8756)")
        return 1

    models = build_models(conn)
    p = pathlib.Path(args.config) if args.config else target_path()
    p.parent.mkdir(parents=True, exist_ok=True)

    data = {}
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"HATA: {p} okunamadi ({e}) — el ile onar")
            return 1

    bak = p.with_suffix(f".json.bak-{datetime.datetime.now():%Y%m%d-%H%M%S}")
    if p.exists():
        bak.write_bytes(p.read_bytes())

    provider = data.setdefault("provider", {})
    ox = provider.get("ox") or {}
    if args.reset:
        ox = {}
    ox.setdefault("npm", "@ai-sdk/anthropic")
    ox.setdefault("name", "Ox Local")
    opts = ox.setdefault("options", {})
    opts["baseURL"] = args.gateway
    opts["apiKey"] = conn["gateway_api_key"]
    m = ox.setdefault("models", {})
    if args.reset:
        m = {}
        ox["models"] = m
    for mid, name in models.items():
        entry = m.setdefault(mid, {})
        entry["name"] = name
        entry["tool_call"] = True
    provider["ox"] = ox

    if args.dry_run:
        print(f"DRY-RUN -> {p}")
        print(f"  gateway : {conn['provider']} (mod {conn['active_mode']}) {conn['model']}")
        print(f"  baseURL : {opts['baseURL']}")
        for mid, name in models.items():
            print(f"  model   : {mid}  ({name})")
        return 0

    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"OK: {len(models)} model senkronlandi -> {p}")
    print(f"    yedek: {bak.name if p.exists() else '(yeni dosya)'}")
    print(f"    aktif: mod {conn['active_mode']} / {conn['provider']} / {conn['model']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
