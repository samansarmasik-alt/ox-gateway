# AGENTS.md — ox-gateway

> **Amaç:** Bu repo'yu agent'ın tek başına kurup çalıştırabilmesi için minimal rehber. Delegate + verify odaklı.

## 0) Hızlı Harita

- `gateway.py` — FastAPI proxy, tek giriş noktası (`http://127.0.0.1:8756`). Mod 1 **Atria** (`https://api.atria-asi.ai/v1/messages`, Anthropic), Mod 2 **OpenRouter** (`https://openrouter.ai/api/v1/chat/completions`, OpenAI). Her modun key havuzu ayrı (Fernet + `%APPDATA%\ox-gateway\vault.json`).
- `config.json` — `active_mode` (`1`=Atria, `2`=OpenRouter), `provider_models` (`{"1":"Atria-Dawn-Preview","2":"stealth/space-bunny-alpha"}`), `fallback_models` (free yedekler). Repo'ya girmez (`.gitignore`).
- `config.example.json` — örnek config, commit'lenir.
- `static/index.html` — Dashboard (mod seçici, model seçici, key havuzu, chat testi).
- `agents.py` — minimal gateway client (SubAgent / run_parallel).
- `start.bat` — `py -3 -m uvicorn gateway:app --port 8756`

## 1) Agent Kurulum Görevi (otomatik)

Agent bu repoyu klonladığında **hiç sormadan** şu adımları sırayla yapar:

### A. Bağımlılıklar + config
```powershell
pip install -r requirements.txt
if (!(Test-Path config.json)) { Copy-Item config.example.json config.json }
py -3 -m py_compile gateway.py  # smoke
```

### B. Gateway'i ayağa kaldır
```powershell
Start-Process py -ArgumentList "-3","-m","uvicorn","gateway:app","--port","8756" -WindowStyle Hidden
# bekle: Invoke-RestMethod http://127.0.0.1:8756/api/conn  -> 200 olana kadar 3sn poll
```

### C. OpenCode model sync — örnek (zorunlu)
Gateway çalışıyor; şimdi **aktif modelleri OpenCode'a otomatik ekle** (`%USERPROFILE%\.config\opencode\opencode.json`).
Agent `~/.config/opencode` yoksa oluşturur, varsa yedek alır, `provider.ox.models` altına gateway'in aktif modellerini yazar (tool_call:true). Eski modeller silinmez, sadece eklenir/güncellenir.

> Kaynak: `GET http://127.0.0.1:8756/api/conn` → `provider_models` ve `/api/models` → `default + models`.

**Referans script (agent doğrudan çalıştırır):**
```powershell
py -3 -c @"
import json, pathlib, datetime
gw = __import__('httpx').get('http://127.0.0.1:8756/api/conn', timeout=5).json()
active_models = {
    gw['provider_models']['1']: 'Atria Dawn Preview (Mod 1)',
    gw['provider_models']['2']: 'Space Bunny Alpha (Mod 2, stealth)',
}
# free fallback'ler de kalsın
for fid, name in [
    ('dots-studio/dots-3-note-preview:free','Dots 3 Note (free)'),
    ('liquid/lfm-2.5-2.6b:free','LFM 2.5 fallback'),
    ('nvidia/nemotron-3.5-lightning:free','Nemotron fallback'),
]:
    active_models.setdefault(fid, name)

p = pathlib.Path.home() / '.config' / 'opencode' / 'opencode.json'
p.parent.mkdir(parents=True, exist_ok=True)
bak = p.with_suffix(f'.json.bak-{datetime.datetime.now():%Y%m%d-%H%M%S}')
if p.exists():
    bak.write_bytes(p.read_bytes())
    data = json.loads(p.read_text(encoding='utf-8'))
else:
    data = {'\$schema':'https://opencode.ai/config.json','provider':{}}

ox = data.setdefault('provider', {}).setdefault('ox', {
    'npm':'@ai-sdk/anthropic','name':'Ox Local',
    'options':{'baseURL':'http://127.0.0.1:8756','apiKey': gw['gateway_api_key']}
})
ox['options']['baseURL'] = 'http://127.0.0.1:8756'
ox['options']['apiKey'] = gw['gateway_api_key']
models = ox.setdefault('models', {})
for mid, name in active_models.items():
    models.setdefault(mid, {'name': name, 'tool_call': True})
    models[mid]['name'] = name
    models[mid]['tool_call'] = True
p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding='utf-8')
print(f'synced {len(active_models)} models -> {p} (bak {bak.name})')
"@
```

Beklenen çıktı: `synced 5 models -> ...\opencode.json (bak ...)`
Sonra doğrula:
```powershell
py -3 -c "import json; print(list(json.load(open(r'%USERPROFILE%\.config\opencode\opencode.json',encoding='utf-8'))['provider']['ox']['models'].keys()))"
```

### D. Sağlık kontrolü
```powershell
Invoke-RestMethod http://127.0.0.1:8756/api/stats | ConvertTo-Json -Depth 2
py -3 test_stream.py        # OpenAI SSE
py -3 test_anthropic.py     # Anthropic + thinking
```

## 2) Çalıştırma Sözleşmesi

- **Tek mod kuralı:** Varsayılan `active_mode=1` (Atria). Mod değişimi sadece `POST /mode/set {"mode":"1"|"2"}` ile, kalıcı `config.json` + `provider_models` senkron.
- **Model yönlendirme:** İstek `model` içinde `atria` geçiyorsa Mod1'e zorunlu route (union tutarlılığı için). Diğerleri aktif moda gider.
- **Key yönetimi:** `POST /keys/add {"key":"...","mode":"1"|"2"}` — hedef yoksa aktif moda. Her modun vault'u ayrı, migrate eski liste → Mod2.
- **Doğrulamadan bitirme yok:** `py_compile` + `GET /api/stats` + bir canlı `POST /v1/chat/completions` (her mod için) zorunlu.

## 3) Sık Hatalar

| Belirti | Neden | Çözüm |
|---|---|---|
| Union'da `429 daily limit`, dashboard `selam` çalışıyor | Aktif mod ile istemci modeli farklı provider'a gitmiş | Agent model adıyla route eder (`_should_route_atria`), dashboard mod butonunu göster, `active_mode` logla |
| `space-bunny-alpha: 404` | Eski `ox-alpha/union-alpha` referansı kalmış | Sadece `stealth/space-bunny-alpha` kullan |
| `opencode.json`de model yok | Sync script çalıştırılmamış | §1.C'yi tekrar çalıştır |

## 4) Güvenlik

- `config.json`, `vault.json`, `secret.key`, `*token*`, `.env*` dosyalarını okuma/loga basma/commit'leme.
- Secret'ları `vault.json` (Fernet) dışında düz metin yazma.

---
*Agent notu: Bu dosyadaki §1.C script'i kopyala-yapıştır örnektir; projenin aktif `provider_models` kaynağı her zaman `GET /api/conn`'dır, hardcode model listesi tutma.*
