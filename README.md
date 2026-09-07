# COWOK AI

Telegram AI Assistant untuk deployment Railway.

## Isi
- Bot AI chat langsung di private chat dan mention di grup
- OpenAI Responses API + web search dengan retry/fallback model
- Image generation
- Analisis PDF/TXT
- Telegram user account via QR (Telethon)
- Memory dan settings
- Mandatory Join
- Poster / Branding
- Owner/Admin panel, logs, emergency stop
- Contact Admin dengan username otomatis atau fallback Telegram ID
- Tombol inline berwarna (primary/success/danger)

## Railway Variables
Wajib:
- `BOT_TOKEN`
- `API_ID`
- `API_HASH`
- `OPENAI_API_KEY`
- `OWNER_ID`

Disarankan:
- `AI_MODEL=gpt-5`
- `AI_FALLBACK_MODELS=gpt-5-mini,gpt-4.1-mini`
- `SESSION_ENCRYPTION_KEY` (32-byte base64/url-safe secret)
- `ADMIN_CONTACT_URL` (opsional; default `tg://user?id=OWNER_ID`)

Opsional:
- `REQUIRED_CHAT_IDS`
- `POSTER_FILE_ID`

## Deploy
Upload isi folder ini ke root repository GitHub agar `main.py` dan `Procfile` berada di root. Railway menjalankan:

`worker: python main.py`

Setelah deploy, buka bot dan `/start`.

## Jika AI gagal
Bot tidak lagi menelan error. Error API dicatat ke log dan Owner menerima notifikasi diagnostik. Periksa `OPENAI_API_KEY`, model yang dipilih, akses/billing API, dan log Railway.

Jangan pernah mengirim API key, API hash, OTP, password 2FA, atau session Telegram ke orang lain.
