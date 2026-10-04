# AGENTS.md — ox-gateway

> **Amaç:** Bu repo'yu agent'ın **tek başına, kesintisiz** kurup çalıştırabilmesi. Sorma, kur, doğrula, bitir.
> Kurallar buraya gömülüdür; agent ayrıca README'ye bakmak zorunda değildir.

## 0) Hızlı Harita

| Dosya | Ne |
|---|---|
| `gateway.py` | FastAPI proxy, tek giriş `http://127.0.0.1:8756`. **Mod 1 Atria** (`https://api.atria-asi.ai/v1/messages`, Anthropic) · **Mod 2 OpenRouter** (`https://openrouter.ai/api/v1/chat/completions`, OpenAI). Her modun key havuzu ayrı. |
| `config.json` | `active_mode` · `provider_models` · `fallback_models` · token/reasoning ayarları. `.gitignore`'da, **okuma yasak**. |
| `config.example.json` | Commit'lenen şablon; `gateway_api_key` bilerek boş (ilk açılışta üretilir). |
| `launcher.py` | `supervisor.py`'yi `DETACHED_PROCESS` + `CREATE_BREAKAWAY_FROM_JOB` ile başlatır. Kısa ömürlü. |
| `supervisor.py` | `pythonw.exe` altında görünmez süreç yöneticisi: `logs\gateway.log`, `logs\gateway.pid`, ustel backoff'lu yeniden başlatma, `--stop`. |
| `start-detached.bat` | Konsolsuz başlatma giriş noktası → `launcher.py`. Port doluysa çıkar. |
| `start.bat` | Ön plan varyantı; `uvicorn` bu pencerede çalışır, `pause` yoktur. |
| `stop-gateway.bat` | `py -3 supervisor.py --stop`. |
| `sync_opencode.py` | OpenCode senkronizasyonu (§1.D). `--dry-run` / `--reset` destekler. |
| `doctor.py` | Tek komutlu teşhis, 14 kontrol (§1.F). `--json` / `--quiet` / `--no-network` / `--timeout`. Yeni: `stream` (boş akış), `silent_fallback` (istenen≠yanıtlayan model), `logrotate`. |
| `bench.py` | Gecikme/güvenilirlik ölçümü (§1.G). `--n` / `--concurrency` / `--json` / `--out`. |
| `static/index.html` | Dashboard: mod seçici, model seçici, key havuzu, chat testi, paralel agent. |
| `agents.py` | Gateway client (`SubAgent`, `run_parallel`, `chat`). |
| `tests/` | stdlib unittest regresyon paketi (§1.F). |

## 1) Kurulum — TEK TUR, SORMA, HEPSİNİ BİTİR

> Aşağıdaki adımları **tek mesajda** çalıştır. Adım adım durup "sırada X var" deme.
> Her adım bağımsız; biri hata verirse **o adımı düzeltip devam et**, kullanıcıya sorma.

### A. Bağımlılık + config
```powershell
pip install -r requirements.txt
if (!(Test-Path config.json)) { Copy-Item config.example.json config.json }
py -3 -m py_compile gateway.py sync_opencode.py launcher.py supervisor.py doctor.py bench.py
```
`config.json` yoksa gateway import anında `FileNotFoundError` verir. `gateway_api_key`
boş bırakılsın: gateway ilk açılışta `ox-<rastgele>` üretip kalıcı yazar.

### B. Gateway'i ayağa kaldır
**`uvicorn`'i doğrudan `Start-Process` ile başlatma.** Çağıran kabuğun süreç ağacında
kalır; terminal kapanınca gateway de düşer. Doğru zincir:
`start-detached.bat` → `launcher.py` → `supervisor.py` (pythonw, görünmez) → uvicorn.
```powershell
py -3 launcher.py
# supervisor + uvicorn ayaga kalkana kadar /api/hello poll (3 sn aralik, en fazla 15 deneme)
1..15 | ForEach-Object {
  try { if ((Invoke-RestMethod http://127.0.0.1:8756/api/hello -TimeoutSec 3).ok) { break } }
  catch { Start-Sleep 3 }
}
```
`/api/hello` anahtar istemez; `GET`/`HEAD` ikisini de kabul eder. Ayakta değilse
`logs\gateway.log`'a bak. Ön planda izlemek istenirse `.\start.bat`.

