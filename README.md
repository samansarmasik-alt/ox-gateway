# ox-gateway

Birden fazla API key'ini tek bir local proxy'de birleştirir. İstekler tek noktadan
(`http://127.0.0.1:8756`) gelir, key'lere round-robin dağıtılır, hata veya 429'da
otomatik olarak diğer key'e geçilir.

OpenAI ve Anthropic protokollerini aynı anda konuşur: Claude Code, OpenCode, OpenAI SDK
veya herhangi bir uyumlu istemci doğrudan bağlanabilir.

| Mod | Provider | Upstream | Protokol |
|---|---|---|---|
| `1` | Atria | `https://api.atria-asi.ai/v1/messages` | Anthropic |
| `2` | OpenRouter | `https://openrouter.ai/api/v1/chat/completions` | OpenAI |

Her modun key havuzu ayrıdır ve şifrelenerek saklanır.

## Gereksinimler

- Python 3.10+ (`gateway.py` çalışma zamanında `X | None` tip sözdizimi kullanıyor).
  `start-detached.bat` yalnızca 3.11 / 3.12 / 3.13 `pythonw.exe` yollarını arar.
- `pip install -r requirements.txt` → fastapi, uvicorn, httpx, cryptography

## Kurulum

```powershell
pip install -r requirements.txt
Copy-Item config.example.json config.json
```

`config.json` yoksa gateway kendisi çalışmaz (`doctor.py` bu durumu FAIL olarak raporlar).
Kopyaladıktan sonra `gateway_api_key` boş bırakılmıştır: gateway ilk açılışta
`ox-<rastgele>` üretip kalıcı yazar. Elle bir anahtar yazarsanız o anahtar geçerli olur.

## Başlatma ve durdurma

Süreç yönetimi `launcher.py` + `supervisor.py` ile yapılır. Doğrudan
`uvicorn` çalıştırmak desteklenmez: süreç çağıran kabuğun (agent terminali, CI,
PowerShell) süreç ağacında kaldığı için terminal kapanınca gateway de kapanır.

```
start-detached.bat  ->  launcher.py  ->  supervisor.py (pythonw.exe)  ->  uvicorn
        (konsol)         (ayirir)          (yok pencere, log + pid)      (asil is)
```

| Dosya | Ne yapar |
|---|---|
| `start-detached.bat` | Konsol görünmez. Port doluysa hiçbir şey yapmadan çıkar. `launcher.py`'yi çağırır, port 8756 dinlenene kadar en fazla 15 sn bekler, sonra pencereyi kendi kapatır. |
| `launcher.py` | Supervisor'ı `DETACHED_PROCESS` + `CREATE_NEW_PROCESS_GROUP` + `CREATE_BREAKAWAY_FROM_JOB` ile başlatır. Kısa ömürlüdür, görevi bittiğinde çıkar. Konsol açmaz, stdout/stderr tamamen kapatılır. |
| `supervisor.py` | `pythonw.exe` altında, görünmez. `logs\gateway.log` yazar, PID'sini `logs\gateway.pid` içine koyar, uvicorn'u çocuk olarak doğurur. uvicorn çökerse ustel geri çekilme (1/2/3/5/8/15 sn) ile yeniden başlatır. 8 kez 20 sn'den kısa çökerse döngüye girmemesi için kendini kapatır. `logs\gateway.stop` dosyasını görürce temiz kapanır. |
| `start.bat` | Ön plan varyantı: `uvicorn` doğrudan bu pencerede çalışır, pencere açık kalmalıdır. `pause` yoktur ve port doluysa beklemeden çıkar. |
| `stop-gateway.bat` | `py -3 supervisor.py --stop` çağırır: önce supervisor'ı, sonra portun sahibi süreci ağacıyla öldürür, pid/stop dosyalarını siler. |

