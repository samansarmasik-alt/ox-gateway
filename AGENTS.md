# AGENTS.md — ox-gateway

> **Amaç:** Bu repo'yu agent'ın **tek başına, kesintisiz** kurup çalıştırabilmesi. Sorma, kur, doğrula, bitir.
> Kurallar buraya gömülüdür; agent ayrıca README'ye bakmak zorunda değildir.

## 0) Hızlı Harita

| Dosya | Ne |
|---|---|
| `gateway.py` | FastAPI proxy, tek giriş `http://127.0.0.1:8756`. **Mod 1 Atria** (`https://api.atria-asi.ai/v1/messages`, Anthropic) · **Mod 2 OpenRouter** (`https://openrouter.ai/api/v1/chat/completions`, OpenAI). Her modun key havuzu ayrı. |
| `config.json` | `active_mode` (`1`=Atria, `2`=OpenRouter) · `provider_models` (`{"1":"Atria-Dawn-Preview","2":"stealth/space-bunny-alpha"}`) · `fallback_models` (free yedekler). `.gitignore`'da. |
| `config.example.json` | Örnek config, commit'lenir. |
| `sync_opencode.py` | **OpenCode senkronizasyon script'i** (§1.D). `--dry-run` / `--reset` destekler. |
| `static/index.html` | Dashboard: mod seçici, model seçici, key havuzu, chat testi, paralel agent. |
| `agents.py` | Gateway client (`SubAgent`, `run_parallel`, `chat`). |
| `start.bat` | `py -3 -m uvicorn gateway:app --port 8756` |
| `test_*.py` | `test_stream` (OpenAI SSE) · `test_anthropic` (Anthropic+thinking) · `test_tools` · `test_multi_tool` · `test_claude_code`. |

## 1) Kurulum — TEK TUR, SORMA, HEPSİNİ BİTİR

> Aşağıdaki 6 adımı **tek mesajda** çalıştır. Adım adım durup "sırada X var" deme.
> Her adım bağımsız; biri hata verirse **o adımı düzeltip devam et**, kullanıcıya sorma.

### A. Bağımlılık + config
```powershell
pip install -r requirements.txt
if (!(Test-Path config.json)) { Copy-Item config.example.json config.json }
py -3 -m py_compile gateway.py sync_opencode.py   # smoke — hata varsa KESİLME, düzelt
```

### B. Gateway'i ayağa kaldır
```powershell
$running = (netstat -ano | findstr ":8756")
if (-not $running) { Start-Process py -ArgumentList "-3","-m","uvicorn","gateway:app","--port","8756" -WindowStyle Hidden }
# poll: 200 gelene kadar 3sn aralik, en fazla 10 deneme
1..10 | ForEach-Object { try { Invoke-RestMethod http://127.0.0.1:8756/api/conn -TimeoutSec 3; break } catch { Start-Sleep 3 } }
```

### C. Anahtar (key) havuzları
Kullanıcının key'leri `%APPDATA%\ox-gateway\vault.json` (Fernet) içindedir — **dosyayı hiçbir zaman okuma, loglama, commit'leme**.
- Yeni kurulumda havuz boşsa: kullanıcıya *dashboard'dan ekle* de (`http://127.0.0.1:8756`, "Hedef mod" seçerek Mod 1 / Mod 2).
- Key'i olan kurulumda hiçbir şey yapma; havuz `GET /api/stats` içinde `total_keys` olarak görünür.

### D. OpenCode'a model sync (§1'in asıl amacı)
```powershell
py -3 sync_opencode.py
```
Ne yapar: `GET /api/conn` → aktif `provider_models` + `config.json` free fallback'ları → `~/.config/opencode/opencode.json` içindeki `provider.ox` altına `baseURL` + `apiKey` + her modeli `tool_call:true` ile **ekler/günceller**. Mevcut dosyayı `.json.bak-<tarih>` ile yedekler. **Eski modelleri silmez** (`--reset` sadece `ox` provider'ını boşaltmak içindir — kullanma).
Çıktı doğrulaması:
```powershell
py -3 -c "import json,pathlib;d=json.load(open(pathlib.Path.home()/'.config'/'opencode'/'opencode.json',encoding='utf-8'));o=d['provider']['ox'];print(o['options']['baseURL'], list(o['models'].keys()))"
```
Beklenen: `http://127.0.0.1:8756` + `Atria-Dawn-Preview`, `stealth/space-bunny-alpha` ve free fallback'lar.

### E. Claude Code (opsiyonel, kullanıcı isterse)
`%USERPROFILE%\.claude\settings.json` → `env` içindeki 6 model değişkeni (`ANTHROPIC_MODEL`, `*_OPUS_MODEL`, `*_SONNET_MODEL`, `*_HAIKU_MODEL`, `ANTHROPIC_SMALL_FAST_MODEL`, `CLAUDE_CODE_SUBAGENT_MODEL`) aktif gateway modeliyle aynı yapılır. `ANTHROPIC_BASE_URL=http://127.0.0.1:8756` + `ANTHROPIC_AUTH_TOKEN=<gateway key>` kalır. Değiştirmeden önce `.bak-<tarih>` al.