### C. Anahtar (key) havuzları
Kullanıcının key'leri `%APPDATA%\ox-gateway\vault.json` (Fernet) içindedir — **dosyayı hiçbir zaman okuma, loglama, commit'leme**.
- Yeni kurulumda havuz boşsa: kullanıcıya *dashboard'dan ekle* de (`http://127.0.0.1:8756`, "Hedef mod" seçerek Mod 1 / Mod 2).
- Key'i olan kurulumda hiçbir şey yapma; havuz `GET /api/stats` içinde `total_keys` olarak görünür.
- `config.json` içindeki `api_keys` eski düz listedir; ilk açılışta **Mod 2'ye taşınır ve silinmez**.

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
`%USERPROFILE%\.claude\settings.json` → `env` içindeki model değişkenleri (`ANTHROPIC_MODEL`, `*_OPUS_MODEL`, `*_SONNET_MODEL`, `*_HAIKU_MODEL`, `ANTHROPIC_SMALL_FAST_MODEL`, `CLAUDE_CODE_SUBAGENT_MODEL`) aktif gateway modeliyle aynı yapılır. `ANTHROPIC_BASE_URL=http://127.0.0.1:8756` + `ANTHROPIC_AUTH_TOKEN=<gateway key>` kalır. Değiştirmeden önce `.bak-<tarih>` al. `py -3 doctor.py` bu dosyayı kendisi de kontrol eder.

### F. Sağlık kontrolü (bitmeden "bitti" deme)
```powershell
py -3 doctor.py                    # tam rapor; FAIL/WARN satirlari ne yapilacagini yazar
py -3 doctor.py --quiet            # sadece verdict + FAIL'ler
py -3 doctor.py --no-network       # ag cagrisi yapmadan (config/log/pid kontrolleri)
py -3 -m unittest discover -s tests -v     # regresyon paketi, pytest gerekmez
```
`doctor.py` çıkış kodu: `0` = OK/WARN, `1` = en az bir FAIL. Testler `gateway.py`'yi
salt okunur import eder; çalışan gateway'e karşı canlı testler gateway kapalıysa
otomatik atlanır. Aktif moda göre canlı istek:
```powershell
py -3 -c "import httpx;c=httpx.get('http://127.0.0.1:8756/api/conn').json();k=c['gateway_api_key'];r=httpx.post('http://127.0.0.1:8756/v1/chat/completions',json={'model':c['model'],'messages':[{'role':'user','content':'selam'}],'max_tokens':32},headers={'Authorization':f'Bearer {k}'},timeout=60);print(c['active_mode'],r.status_code,r.text[:200])"
```
Amaçlı canlı test betikleri (gateway + key gerekir): `py -3 test_stream.py`,
`py -3 test_anthropic.py`, `py -3 test_tools.py`, `py -3 test_multi_tool.py`,
`py -3 test_claude_code.py`.

### G. Ölçüm (yalnızca performans/limit şüphesi varsa)
```powershell
py -3 bench.py                                   # 5 istek, es zamanlilik 3
py -3 bench.py --n 20 --concurrency 5
py -3 bench.py --mode 2 --n 10                   # modu DEGISTIRMEZ, sadece olculecek modeli secer
py -3 bench.py --json
py -3 bench.py --out logs\bench.jsonl             # trend biriktirme
```
`bench.py` her koşuda `max_tokens: 131072` göndererek `_clamp_max_tokens` tavanını
(32000) görünür kılar. Gateway'i değiştirmez, `/mode/set` çağırmaz, anahtarı yalnızca
bellekte tutar.

Rapor: **ne kuruldu / ne doğrulandı / geriye ne kaldı** (3 madde, uzatma).

## 2) Çalışma Kuralları