```powershell
.\start-detached.bat          # arka planda başlat (önerilen)
.\stop-gateway.bat            # durdur
py -3 launcher.py             # .bat olmadan başlatmanın equivalenti
py -3 supervisor.py --stop    # .bat olmadan durdurmanın equivalenti
.\start.bat                   # ön planda başlat, logu ekranda izle
```

Sağlık kontrolü (anahtar istemez):

```powershell
Invoke-RestMethod http://127.0.0.1:8756/api/hello
```

## Yapılandırma (config.json)

Tüm anahtarlar `gateway.py` içinde `CONFIG.get(...)` ile okunur. Güvenli varsayılanlar:

| Anahtar | Varsayılan | Anlamı / tehlikeli değeri |
|---|---|---|
| `model` | `Atria-Dawn-Preview` | `provider_models` boşsa düşülen isim. Dashboard'dan değişir. |
| `active_mode` | `"1"` | `1` = Atria, `2` = OpenRouter. Yalnızca `POST /mode/set` ile değişir. |
| `provider_models` | `{"1": "Atria-Dawn-Preview", "2": "stealth/space-bunny-alpha"}` | Mod başına model. Tek kaynak budur; kodu değiştirme. |
| `protocol` | `"openai"` | `openai` \| `anthropic`. Yalnızca dashboard göstergesi; yönlendirmeyi belirlemez. |
| `gateway_api_key` | boş → ilk açılışta üretilir | `/v1/*` isteklerinin anahtarı. Sadece `POST /api/rotate` ile değişir. |
| `api_keys` | `[]` | Eski düz liste. İlk açılışta Mod 2'ye taşınır ve silinmez; yeni key'ler buraya yazılmaz. |
| `pace_ms` | `100` | Upstream çağrıları arası bekleme. Bekleme hiçbir zaman bu değeri geçmez. |
| `first_token_ms` | `20000` | Bu sürede ilk token gelmezse key cooldown'a girer, failover olur. |
| `stall_timeout` | `45` | Stream ortasında bu kadar saniye veri gelmezse akış kapatılır. |
| `request_timeout` | `120` | Okuma timeout'u (sn). Connect 10 sn, write 30 sn sabit. |
| `cooldown_seconds` | `3` | Hatalı key'nin bekleme süresi (tavanı 3 sn). |
| `cooldown_every` | `3` | Her key bu kadar istekten sonra dinlenmeye girir. |
| `rest_seconds` | `3` | Dinlenme süresi (tavanı 3 sn). |
| `max_retries` | `3` | Bir turda denenecek farklı key sayısı. |
| `heal_retries` | `2` | Key zinciri tükenince tüm zincir kaç tur daha denenir. |
| `graceful_degradation` | `true` | Hiçbir şey yanıt vermezse istemciye hata yerine geçerli cevap + `GRACEFUL_TEXT` döner. `false` ise 502 döner. |
| `min_token_budget` | `1024` | Streaming'de token bütçesi tabanı. Altına inen istekler bu değere yükseltilir. |
| `max_token_budget` | `32768` | Kırpılma (truncation) retry'sinde tavan. |
| `token_budget_step` | `2048` | Retry'de bütçe artış adımı (en az 512). |
| `token_budget_tries` | `3` | Kırpılma/kötü tur için ek deneme sayısı. |
| `reasoning_max_tokens` | `1024` | Gateway'in kendi reasoning bütçesi. `0` reasoning'i kapatır. İstemci kendi `thinking.budget_tokens` gönderdiyse o kazanır. |
| `max_reasoning_budget` | `2048` | Kötü tur retry'sinde reasoning bütçesinin tavanı. |
| `degenerate_retry` | `false` | **Varsayılan kapalı, kapalı kalmalı.** Açmak runaway reasoning'e yol açar (aşağıya bak). |
| `degenerate_token_floor` | `64` | Yalnızca `degenerate_retry` açıkken: bu altındaki `completion_tokens` "kötü tur" sayılır. |
| `degenerate_char_ceiling` | `400` | Yalnızca `degenerate_retry` açıkken: bu altındaki karakter sayısı "kötü tur" sayılır. |
| `auto_model_fallback` | `true` | 429'da `fallback_models` + ücretsiz modellere geç. Yalnızca Mod 2'de etkin. |
| `max_model_fallbacks` | `4` | En fazla kaç yedek model denenir. |
| `fallback_models` | 3 adet `:free` model | Yedek zinciri. Ayrılmış gerçek bir model adı yazmayın; kaba tahminle bile ücretsiz olanları seçin. |

