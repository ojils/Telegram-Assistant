# COWOK AI — Railway-ready foundation

This project implements the core architecture discussed:
- Telegram bot as Control Panel
- Telegram user account connection using QR login (no OTP/password is collected by the bot)
- Encrypted-at-rest Telethon session blobs
- Owner/Admin/User roles
- Mandatory join checking
- AI chat through connected user accounts
- OpenAI Responses API with web search
- Image generation command
- PDF/text document extraction
- Per-account memory
- Scheduler
- Activity logging
- Emergency stop
- Poster file_id support
- Back/Kembali navigation

## Railway variables
Set:
BOT_TOKEN, API_ID, API_HASH, OPENAI_API_KEY, OWNER_ID.
Optional:
REQUIRED_CHAT_IDS=-100123,-100456
AI_MODEL=gpt-5.4
POSTER_FILE_ID=<telegram file_id>
SESSION_ENCRYPTION_KEY=<32-byte urlsafe base64 key>

Generate a key locally with:
python -c "import secrets,base64; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"

API_ID/API_HASH come from Telegram's official API development tools. The QR flow lets the account owner authorize the user account directly in Telegram.

## Run
pip install -r requirements.txt
python main.py

Railway start command:
python main.py

Important:
- This is a production-oriented foundation, not a claim that every Telegram feature is universally available.
- Telegram permissions/API limits still apply.
- Do not commit .env or session material.
- Keep the service on a persistent Railway volume if you want local SQLite/session files to survive redeploys; otherwise use PostgreSQL/object storage for production scale.