- **Kesintisiz çalış.** Kurulum/onarım görevinde tur bitirmek için bahaneyle ara verme. Bir adım takılırsa: log'u oku → kök nedeni düzelt → aynı turda devam et. Ancak **veri kaybı riski** olan işlemler (vault silme, `config.json` üzerine yazma, key rotasyonu) kullanıcı onayı ister.
- **Tek mesajda toplu yaz.** Birden fazla dosya/ayar değişikliği gerekiyorsa hepsini **tek turda, paralel tool çağrılarıyla** yap. "Şimdi X'i yazıyorum" deyip tek dosya yazıp durma — kalan dosyaları da aynı turda bitir.
- **Bütün adımları oku, sonra karar ver.** Kod değişikliği önce `gateway.py` + `config.example.json` + `static/index.html` okunur; sonra tek diff'le uygulanır.
- **Config tek kaynak.** Aktif model/provider bilgisi her zaman `GET /api/conn` → `provider_models`'tir; model listesini kod veya README'ye sabitleme.
- **Süreç yönetimini elle yapma.** `uvicorn`'i doğrudan `Start-Process`/arka plan başlatma; `launcher.py` + `supervisor.py` vardır. Doğrudan başlatılan gateway, çağıran kabuk kapanınca ölür.

## 3) Çalışma Sözleşmesi (kod tarafı)