### Token bütçesi tavanı (kod içinde sabit)

`_MAX_OUTPUT_TOKENS = 32000` (`gateway.py`). İstemcinin istediği `max_tokens`
her OpenAI ve Anthropic isteğinde `_clamp_max_tokens` ile buraya indirilir; `0`,
negatif veya sayı olmayan değerler `1024` olur.

Sebep: OpenCode'un kendi config'i 131072 / 128000 çıktı token'ı istiyor. Sağlayıcı
ya bu isteği reddediyor ya da bütçeyi tamamen reasoning'e harcayıp boş/kırpılmış
cevap döndürüyor. Aynı değer `bench.py` tarafından bilerek 131072 gönderilir; her
ölçümde tavanın çalıştığı görülür.

### Kötü tur ve runaway reasoning

`degenerate_retry` kapalıyken gateway "araç sunulmuş ama neredeyse boş tur" tespitini
hiç yapmaz. Açıldığında model 3448 karakterlik normal bir metin turu ürettiği hâlde
gateway bunu bozuk sayıp 3 kez tekrar deniyor, reasoning bütçesini 3072 → 6144 →
12288'e çıkarıyor, turu dört kat uzatıyor, kotayı yiyor ve tool çağrısı yine
üretilmiyordu. Bu bir regresyondur, bu yüzden varsayılan `false`'tir.
`max_reasoning_budget` bu tür tırmanmanın tavanını sınırlar.

## Endpoint'ler

| Yol | Anahtar | Açıklama |
|---|---|---|
| `GET`/`HEAD` `/api/hello` | yok | Sağlık ucu. `{"ok": true, "service", "mode"}`. |
| `GET` `/` | yok | Dashboard (`static/index.html`). |
| `POST /v1/chat/completions` | gerekli | OpenAI uyumlu; `stream: true` destekler. |
| `GET /v1/models` | gerekli | OpenRouter model listesi, default en üstte. |
| `POST /v1/messages` · `POST /messages` | gerekli | Anthropic Messages API; stream + `tool_use`. |
| `POST /v1/messages/count_tokens` · `POST /messages/count_tokens` | gerekli | Anthropic token sayımı (aşağıya bak). |
| `POST /agent/run` · `/agent/parallel` | yok | Sub-agent çalıştırıcı; `agents.py` bunu kullanır. |
| `GET /api/stats` · `/api/conn` · `/api/models` · `/api/providers` · `/api/diag` · `/keys` | yok | Dashboard ve teşhis verisi. |
| `POST /mode/set` · `/provider/set` | yok | `{"mode": "1"\|"2"}`; kalıcı `config.json` + `provider_models` senkronlanır. |
| `POST /protocol/set` | yok | `{"protocol": "openai"\|"anthropic"}`. |
| `POST /model/set` | yok | `{"model": "...", "mode": "1"\|"2"}`; mod bazında kalıcı. |
| `POST /keys/add` · `/keys/remove` | yok | `{"key": "sk-or-v1-...","mode": "1"\|"2"}`; `mode` yoksa aktif mod. Fernet kasasına yazar. |
| `POST /api/rotate` | yok | Gateway api key'ini yeniler. |
| `POST /api/diag/reset` | yok | Teşhis sayaçlarını sıfırlar. |