### F. Sağlık kontrolü (bitmeden "bitti" deme)
```powershell
py -3 sync_opencode.py --dry-run                       # hangi modeller senkronlanacak
Invoke-RestMethod http://127.0.0.1:8756/api/stats | ConvertTo-Json -Depth 2
py -3 test_stream.py        # OpenAI SSE
py -3 test_anthropic.py     # Anthropic + thinking
```
Aktif moda göre canlı istek:
```powershell
py -3 -c "import httpx,pathlib;c=httpx.get('http://127.0.0.1:8756/api/conn').json();k=c['gateway_api_key'];r=httpx.post('http://127.0.0.1:8756/v1/chat/completions',json={'model':c['model'],'messages':[{'role':'user','content':'selam'}],'max_tokens':32},headers={'Authorization':f'Bearer {k}'},timeout=60);print(c['active_mode'],r.status_code,r.text[:200])"
```
Rapor: **ne kuruldu / ne doğrulandı / geriye ne kaldı** (3 madde, uzatma).

## 2) Çalışma Kuralları

- **Kesintisiz çalış.** Kurulum/onarım görevinde tur bitirmek için bahaneyle ara verme. Bir adım takılırsa: log'u oku → kök nedeni düzelt → aynı turda devam et. Ancak **veri kaybı riski** olan işlemler (vault silme, `config.json` üzerine yazma, key rotasyonu) kullanıcı onayı ister.
- **Tek mesajda toplu yaz.** Birden fazla dosya/ayar değişikliği gerekiyorsa hepsini **tek turda, paralel tool çağrılarıyla** yap. "Şimdi X'i yazıyorum" deyip tek dosya yazıp durma — kalan dosyaları da aynı turda bitir.
- **Bütün adımları oku, sonra karar ver.** Kod değişikliği önce `gateway.py` + `config.example.json` + `static/index.html` okunur; sonra tek diff'le uygulanır.
- **Doğrulamadan bitirme yok.** `py_compile` + `GET /api/stats` + en az bir canlı `POST /v1/chat/completions` (aktif mod için) geçmeden "bitti" deme.
- **Config tek kaynak.** Aktif model/provider bilgisi her zaman `GET /api/conn` → `provider_models`'tir; model listesini kod içine sabitleme.

## 3) Çalışma Sözleşmesi (kod tarafı)

- Varsayılan `active_mode=1` (Atria). Mod değişimi **sadece** `POST /mode/set {"mode":"1"|"2"}` ile; kalıcı `config.json` + `provider_models` senkronlanır.
- **Yönlendirme:** İstek `model` içinde `atria` geçiyorsa Mod 1'e zorunlu gider (`_should_route_atria`, `gateway.py`), aktif mod ne olursa olsun. Diğerleri aktif moda gider. Bu, "dashboard çalışıyor ama API 429 veriyor" ayrışmasını kapatır.
- **Key yönetimi:** `POST /keys/add {"key":"...","mode":"1"|"2"}` — `mode` yoksa aktif moda. Her modun vault'u ayrı; eski düz liste ilk açılışta Mod 2'ye taşınır ve **silinmez**.
- **429 davranışı:** Rate-limit cooldown `MAX_RATE_LIMIT_COOLDOWN_S=3600` (günlük limit 3 sn'de yeniden denenmez). `X-RateLimit-Reset` yoksa 60 sn.
- **Graceful degradation:** Zincir tükenirse istemciye hata yerine geçerli cevap + `GRACEFUL_TEXT` döner; SSE akışları `event: error` / `data:[DONE]` ile kapanır, ASGI çökmez.

## 4) Sık Hatalar

| Belirti | Neden | Çözüm |
|---|---|---|
| Union/API `429 daily limit`, dashboard `selam` çalışıyor | Aktif mod ile istemcinin istediği model farklı provider'a gitmiş | `_should_route_atria` model adına bakar; `active_mode` + `provider` logla, `GET /api/providers` ile teyit et |
| `space-bunny-alpha: 404` | Eski `ox-alpha` / `union-alpha` referansı kalmış | Sadece `stealth/space-bunny-alpha` |
| `opencode.json` içinde model yok | `sync_opencode.py` çalıştırılmamış | §1.D |
| `sync_opencode.py` "gateway'e baglanilamadi" | Gateway ayakta değil | §1.B (port 8756) |
| OpenCode'da boş cevap | Atria kucuk `max_tokens`'ta thinking döndürür | Gateway thinking→text fallback'i uygular; istemcide `max_tokens` yükselt |
| Tüm key'ler aynı anda denendi | `_available()` cooldown doluysa hepsini aday yapar | Beklenen davranış; `rest_seconds`/`cooldown_every` ayarla |

## 5) Güvenlik

- `config.json`, `vault.json`, `secret.key`, `*token*`, `*key*.json`, `.env*` → **okuma, loglama, commit yok**. Sadece `sync_opencode.py`'nin hedeflediği `~/.config/opencode/opencode.json` düzenlenir (o da git'e girmez).
- Key'ler sadece Fernet kasasında düz metin tutulmaz; `keys/*_out.txt`, `*_err.txt` zaten ignore'lu.
- `git push` ancak kullanıcı açıkça isterse; push öncesi `git status` ile `config.json`'un stage edilmediğini doğrula.

---
*Agent notu: Model listesi kaynağı her zaman `GET /api/conn` → `provider_models`; README'deki listeyi kopyalayıp hardcode'lama.*