- Varsayılan `active_mode=1` (Atria). Mod değişimi **sadece** `POST /mode/set {"mode":"1"|"2"}` ile; kalıcı `config.json` + `provider_models` senkronlanır.
- **Yönlendirme:** İstek `model` içinde `atria` geçiyorsa Mod 1'e zorunlu gider (`_should_route_atria`, `gateway.py`), aktif mod ne olursa olsun. Diğerleri aktif moda gider. Bu, "dashboard çalışıyor ama API 429 veriyor" ayrışmasını kapatır.
- **Key yönetimi:** `POST /keys/add {"key":"...","mode":"1"|"2"}` — `mode` yoksa aktif moda. Her modun vault'u ayrı; eski düz liste ilk açılışta Mod 2'ye taşınır ve **silinmez**.
- **Token tavanı:** `_MAX_OUTPUT_TOKENS = 32000` sabittir; `_clamp_max_tokens` her istekte uygulanır (`<=0`/çöp → 1024). OpenCode 131072/128000 istiyor; sağlayıcı bunu ya reddediyor ya da bütçeyi reasoning'e harcayıp boş/kırpılmış cevap döndürüyor. Bu yüzden `min_token_budget` (1024) ve `_apply_budget_floor` vardır.
- **`count_tokens`:** `POST /v1/messages/count_tokens` (+ `/messages/count_tokens` alias) **mutlaka** 200 dönmeli. OpenCode'un Anthropic SDK'sı her istekten önce çağırır; 404 alınca SDK tüm isteği hata sayıp istemci hiç cevap almıyor. Yanıt **yerel tahminle** üretilir (upstream çağrısı yok): ASCII ~4 karakter/token, unicode ~2.5, görsel **sabit ~1600** token. Sapma yüzde birkaç düzeyindedir, hata değildir.
- **`/api/hello`:** `GET`/`HEAD`, anahtar istemez. İstemciler bunu yoklarken 404'ü "gateway ulaşılamaz" sanıyordu. Sağlık kontrolünde `/api/conn` yerine bunu kullan.
- **Thinking / reasoning:** `reasoning_effort` = `off` | `minimal` | `low` | `medium` | `high` | `xhigh` (ya da sayı, ör. `4096`). Sayı seçilirse `reasoning.max_tokens`, ad seçilirse `reasoning.effort` gönderilir — **ikisi asla birlikte** gönderilmez, OpenRouter `HTTP 400` verir ("Only one of reasoning.effort and reasoning.max_tokens can be specified"). Değiştir: `POST /reasoning/set {"effort":"high"}` (auth'lu) veya dashboard'daki Thinking seçici; okumak için `GET /reasoning`.
  - **OpenCode thinking seçici:** `sync_opencode.py` her modele `reasoning:true`, `limit` (output 32000 = gateway tavanı) ve `variants` yazar: `off` (`thinking:{type:"disabled"}`), `low`/`medium`/`high`/`max` (`budgetTokens` 4096/10240/16384/24576 → gateway %60 ile 4096/10240/16384/19200'a kirpar). Bu şekil opencode'un kendi kodundan çıkarıldı (`case "@ai-sdk/anthropic": return {thinking:{type:"enabled", budgetTokens:Z}}`); SDK gövdeye `thinking.budget_tokens` çevirir, gateway o alanı okur.
  - **opencode'da bu seçenekler model listesinde ayrı kontrol olarak ÇIKMAZ.** opencode'un kendi tuş haritası: `Ctrl+T` = "Cycle model variants". `reasoning` bayrağı varsayılan **false** olduğu için, o işaretlenmeden de thinking arayüzü açılmıyor.
- **Model başına effort:** `POST /reasoning/set {"effort":"low","model":"vendor/model"}` o modele özel değer yazar (config `reasoning_effort_by_model`); `POST /reasoning/reset` hepsini siler. Çözümleme sırası: **istemci > modele özel > genel varsayılan**. Dashboard'da model listesinin altında her model için effort seçici vardır; `/api/models` her modele `effort` alanı ile döner.
  - **Düzeltilen ikinci hata:** istemcinin OpenAI tarzı `reasoning` / `reasoning_effort` alanları **hiç okunmuyordu** — opencode effort seçip gönderiyor, gateway sessizce kendi varsayılanını kullanıyordu. Artık okunuyor.
  - Ölçülen davranış: `reasoning` alanı hiç gönderilmezse düşünme karakteri **0**; gönderilince 240–570 (space-bunny, 2 istek). Düşünme bütçesi cevap bütçesinin en fazla **%60'ı** kadar (`_clamp_thinking_budget`) — aksi halde model bütçeyi tek başına yiyip boş metin döner. `thinking: {"type":"disabled"}` kapatır.
  - Ölçüm notu (dürüst): `off/low/medium/high` seviyeleri arasında space-bunny'de **güvenilir fark ölçülemedi** (n=3: off 2052, medium 571, high 642 karakter). Bu model ücretsiz/steadalth; seviye ayarı gönderim güvenilirliği için var, düşünme miktarını garanti etmez.
  - `reasoning_max_tokens` (1024) geriye uyum için duruyor: `reasoning_effort` boşsa o kullanılır. `max_reasoning_budget` (2048) yalnızca *kötü tur* retry'sinde tavanı belirler.
  - Non-stream yolda düşünme `thinking` bloğu olarak döner (önce düşüyordu).
- **`degenerate_retry` varsayılan `false` ve kapalı kalmalı.** Açmak runaway döngü yaratır: 3448 karakterlik normal bir metin turu "bozuk" sayılıp 3 kez tekrar denendi, reasoning 3072 → 6144 → 12288'e tırmanıp tur 4 kat uzadı, tool çağrısı yine üretilmedi ve kota yendi. `doctor.py` bu ayarı açıkken WARN verir.
- **429 davranışı:** Rate-limit cooldown `MAX_RATE_LIMIT_COOLDOWN_S=3600` (günlük limit 3 sn'de yeniden denenmez). `X-RateLimit-Reset` yoksa 60 sn.
- **Graceful degradation:** İçerik **akmaya başladıktan sonra** akış koptuysa istemciye `GRACEFUL_TEXT` ile temiz kapanış yapılır. Hiçbir şey akmadan boş/hatalı tur gelirse sahte başarı **üretilmez**: `_RetryableUpstream` ile zincirdeki sonraki modele geçilir, hepsi başarısızsa tek `event: error` (non-stream: HTTP 502) döner. ASGI hiçbir yolda çökmez.
- **Sessiz fallback görünür:** `/api/diag` → `last.model` **gerçek yanıtlayan** model, `last.requested_model` istemcinin istediği, `models_tried` denenenler. `doctor.py` "silent_fallback", `bench.py` `fallback` bayrağı bunu raporlar. `empty_stream_turns` boş/başarısız tur sayacıdır; `POST /api/diag/reset` sıfırlar.
- **Kasa dayanıklılığı:** `vault.json` okunamır/çözülemezse sessiz boş liste dönülmez; dosya `vault.json.corrupt` olarak yedeklenir, yazma `503` ile durur. Bozuk kasayı silme/üzerine yazma — kullanıcı onayı ister.
- **Anahtar taşıma:** Gateway api key'i `Authorization: Bearer` veya `x-api-key` header'ı ile kabul edilir; `?api_key=` **bilinçli olarak 401** ile reddedilir (URL'ler loglanır). Yalnızca `POST /api/rotate` değiştirir.
- **Yedek zincire ücretli model GİREMEZ — fiyatla, isimle değil.** `_model_is_free()` önce OpenRouter kataloğundaki **gerçek fiyatı** okur (`pricing.prompt == 0 and pricing.completion == 0` → ücretsiz), katalogda yoksa isim kuralına düşer (`:free` / `/free`). `allow_paid_fallbacks: false` (varsayılan) ile zincire ücretli model alınmaz ve bir kez uyarı yazılır. Sebep: 429'da gateway sıradaki modeli **otomatik** çağırır. **Kullanıcının kendi seçtiği aktif model asla engellenmez.** Dikkat: isim kuralı tek başına yanlıştır — `stealth/space-bunny-alpha` `:free` etiketi taşımaz ama fiyatı **0'dır** (ölçüldü); önceki sürüm onu yanlışlıkla ücretli sayıyordu. `/mode/set`, `/provider/set`, `/protocol/set`, `/model/set`, `/keys/add`, `/keys/remove`, `/api/rotate`, `/agent/run`, `/agent/parallel` `check_auth` çağırır (header zorunluluğu tarayıcıdan gelen sürpriz POST'ları da engeller). Dashboard `jfetch` anahtarı **bellekte** tutar, `localStorage`'a yazmaz; 401'de bir kez tazeleyip tekrar dener. Okuma uçları (`/api/hello`, `/api/*`, `/v1/models`, `GET /keys`) bilinçli olarak auth'suz; `/api/conn` anahtarı döndürdüğü için bootstrap'ta korunamaz.

- **Reasoning ölçümü:** `GET /api/diag` → `last.reasoning_tokens` sağlayıcının modelin **gerçekten** düşünüp düşünmediğini söyler. Ölçüldü: `stealth/space-bunny-alpha` → **0** (reasoning modeli **değil**; birkaç yüz karakterlik refleks çıktı üretiyor), `qwen/qwen3.8-27b:free` → 95–158, `openrouter/free` → 208–360. Yani "thinking çok az" belirtisi **modelden** gelir, gateway'den değil. Reasoning açıkken upstream'e `include_reasoning: true` de gönderilir (düşünme 339 → 492 ölçüldü).
- **Yönetim uçları auth ister:** `/mode/set`, `/provider/set`, `/protocol/set`, `/model/set`, `/keys/add`, `/keys/remove`, `/api/rotate`, `/agent/run`, `/agent/parallel` `check_auth` çağırır (header zorunluluğu tarayıcıdan gelen sürpriz POST'ları da engeller). Dashboard `jfetch` anahtarı **bellekte** tutar, `localStorage`'a yazmaz; 401'de bir kez tazeleyip tekrar dener. Okuma uçları (`/api/hello`, `/api/*`, `/v1/models`, `GET /keys`) bilinçli olarak auth'suz; `/api/conn` anahtarı döndürdüğü için bootstrap'ta korunamaz.

## 4) Sık Hatalar

| Belirti | Neden | Çözüm |
|---|---|---|
| Terminal kapanınca gateway düşüyor | `uvicorn` doğrudan başlatılmış, süreç çağıran kabuğa bağlı | `py -3 launcher.py` (veya `start-detached.bat`) kullan |
| `logs\gateway.log` yok | Supervisor ile başlatılmamış | `py -3 launcher.py`; log yoksa `doctor.py` "log sagligi" WARN verir |
| Port dolu, gateway açılmıyor | Başka bir süreç 8756'da | `.\stop-gateway.bat`; sonra `netstat -ano \| findstr 8756` ile kalan PID'e bak |
| OpenCode hiç cevap almıyor, dashboard çalışıyor | `count_tokens` 404 → SDK tüm isteği hata sayıyor | Yeni `gateway.py` gerekiyor; `py -3 doctor.py` uçları doğrular |
| Union/API `429 daily limit`, dashboard `selam` çalışıyor | Aktif mod ile istemcinin istediği model farklı provider'a gitmiş | `_should_route_atria` model adına bakar; `active_mode` + `provider` logla, `GET /api/providers` ile teyit et |
| Boş veya kırpılmış cevap | Bütçe reasoning'e gitti, tool_call üretilmedi | `GET /api/diag` → `truncated_turns` / `empty_turns` / `reasoning_sent`; `max_tokens` bütçesini yükselt |
| OpenCode'da model seçerken thinking seçeneği çıkmıyor | `~/.config/opencode/opencode.json` içindeki modellerde `variants` alanı yok | `py -3 sync_opencode.py` yeniden çalıştır (her modele `off/low/medium/high` variants yazar) |
| Turn çok uzun sürüyor, cevap boş | `degenerate_retry` açık, reasoning 12288'e tırmandı | `degenerate_retry: false` yap (doctor.py zaten uyarıyor) |
| `space-bunny-alpha: 404` | Eski `ox-alpha` / `union-alpha` referansı kalmış | Sadece `stealth/space-bunny-alpha` |
| `opencode.json` içinde model yok | `sync_opencode.py` çalıştırılmamış | §1.D |
| `sync_opencode.py` "gateway'e baglanilamadi" | Gateway ayakta değil | §1.B (`py -3 launcher.py`) |
| Tüm key'ler aynı anda denendi | `_available()` cooldown doluysa hepsini aday yapar | Beklenen davranış; `rest_seconds`/`cooldown_every` ayarla |
| `pytest` yok hatası | Regresyon paketi pytest değil stdlib unittest kullanıyor | `py -3 -m unittest discover -s tests -v` |
| Regresyon paketi kırmızı çalışıyor | Kod kaynaklı gerçek hata, test hatası değil | Testi değiştirip kapatma; `gateway.py`'yi düzelt. Şu an 189/189 yeşil. |
| Upstream hata istemciye başarı görünüyor | `_anthropic_stop("error")` `end_turn` dönüyordu; `sse_anthropic` ve non-stream yolu sahte başarılı tur kuruyordu | `finish_reason=error` artık `_anthropic_stop` → `"error"` ve akış `event: error` / HTTP 502 veriyor |

## 5) Güvenlik

- `config.json`, `vault.json`, `secret.key`, `*token*`, `*key*.json`, `.env*` → **okuma, loglama, commit yok**. `config.json`'ı sen sadece **yokluğunu** kontrol edersin; içeriğini basma. Sadece `sync_opencode.py`'nin hedeflediği `~/.config/opencode/opencode.json` düzenlenir (o da git'e girmez).
- Key'ler sadece Fernet kasasında düz metin tutulmaz; `*_out.txt`, `*_err.txt`, `logs/` zaten ignore'lu.
- `config.example.json` yalnızca placeholder tutar; gerçek görünümlü anahtar yazma. `gateway_api_key` boş bırakılır (otomatik üretilir).
- `doctor.py` ve `bench.py` sırları okumaz: `vault.json`/`secret.key` açılmaz, key havuzu yalnızca sayı olarak raporlanır, gateway'in maskelenmiş `key` alanı kullanılır.
- `git push` ancak kullanıcı açıkça isterse; push öncesi `git status` ile `config.json`'un stage edilmediğini doğrula.

---
*Agent notu: Model listesi kaynağı her zaman `GET /api/conn` → `provider_models`; README'deki listeyi kopyalayıp hardcode'lama.*