Anahtar yalnızca header'da kabul edilir: `Authorization: Bearer <key>` veya
`x-api-key: <key>`. `?api_key=` query parametresi bilinçli olarak 401 ile reddedilir
(URL'ler loglanır).

### `count_tokens` neden var ve neden upstream'e gitmiyor

OpenCode'un kullandığı Anthropic SDK, her istekten önce `POST /v1/messages/count_tokens`
çağırır. Bu uç yokken 404 dönüyor, SDK tüm isteği hataya çeviriyor ve istemci hiç cevap
almıyordu; dashboard çalıştığı için sorun görünmüyordu.

Gateway bu sayımı **yerel tahminle** yapar, upstream'e istek atmaz: hem yavaş hem de
kotada maliyetli olurdu, amaç yalnızca SDK'nın "şu ana kadar kaç token kullandım"
göstergesini doldurmaktır. ASCII için ~4 karakter/token, unicode ağırlıklı ~2.5.
Görseller sabit **~1600 token** sayılır (Claude Code görsel eklediği için önemli).
Sapma yüzde birkaç düzeyindedir ve bir hata değildir.

### `/api/hello` neden var

İstemciler (OpenCode dahil) sağlık kontrolünü `HEAD /api/hello` ile yapıyordu. Uç
olmadığı için 404 alıyor ve gateway "ulaşılamaz" sanılıyordu. `GET`/`HEAD` ikisini de
kabul eder ve anahtar istemez.

## İstemci olarak kullanma

### OpenAI uyumlu

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8756/v1",
    api_key="<dashboard'daki gateway api key>",
)
resp = client.chat.completions.create(
    model="Atria-Dawn-Preview",
    messages=[{"role": "user", "content": "selam"}],
    stream=True,  # opsiyonel
)
```

### Anthropic uyumlu (Claude Code)

```python
from anthropic import Anthropic

client = Anthropic(base_url="http://127.0.0.1:8756", api_key="<gateway api key>")
msg = client.messages.create(model="Atria-Dawn-Preview", max_tokens=1024,
                             messages=[{"role": "user", "content": "selam"}])
```

### Dashboard

Tarayıcıdan `http://127.0.0.1:8756` aç. Bölümler: proxy bağlantısı (protokol, base URL,
anahtar kopyala, rotate), model seçici, key havuzu (durum, istek sayısı, yanıt süresi,
cooldown, sil), canlı chat testi, paralel sub-agent çalıştırıcı.

### OpenCode'a otomatik sync

```powershell
py -3 sync_opencode.py               # ~/.config/opencode/opencode.json'a ekle/güncelle
py -3 sync_opencode.py --dry-run     # ne yapılacağını göster, yazma
py -3 sync_opencode.py --reset       # ox provider'ını boşaltıp sadece aktif modelleri yazar
```

`GET /api/conn` → aktif `provider_models` + `config.json` free fallback'larını okur,
`provider.ox` altına `baseURL` + `apiKey` + her modeli `tool_call: true` ile yazar,
mevcut dosyayı `.json.bak-<tarih>` olarak yedekler. Eski modelleri silmez;
`--reset` sadece `ox` provider'ını temizler. Hedef dosya `OPENCODE_CONFIG` ortam
değişkeniyle değiştirilebilir.

### Claude Code

`%USERPROFILE%\.claude\settings.json` içindeki `env` altında `ANTHROPIC_BASE_URL`
değerini `http://127.0.0.1:8756` yapın ve model değişkenlerini (`ANTHROPIC_MODEL`,
`ANTHROPIC_SMALL_FAST_MODEL`, `CLAUDE_CODE_SUBAGENT_MODEL` vb.) aktif gateway modeliyle
eşleyin. Değiştirmeden önce `.bak-<tarih>` alın.

## Teşhis araçları

### `doctor.py` — tek komutlu kendi kendine teşhis

```powershell
py -3 doctor.py                # tam rapor
py -3 doctor.py --quiet        # sadece verdict + FAIL'ler
py -3 doctor.py --json         # makine-okur çıktı
py -3 doctor.py --no-network   # hiçbir ağ çağrısı yapmadan
py -3 doctor.py --timeout 10   # ağ timeout'u (varsayılan 5 sn)
```

