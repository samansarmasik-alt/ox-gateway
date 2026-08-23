# ⚡ ox-gateway

**Birden fazla OpenRouter API key'ini tek bir local API proxy'de birleştirir.**
Tüm istekler tek noktadan gelir, key'lere eşit dağıtılır; hata olursa otomatik failover yapar.

OpenAI **ve** Anthropic protokollerini aynı anda konuşur — Claude Code, OpenAI SDK,
herhangi bir uyumlu istemci doğrudan bağlanabilir.

---

## ✨ Özellikler

- 🔀 **Akıllı key havuzu:** Round-robin dağıtım + otomatik failover (401/429'da diğer key'e geçer)
- 🔄 **3 istekte bir dinlenme:** Her key N istekten sonra (`cooldown_every`) kısa cooldown'a girer, yük diğerlerine döner
- 🔐 **Şifreli key saklama:** Key'ler Fernet ile şifrelenip `%APPDATA%\ox-gateway` altında tutulur — repo'da asla görünmez
- 🌊 **Streaming:** OpenAI SSE ve Anthropic event akışı tam destekli
- 🧠 **Claude Code uyumlu:** `thinking` yönetimi, `tool_use` blokları, `tool_result` desteği — agentic araçlar düzgün çalışır
- 🤖 **Model seçici:** OpenRouter'ın tüm modelleri otomatik çekilir; varsayılan en üstte, ücretsizler 🆓 işaretli
- 📊 **Web dashboard:** Key durumları, istek sayıları, yanıt süreleri (ms), canlı chat testi, paralel sub-agent çalıştırıcı
- ⏱️ **Koruma katmanları:** İlk-token timeout, stream stall guard, pacing (max 100ms), connect timeout

## 🚀 Hızlı Başlangıç

```bash
pip install -r requirements.txt
copy config.example.json config.json   # Windows
start.bat                              # sunucuyu başlatır (port 8756)
```

Tarayıcıdan **http://127.0.0.1:8756** aç → key'lerini ekle → kullan.

> İlk açılışta gateway API key otomatik oluşturulur; dashboard'dan kopyalarsın.

## 🔌 Proxy olarak kullanma

### OpenAI uyumlu
```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8756/v1",
    api_key="<dashboard'daki gateway api key>",
)
resp = client.chat.completions.create(
    model="ox-alpha",
    messages=[{"role": "user", "content": "selam"}],
    stream=True,  # opsiyonel
)
```

### Anthropic uyumlu (Claude Code vb.)
```bash
ANTHROPIC_BASE_URL=http://127.0.0.1:8756
ANTHROPIC_API_KEY=<gateway api key>
```
```python
from anthropic import Anthropic

client = Anthropic(base_url="http://127.0.0.1:8756", api_key="<gateway api key>")
msg = client.messages.create(model="ox-alpha", max_tokens=1024,
                             messages=[{"role": "user", "content": "selam"}])
```

Dashboard'daki **🟢 OpenAI / 🧠 Anthropic** düğmeleriyle aktif protokolü tek tuşla değiştir.

## 🖥️ Dashboard

| Bölüm | Ne yapar |
|---|---|
| 🔌 Proxy Bağlantısı | Protokol seçimi (tek tuş), base URL + api key kopyala, rotate |
| Model | OpenRouter modelleri otomatik; default ⭐ en üstte, free 🆓 sonra |
| 🔑 Key Havuzu | Durum, istek sayısı, ✓/✗, son & ort. yanıt süresi (ms), cooldown, sil |
| 💬 Chat Testi | Seçili protokol üzerinden canlı streaming test |
| 🤖 Paralel Agent | Her satır `rol \| görev` → N sub-agent paralel çalışır |

## 📡 Endpoint'ler

| Yol | Açıklama |
|---|---|
| `POST /v1/chat/completions` | OpenAI uyumlu (stream destekli) |
| `GET /v1/models` | OpenAI uyumlu model listesi |
| `POST /v1/messages` · `POST /messages` | Anthropic Messages API (stream + tool_use) |
| `POST /agent/run` · `/agent/parallel` | Sub-agent çalıştırıcı |
| `GET /api/stats` · `/api/conn` · `/api/models` | Dashboard verileri |
| `POST /keys/add` · `/keys/remove` | Canlı key yönetimi (kalıcı) |
| `POST /model/set` · `/protocol/set` · `/api/rotate` | Model / protokol / api key yenileme |

> `/v1/*` endpoint'leri gateway api key ister; dashboard ve yönetim endpoint'leri localhost'ta açıktır.

## ⚙️ Ayarlar (config.json)

| Anahtar | Varsayılan | Açıklama |
|---|---|---|
| `model` | `ox-alpha` | Varsayılan model (dashboard'dan değişir) |
| `pace_ms` | 100 | İstekler arası bekleme — **en fazla bu kadar** |
| `first_token_ms` | 20000 | İlk token gelmezse key cooldown'a girer, failover |
| `stall_timeout` | 45 | Stream ortasında ver kesilirse akış kapatılır |
| `request_timeout` | 120 | Okuma timeout'u (sn); connect 10 sn |
| `cooldown_seconds` | 30 | Hatalı key bekleme süresi |
| `cooldown_every` | 3 | Her key bu kadar istekte bir dinlenmeye girer |
| `rest_seconds` | 8 | Dinlenme süresi |
| `max_retries` | 3 | Failover'da denenecek farklı key sayısı |

## 🔐 Güvenlik

- OpenRouter key'ler **Fernet ile şifrelenir** → `%APPDATA%\ox-gateway\vault.json`
- Şifreleme anahtarı: `%APPDATA%\ox-gateway\secret.key` (otomatik üretilir)
- `config.json` ve kasa dosyaları `.gitignore`'da — repo'ya asla girmez
- Eski konumdaki kasa ilk çalıştırmada otomatik taşınır

## 🧪 Testler

```bash
py -3 test_stream.py      # OpenAI SSE akışı
py -3 test_anthropic.py   # Anthropic protokol + thinking
py -3 test_tools.py       # tool_use blokları
py -3 test_claude_code.py # Claude Code akış simülasyonu
```

## 📋 Gereksinimler

- Python 3.11+
- fastapi, uvicorn, httpx, cryptography, pydantic (`requirements.txt`)

---

*ox-alpha ve OpenRouter topluluğu için ❤️ ile yapıldı.*