Kontrol edilenler: bağımlılıklar, `config.json`, port + `/api/hello`, port sahibinin
gerçekten bizim sürecimiz olup olmadığı, supervisor canlılığı, API uçları, key havuzu,
free yedek zinciri, `opencode.json` (`ox`), Claude settings, reasoning ayarları,
`logs\gateway.log` sağlığı. Her FAIL/WARN satırı ne yapılacağını da söyler.
Çıkış kodu: `0` = OK veya sadece WARN, `1` = en az bir FAIL (CI/preflight için).

Hiçbir sırrı okumaz: `vault.json` ve `secret.key` açılmaz, key havuzu yalnızca sayı
olarak raporlanır.

### `bench.py` — gecikme ve güvenilirlik ölçümü

```powershell
py -3 bench.py                                   # 5 istek, eş zamanlılık 3
py -3 bench.py --n 20 --concurrency 5             # daha geniş ölçüm
py -3 bench.py --mode 2 --n 10                    # belirli mod (modu DEĞİŞTİRMEZ)
py -3 bench.py --json                            # makine-okur çıktı
py -3 bench.py --out logs\bench.jsonl            # JSON satırı ekle (trend biriktirme)
py -3 bench.py --no-tools                        # araçsız düz metin ölçümü
```

Streamed SSE üzerinden `POST /v1/messages` çağırır (gerçek istemcilerin gördüğü yol).
Ölçtüğü: ilk bayt, ilk gerçek içerik, toplam süre, sonuç sınıflandırması, token
sayımı, key/model dağılımı. Gateway'i değiştirmez ve `/mode/set` çağırmaz; yönlendirme
model adına baktığı için `--mode` yalnızca hangi modelin ölçüleceğini belirler.
Anahtar yalnızca çalışma anında `GET /api/conn`'dan alınır ve bellekte tutulur.

## Testler

Regresyon paketi stdlib `unittest` ile çalışır, pytest gerekmez:

```powershell
py -3 -m unittest discover -s tests -v
```

| Dosya | Neyi koruyor |
|---|---|
| `tests/test_tokens.py` | `_clamp_max_tokens` (32000 tavanı, `<=0`/çöp değer → 1024), `_estimate_tokens`, `_count_request_tokens` (görsel ~1600, upstream çağrısı yok), reasoning bütçesi ve payload varyantları. |
| `tests/test_protocol.py` | Anthropic ↔ OpenAI dönüşümü: system, `tool_use` → `tool_calls`, `tool_result` → `role: tool`, çok turlu araç geçmişinin atılmaması, görseller, `tools`/`input_schema`, `tool_choice`, `stop_reason` ↔ `finish_reason` eşlemesi (`error` başarılı tur sayılmaz). |
| `tests/test_routes.py` | Uçlar süreç içi ASGI ile: `/api/hello`, `/api/conn`, `/api/stats`, `/api/diag`, `count_tokens`, yetkilendirme (401), mod/protokol rotaları. Ayrıca çalışan gateway'e karşı canlı testler — gateway kapalıysa otomatik atlanır. |
| `tests/test_streaming.py` | SSE olay biçimi, unicode gidiş-dönüşü, graceful kapanış olaylarının geçerliliği, `/api/diag` sayaçları, boş stream ve içerik birleştirme. |

`gateway.py` testler boyunca salt okunur: durum değişiklikleri mock/guard ile yapılır,
disk ve vault'a dokunulmaz.

Paket 141 testten 141 yeşil. İki test daha önce kırmızıydı ve ikisi de gerçek
`gateway.py` hatasını avıyordu; ikisi de düzeltildi:

- `test_image_only_message_sent_once` — görsel içeren tek mesaj upstream'e iki kez
  gönderiliyordu, yani her görsel iki kez ücretleniyordu.
- `test_anthropic_stop_error_is_not_successful_turn` — `_anthropic_stop("error")`
  `end_turn` döndürüyordu, yani upstream hata bildirdiğinde istemci başarılı bir
  tur görüyordu. Artık `"error"` döner ve akış `event: error`, non-stream yolu
  HTTP 502 verir.

Elle çalıştırılan, canlı gateway ve key gerektiren betikler (regresyon paketinin
yerine geçmez, sadece uçtan uca duman testidir):

```powershell
py -3 test_stream.py         # OpenAI SSE akışı
py -3 test_anthropic.py      # Anthropic protokol + thinking
py -3 test_tools.py          # tool_use blokları
py -3 test_multi_tool.py     # çok turlu araç kullanımı
py -3 test_claude_code.py    # Claude Code akış simülasyonu
```

## Güvenlik

- Key'ler Fernet ile şifrelenir: `%APPDATA%\ox-gateway\vault.json` (anahtar
  `%APPDATA%\ox-gateway\secret.key`, ilk açılışta üretilir).
- Kasa okunamaz/çözülemezse sessizce boş liste dönülmez: dosya
  `vault.json.corrupt` olarak yedeklenir ve yazma `503` ile durdurulur, key'ler
  kaybolmaz. Eski `secret.key`'i geri koyup yeniden deneyin.
- `config.json`, `vault.json`, `secret.key`, `*_out.txt`, `*_err.txt` ve `logs/`
  `.gitignore`'dadır.
- Gateway yalnızca `127.0.0.1:8756` üzerinde dinler; anahtar koruması `/v1/*` ve
  `/messages*` yollarındadır, yönetim uçları localhost'a açıktır.

## Sık karşılaşılan sorunlar

| Belirti | Neden / çözüm |
|---|---|
| Terminal kapanınca gateway düşüyor | `uvicorn`'i doğrudan çalıştırmışsın. `start-detached.bat` veya `py -3 launcher.py` kullan. |
| `/api/hello` 404 | Çalışan süreç eski `gateway.py`. `stop-gateway.bat` + `start-detached.bat` ile yenile. |
| OpenCode hiç cevap almıyor, dashboard çalışıyor | `count_tokens` 404 dönüyor. Yeni `gateway.py` gerekiyor. |
| `429 daily limit`, dashboard `selam` çalışıyor | İstemcinin istediği model farklı provider'a gitmiş. `_should_route_atria` model adına bakar; `GET /api/providers` ile teyit et. |
| `space-bunny-alpha: 404` | Eski `ox-alpha` / `union-alpha` referansı. Doğrusu `stealth/space-bunny-alpha`. |
| Boş/kırpılmış cevap | Bütçe reasoning'e gitti. `GET /api/diag` → `truncated_turns` / `empty_turns` sayacına bak. |
| Tüm key'ler aynı anda denendi | `_available()` cooldown doluysa hepsini aday yapar. Beklenen davranış; `rest_seconds` / `cooldown_every` ayarla. |
| Log dosyası yok | Supervisor ile başlatılmamış. `py -3 launcher.py`. |

## Dosya haritası

| Dosya | Ne |
|---|---|
| `gateway.py` | FastAPI proxy; tüm HTTP uçları ve anahtar havuzu mantığı. |
| `launcher.py` | Supervisor'ı ayrı süreç grubunda başlatır. |
| `supervisor.py` | Görünmez süreç yöneticisi: log, pid, yeniden başlatma, `--stop`. |
| `start-detached.bat` · `start.bat` · `stop-gateway.bat` | Arka plan / ön plan başlatma ve durdurma giriş noktaları. |
| `sync_opencode.py` | OpenCode `opencode.json` senkronizasyonu. |
| `doctor.py` · `bench.py` | Teşhis ve ölçüm araçları. |
| `agents.py` | Gateway client: `SubAgent`, `run_parallel`, `chat`. |
| `static/index.html` | Dashboard. |
| `config.example.json` | Commit'lenen config şablonu. |
| `tests/` | stdlib unittest regresyon paketi. |
