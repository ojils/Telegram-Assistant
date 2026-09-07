import os, io, re, json, base64, asyncio, logging, secrets, hashlib
from datetime import datetime, timezone, timedelta
from pathlib import Path

import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from aiogram.exceptions import TelegramBadRequest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from openai import AsyncOpenAI
from pypdf import PdfReader
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.functions.auth import ResetAuthorizationsRequest

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("cowok-ai")

BOT_TOKEN = os.environ["BOT_TOKEN"]
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
OWNER_ID = int(os.environ["OWNER_ID"])
ADMIN_CONTACT_URL = os.getenv("ADMIN_CONTACT_URL", f"tg://user?id={OWNER_ID}")
AI_MODEL = os.getenv("AI_MODEL", "gpt-5")
AI_FALLBACK_MODELS = [x.strip() for x in os.getenv("AI_FALLBACK_MODELS", "gpt-5-mini,gpt-4.1-mini").split(",") if x.strip()]
POSTER_FILE_ID = os.getenv("POSTER_FILE_ID", "")
REQUIRED_CHAT_IDS = [int(x.strip()) for x in os.getenv("REQUIRED_CHAT_IDS", "").split(",") if x.strip()]
DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "cowok_ai.db"

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
oa = AsyncOpenAI(api_key=OPENAI_API_KEY)
scheduler = AsyncIOScheduler(timezone="UTC")
clients = {}
qr_tasks = {}
awaiting_poster = set()
awaiting_join = set()
STOP_ALL = False

def utcnow():
    return datetime.now(timezone.utc).isoformat()

async def db_fetchone(c, sql, params=()):
    cur = await c.execute(sql, params)
    return await cur.fetchone()

async def db_fetchall(c, sql, params=()):
    cur = await c.execute(sql, params)
    return await cur.fetchall()

def enc_key():
    raw = os.getenv("SESSION_ENCRYPTION_KEY")
    if not raw:
        # Development fallback only. Set SESSION_ENCRYPTION_KEY in production.
        return b"dev-only-change-this-key-32bytes!!"
    try:
        return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except Exception:
        return hashlib.sha256(raw.encode()).digest()

KEY = enc_key()
if len(KEY) != 32:
    KEY = hashlib.sha256(KEY).digest()

# Lightweight authenticated encryption using AES-GCM when cryptography is present.
try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except Exception:
    AESGCM = None

def encrypt_blob(data: str) -> str:
    if AESGCM is None:
        raise RuntimeError("cryptography package is required for encrypted sessions")
    nonce = secrets.token_bytes(12)
    ct = AESGCM(KEY).encrypt(nonce, data.encode(), None)
    return base64.urlsafe_b64encode(nonce + ct).decode()

def decrypt_blob(token: str) -> str:
    raw = base64.urlsafe_b64decode(token)
    return AESGCM(KEY).decrypt(raw[:12], raw[12:], None).decode()

def db():
    # Return the aiosqlite connection context manager directly.
    # Do not await connect() here and then use async-with: that starts the
    # same worker thread twice on recent aiosqlite versions.
    return aiosqlite.connect(DB_PATH)

async def init_db():
    async with db() as c:
        await c.executescript("""
        CREATE TABLE IF NOT EXISTS users(
          tg_id INTEGER PRIMARY KEY, role TEXT NOT NULL DEFAULT 'user',
          blocked INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ai_accounts(
          owner_id INTEGER PRIMARY KEY, tg_id INTEGER, name TEXT, username TEXT,
          session_enc TEXT NOT NULL, connected INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL, last_active TEXT
        );
        CREATE TABLE IF NOT EXISTS memory(
          owner_id INTEGER PRIMARY KEY, text TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS logs(
          id INTEGER PRIMARY KEY AUTOINCREMENT, actor_id INTEGER, action TEXT,
          status TEXT, details TEXT, created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS settings(
          owner_id INTEGER PRIMARY KEY, personality TEXT DEFAULT 'helpful',
          mode TEXT DEFAULT 'smart', memory_on INTEGER DEFAULT 1,
          tools_on INTEGER DEFAULT 1, mentions_on INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS schedules(
          id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER, chat_id INTEGER,
          prompt TEXT, run_at TEXT, done INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS branding(
          owner_id INTEGER PRIMARY KEY, poster_file_id TEXT NOT NULL DEFAULT '', updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS join_targets(
          id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL UNIQUE,
          title TEXT NOT NULL DEFAULT '', invite_url TEXT NOT NULL DEFAULT '',
          enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
        );
        """)
        await c.execute("INSERT OR IGNORE INTO branding(owner_id,poster_file_id,updated_at) VALUES(?,?,?)",
                        (OWNER_ID, POSTER_FILE_ID, utcnow()))
        await c.execute("INSERT OR IGNORE INTO users(tg_id,role,created_at) VALUES(?,?,?)",
                        (OWNER_ID, "owner", utcnow()))
        await c.commit()

async def ensure_user(uid):
    async with db() as c:
        await c.execute("INSERT OR IGNORE INTO users(tg_id,role,created_at) VALUES(?,?,?)",
                        (uid, "user", utcnow()))
        await c.execute("INSERT OR IGNORE INTO memory(owner_id,text) VALUES(?,?)", (uid, ""))
        await c.execute("INSERT OR IGNORE INTO settings(owner_id) VALUES(?)", (uid,))
        await c.commit()

async def role(uid):
    await ensure_user(uid)
    async with db() as c:
        r = await db_fetchone(c, "SELECT role FROM users WHERE tg_id=? AND blocked=0", (uid,))
        return r[0] if r else "blocked"

async def log_action(actor, action, status="ok", details=""):
    async with db() as c:
        await c.execute("INSERT INTO logs(actor_id,action,status,details,created_at) VALUES(?,?,?,?,?)",
                        (actor, action, status, details[:1000], utcnow()))
        await c.commit()

async def get_join_targets():
    async with db() as c:
        rows = await db_fetchall(c, "SELECT id,chat_id,title,invite_url,enabled FROM join_targets ORDER BY id")
    if rows:
        return rows
    return [(0, cid, str(cid), "", 1) for cid in REQUIRED_CHAT_IDS]

async def mandatory_join_ok(uid):
    targets = [r for r in await get_join_targets() if r[4]]
    if not targets:
        return True
    for _, cid, _, _, _ in targets:
        try:
            m = await bot.get_chat_member(cid, uid)
            if m.status in ("left", "kicked"):
                return False
        except Exception:
            # If the bot cannot verify membership, fail closed.
            return False
    return True

async def send_join_gate(uid):
    targets = [r for r in await get_join_targets() if r[4]]
    rows = []
    for _, cid, title, invite_url, _ in targets:
        if invite_url:
            rows.append([InlineKeyboardButton(text=f"📢 {title or cid}", url=invite_url)])
    rows.append([InlineKeyboardButton(text="🟢 Saya Sudah Join", callback_data="join:check", style="success")])
    rows.append([InlineKeyboardButton(text="🔙 Kembali", callback_data="back:main", style="primary")])
    await bot.send_message(uid,
        "🔒 AKSES TERKUNCI\n\n"
        "Kamu harus bergabung ke semua grup/channel wajib sebelum dapat menggunakan COWOK AI.\n\n"
        "1. Tekan tombol grup/channel di bawah.\n"
        "2. Join semuanya.\n"
        "3. Tekan ‘Saya Sudah Join’ untuk verifikasi.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

def button_style(text: str, callback_data: str) -> str:
    """Choose Telegram's native button style by action type."""
    danger_words = ("hapus", "putus", "delete", "stop", "blokir", "hapus", "disconnect", "keluar")
    success_words = ("hubung", "connect", "simpan", "aktif", "resume", "konfirmasi", "verifikasi", "join", "cek", "oke", "mengerti")
    low = f"{text} {callback_data}".lower()
    if any(w in low for w in danger_words):
        return "danger"
    if any(w in low for w in success_words):
        return "success"
    return "primary"


def kb(rows):
    buttons=[]
    for row in rows:
        out=[]
        for t,d in row:
            if isinstance(d, str) and (d.startswith("https://") or d.startswith("http://") or d.startswith("tg://")):
                out.append(InlineKeyboardButton(text=t, url=d, style="primary"))
            else:
                out.append(InlineKeyboardButton(text=t, callback_data=d, style=button_style(t, d)))
        buttons.append(out)
    return InlineKeyboardMarkup(inline_keyboard=buttons)

async def ai_response(*, instructions, input_text, use_web=False):
    """Call the Responses API robustly, with a safe no-tool retry and model fallback.

    This prevents a temporary web-tool/model compatibility issue from turning
    every normal chat message into the generic AI error. The real exception is
    returned to the caller so it can be logged and diagnosed.
    """
    models=[]
    for model in [AI_MODEL, *AI_FALLBACK_MODELS]:
        if model and model not in models:
            models.append(model)
    last_error=None
    for model in models:
        attempts=[]
        if use_web:
            attempts.append([{"type":"web_search"}])
        attempts.append([])
        for tools in attempts:
            try:
                response=await oa.responses.create(
                    model=model,
                    instructions=instructions,
                    input=input_text,
                    tools=tools,
                )
                answer=(response.output_text or "").strip()
                if not answer:
                    raise RuntimeError(f"OpenAI returned empty output (model={model})")
                return answer, model, None
            except Exception as e:
                last_error=e
                log.warning("AI request failed model=%s tools=%s: %s", model, bool(tools), e)
    return None, None, last_error

async def notify_owner_ai_error(context, exc):
    details=f"{type(exc).__name__}: {exc}"
    await log_action(OWNER_ID, context, "error", details)
    try:
        await bot.send_message(OWNER_ID, "⚠️ <b>COWOK AI — AI API ERROR</b>\n\n"
            f"<b>Bagian:</b> {context}\n"
            f"<b>Error:</b> <code>{details[:3000]}</code>\n\n"
            "Periksa OPENAI_API_KEY, AI_MODEL, saldo/akses API, dan deployment Railway.", parse_mode="HTML")
    except Exception:
        log.exception("Failed to notify owner")

async def gate(uid):
    current_role = await role(uid)
    if current_role == "blocked":
        return False
    # Owner can always access the control panel, including while configuring
    # Mandatory Join. Regular users must satisfy all enabled targets.
    if current_role == "owner":
        return True
    if not await mandatory_join_ok(uid):
        await send_join_gate(uid)
        return False
    return True

def main_kb(uid, r):
    rows = [
        [("🤖 AI ASSISTANT","menu:ai"), ("🔗 AKUN AI","menu:account")],
        [("🖼️ IMAGE AI","menu:image"), ("🛠️ AI TOOLS","menu:tools")],
        [("👥 GROUP & CHANNEL","menu:telegram")],
        [("⚙️ SETTINGS","menu:settings")],
        [("👨‍💼 KONTAK ADMIN","contact:admin")],
    ]
    if r in ("owner","admin"):
        rows.append([("👨‍💼 ADMIN","menu:admin")])
    rows.append([("📜 TERMS & CONDITIONS","menu:terms")])
    return kb(rows)

async def get_active_poster():
    # Branding is global and managed by the Owner from the bot panel.
    # If the database has no branding row, fall back to POSTER_FILE_ID.
    async with db() as c:
        row = await db_fetchone(c, "SELECT poster_file_id FROM branding WHERE owner_id=?", (OWNER_ID,))
    return (row[0] if row else POSTER_FILE_ID) or ""

async def set_active_poster(file_id: str):
    async with db() as c:
        await c.execute("INSERT INTO branding(owner_id,poster_file_id,updated_at) VALUES(?,?,?) ON CONFLICT(owner_id) DO UPDATE SET poster_file_id=excluded.poster_file_id, updated_at=excluded.updated_at",
                        (OWNER_ID, file_id, utcnow()))
        await c.commit()

async def clear_active_poster():
    async with db() as c:
        await c.execute("INSERT INTO branding(owner_id,poster_file_id,updated_at) VALUES(?,?,?) ON CONFLICT(owner_id) DO UPDATE SET poster_file_id=excluded.poster_file_id, updated_at=excluded.updated_at",
                        (OWNER_ID, "", utcnow()))
        await c.commit()

async def send_panel(chat_id, text, uid=None, keyboard=None):
    uid = uid or chat_id
    poster = await get_active_poster()
    if poster:
        try:
            await bot.send_photo(chat_id, poster, caption=text, reply_markup=keyboard)
            return
        except Exception:
            pass
    await bot.send_message(chat_id, text, reply_markup=keyboard)

@dp.message(CommandStart())
async def start(m: Message):
    uid = m.from_user.id
    await ensure_user(uid)
    if not await gate(uid): return
    await send_panel(uid, "🤖 COWOK AI\n\nYour Personal Telegram AI Assistant\n\nPilih menu:", uid, main_kb(uid, await role(uid)))

@dp.callback_query(F.data.startswith("menu:"))
async def menus(q: CallbackQuery):
    uid=q.from_user.id
    if not await gate(uid):
        await q.answer(); return
    key=q.data.split(":",1)[1]
    await q.answer()
    back=kb([[("🔙 Kembali","back:main")]])
    if key=="ai":
        await send_panel(uid,"🤖 AI ASSISTANT\n\nAI berjalan melalui akun Telegram yang terhubung.\n\nPilih pengaturan:",uid,
                         kb([[("🧠 Personality","set:personality"),("🎯 AI Mode","set:mode")],
                             [("💾 Memory","set:memory"),("🛠️ Tools","set:tools")],
                             [("🔔 Mention","set:mention")],[("🔙 Kembali","back:main")]]))
    elif key=="account":
        await account_panel(uid)
    elif key=="image":
        await send_panel(uid,"🖼️ IMAGE AI\n\nKirim:\n/image deskripsi gambar\n\nHasil akan dikirim kembali oleh bot panel pada versi dasar ini.",uid,back)
    elif key=="tools":
        await send_panel(uid,"🛠️ AI TOOLS\n\n🌐 Web Search: aktif melalui AI\n📄 PDF/TXT: didukung\n💻 Coding: didukung melalui AI\n📊 Data analysis: dasar\n\n🔙 Kembali",uid,back)
    elif key=="telegram":
        await send_panel(uid,"👥 GROUP & CHANNEL\n\nTindakan Telegram tingkat lanjut harus dilakukan oleh akun AI yang terhubung dan tetap mengikuti permission Telegram.\n\nFitur moderation/creation dapat ditambahkan di layer Telegram tools.",uid,back)
    elif key=="settings":
        await send_panel(uid,"⚙️ SETTINGS\n\nPengaturan AI per akun tersimpan terpisah.",uid,
                         kb([[("🧠 Personality","set:personality"),("🎯 AI Mode","set:mode")],
                             [("💾 Memory","set:memory"),("🛑 Emergency Stop","admin:stop")],
                             [("🔙 Kembali","back:main")]]))
    elif key=="admin":
        if await role(uid) not in ("owner","admin"): return
        await send_panel(uid,"👨‍💼 ADMIN PANEL",uid,
                         kb([[("📊 Dashboard","admin:stats"),("👥 Users","admin:users")],
                             [("📋 Logs","admin:logs"),("📢 Mandatory Join","admin:join")],
                             [("🎨 Poster / Branding","admin:poster")],
                             [("🛑 Emergency Stop","admin:stop"),("▶️ Resume","admin:resume")],
                             [("🔙 Kembali","back:main")]]))
    elif key=="terms":
        await send_panel(uid,"📜 TERMS & CONDITIONS\n\nCOWOK AI adalah perangkat lunak AI yang bekerja dengan akun Telegram yang diotorisasi pemiliknya. Jangan gunakan untuk spam, penipuan, impersonasi, akses tanpa izin, atau aktivitas ilegal. Pemilik akun bertanggung jawab atas tindakan yang dilakukan melalui akun tersebut. Fitur dibatasi oleh API dan permission Telegram. Session dan secret harus dijaga aman.\n\n🔙 Kembali",uid,back)

@dp.callback_query(F.data=="contact:admin")
async def contact_admin(q: CallbackQuery):
    uid=q.from_user.id
    if not await gate(uid):
        await q.answer("🔒 Selesaikan Mandatory Join terlebih dahulu.", show_alert=True)
        return
    username=None
    try:
        owner_chat=await bot.get_chat(OWNER_ID)
        username=getattr(owner_chat, "username", None)
    except Exception as e:
        log.warning("Could not resolve owner username: %s", e)
    contact_url=(f"https://t.me/{username}" if username else ADMIN_CONTACT_URL)
    contact_line=(f"Admin: @{username}" if username else f"Admin ID: <code>{OWNER_ID}</code>")
    await q.answer()
    await q.message.answer(
        "👨‍💼 KONTAK ADMIN\n\n"
        "Butuh bantuan, laporan masalah, atau ingin menghubungi pengelola COWOK AI?\n\n"
        f"{contact_line}\n\n"
        "Tekan tombol di bawah untuk membuka chat Admin.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💬 Chat Admin", url=contact_url, style="primary")],
            [InlineKeyboardButton(text="🔙 Kembali", callback_data="back:main", style="primary")]
        ])
    )

async def account_panel(uid):
    async with db() as c:
        a=await db_fetchone(c, "SELECT tg_id,name,username,connected FROM ai_accounts WHERE owner_id=?", (uid,))
    text="🔗 AKUN AI\n\n"
    if a:
        text += f"Status: {'🟢 Connected' if a[3] else '🔴 Disconnected'}\nID: {a[0]}\nNama: {a[1] or '-'}\nUsername: @{a[2] if a[2] else '-'}"
    else:
        text += "Belum ada akun AI yang terhubung."
    await send_panel(uid,text,uid,kb([[("➕ Hubungkan Akun","account:connect")],
                                      [("🔄 Reconnect","account:reconnect"),("❌ Putuskan","account:disconnect")],
                                      [("🔙 Kembali","back:main")]]))

@dp.callback_query(F.data=="account:connect")
async def connect_start(q: CallbackQuery):
    uid=q.from_user.id
    if not await gate(uid): return
    if uid in qr_tasks and not qr_tasks[uid].done():
        await q.answer("Login sedang berlangsung."); return
    await q.answer()
    await q.message.answer("🔐 Memulai QR login Telegram.\n\nTekan tombol di bawah dan scan QR menggunakan aplikasi Telegram yang akan terhubung.")
    qr_tasks[uid]=asyncio.create_task(qr_login(uid))

async def qr_login(owner_id):
    client=TelegramClient(StringSession(), API_ID, API_HASH)
    try:
        await client.connect()
        qr=await client.qr_login()
        # QR token is rendered as tg://login?token=...; clients may scan this QR.
        import qrcode
        img=qrcode.make(qr.url)
        bio=io.BytesIO(); img.save(bio,"PNG"); bio.seek(0)
        await bot.send_photo(owner_id, BufferedInputFile(bio.read(),"login.png"),
                             caption="📱 Scan QR ini dari Telegram pada perangkat akun yang ingin dijadikan AI Assistant.\n\nQR berlaku singkat. Jangan bagikan gambar ini kepada orang lain.")
        try:
            await qr.wait()
        except Exception as e:
            await bot.send_message(owner_id, f"Login belum selesai: {e}")
            return
        if not await client.is_user_authorized():
            await bot.send_message(owner_id,"❌ Autentikasi belum selesai.")
            return
        me=await client.get_me()
        session=client.session.save()
        async with db() as c:
            await c.execute("DELETE FROM ai_accounts WHERE owner_id=?", (owner_id,))
            await c.execute("""INSERT INTO ai_accounts(owner_id,tg_id,name,username,session_enc,connected,created_at,last_active)
                               VALUES(?,?,?,?,?,?,?,?)""",
                            (owner_id,me.id,me.first_name or "",me.username or "",encrypt_blob(session),1,utcnow(),utcnow()))
            await c.commit()
        await log_action(owner_id,"account_connect","ok",str(me.id))
        await bot.send_message(owner_id,f"✅ Akun berhasil terhubung.\n\nNama: {me.first_name or '-'}\nUsername: @{me.username or '-'}\nTelegram ID: {me.id}\n\nAkun tersebut sekarang dapat digunakan sebagai AI Assistant.")
        await attach_client(owner_id)
    except Exception as e:
        log.exception("QR login")
        await bot.send_message(owner_id,f"❌ Gagal menghubungkan akun: {e}")
        await log_action(owner_id,"account_connect","error",str(e))
    finally:
        qr_tasks.pop(owner_id,None)
        try: await client.disconnect()
        except: pass

async def attach_client(owner_id):
    async with db() as c:
        a=await db_fetchone(c, "SELECT session_enc FROM ai_accounts WHERE owner_id=? AND connected=1",(owner_id,))
    if not a: return
    try:
        session=decrypt_blob(a[0])
        client=TelegramClient(StringSession(session),API_ID,API_HASH)
        await client.start()
        clients[owner_id]=client

        @client.on(events.NewMessage(incoming=True))
        async def handler(ev):
            if STOP_ALL: return
            if not ev.raw_text or ev.out:
                return
            # Basic safety: only reply when message mentions the account or is a private chat.
            try:
                chat=await ev.get_chat()
                is_private=getattr(chat,"id",None) == (await client.get_me()).id
                text=ev.raw_text.strip()
                if not is_private and not text.lower().startswith(("ai ","cowok ai","@cowok")):
                    return
                await handle_ai_message(owner_id, ev, text)
            except Exception:
                log.exception("incoming handler")
        log.info("Attached AI client %s",owner_id)
    except Exception as e:
        log.exception("attach failed %s", owner_id)
        await log_action(owner_id,"account_attach","error",str(e))

async def handle_ai_message(owner_id, ev, text):
    async with db() as c:
        mem=(await db_fetchone(c, "SELECT text FROM memory WHERE owner_id=?",(owner_id,)))[0]
        settings=await db_fetchone(c, "SELECT personality,mode,memory_on,tools_on FROM settings WHERE owner_id=?",(owner_id,))
    system=f"You are COWOK AI, a Telegram AI assistant. Be helpful, concise and honest. Personality={settings[0]}; mode={settings[1]}. Clearly identify yourself as an AI when relevant. Do not impersonate a real person or organization."
    if settings[2]: system += f"\nLong-term memory supplied by owner: {mem[:6000]}"
    try:
        answer, used_model, error = await ai_response(
            instructions=system, input_text=text, use_web=bool(settings[3])
        )
        if error or not answer:
            raise error or RuntimeError("AI returned no answer")
        if len(answer)>4000:
            answer=answer[:3990]+"…"
        await ev.reply(answer)
        async with db() as c:
            await c.execute("UPDATE ai_accounts SET last_active=? WHERE owner_id=?",(utcnow(),owner_id))
            await c.commit()
        await log_action(owner_id,"ai_reply","ok",f"model={used_model}; {answer[:180]}")
    except Exception as e:
        await ev.reply("Maaf, AI sedang mengalami kendala sementara. Coba lagi sebentar.")
        await notify_owner_ai_error("ai_account_reply", e)

@dp.message(F.text)
async def bot_ai_chat(m: Message):
    """Make the Bot account itself usable as a normal AI assistant.

    Private chats get normal conversational AI. In groups, the bot responds
    only when addressed with an AI/COWOK AI prefix, keeping group noise low.
    """
    uid=m.from_user.id
    text=(m.text or "").strip()
    if not text or text.startswith("/"):
        return

    # Mandatory Join setup must be handled here because aiogram stops
    # propagation after a matching F.text handler. This prevents the setup
    # message from being silently swallowed by the general AI chat handler.
    if uid in awaiting_join:
        if uid != OWNER_ID:
            awaiting_join.discard(uid)
            return
        raw=text
        parts=[x.strip() for x in raw.split('|')]
        if len(parts) != 3:
            await m.answer("❌ Format salah. Gunakan: CHAT_ID | NAMA | LINK_JOIN", reply_markup=kb([[('🔙 Kembali','back:main')]]))
            return
        try:
            cid=int(parts[0])
        except ValueError:
            await m.answer("❌ CHAT_ID harus berupa angka, misalnya -1001234567890.")
            return
        title,link=parts[1],parts[2]
        if not re.match(r"^https?://t\.me/",link):
            await m.answer("❌ LINK_JOIN harus berupa link Telegram, misalnya https://t.me/namagrup")
            return
        try:
            chat=await bot.get_chat(cid)
            title=title or (chat.title or str(cid))
        except Exception as e:
            await m.answer("❌ Bot tidak dapat mengakses chat tersebut. Pastikan CHAT_ID benar dan bot sudah masuk ke grup/channel.")
            await log_action(uid,"mandatory_join_add","error",str(e))
            return
        async with db() as c:
            await c.execute("INSERT INTO join_targets(chat_id,title,invite_url,enabled,created_at) VALUES(?,?,?,?,?) ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title,invite_url=excluded.invite_url,enabled=1",
                            (cid,title,link,1,utcnow()))
            await c.commit()
        awaiting_join.discard(uid)
        await log_action(uid,"mandatory_join_add","ok",str(cid))
        await m.answer(f"✅ Mandatory Join ditambahkan: {title}\n\nCHAT_ID: {cid}\nStatus: 🟢 Aktif", reply_markup=kb([[('📢 Mandatory Join','admin:join')],[('🔙 Kembali','back:main')]]))
        return

    if uid in awaiting_poster:
        return
    if not await gate(uid):
        return

    # In groups, respond only when explicitly addressed.
    if m.chat.type != "private":
        low=text.lower()
        if not low.startswith(("ai ", "ai,", "cowok ai", "@cowok", "@cowokbot")):
            return

    async with db() as c:
        mem_row=await db_fetchone(c, "SELECT text FROM memory WHERE owner_id=?", (uid,))
        settings=await db_fetchone(c, "SELECT personality,mode,memory_on,tools_on FROM settings WHERE owner_id=?", (uid,))
    mem=(mem_row[0] if mem_row else "")
    personality,mode,memory_on,tools_on=settings or ("helpful","smart",1,1)
    system=(
        "You are COWOK AI, the AI assistant operating directly through the Telegram bot. "
        "You are not a human and must not claim to be one. Be helpful, natural, concise, "
        "and honest. Answer in the user's language when practical. You can help with "
        "conversation, explanations, writing, coding, planning, analysis, and other lawful tasks. "
        f"Personality={personality}; mode={mode}."
    )
    if memory_on and mem:
        system += f"\nRelevant long-term memory for this user: {mem[:6000]}"
    try:
        try:
            await bot.send_chat_action(m.chat.id, "typing")
        except Exception:
            pass
        answer, used_model, error = await ai_response(
            instructions=system, input_text=text, use_web=bool(tools_on)
        )
        if error or not answer:
            raise error or RuntimeError("AI returned no answer")
        if len(answer)>4000:
            answer=answer[:3990]+"…"
        await m.answer(answer)
        await log_action(uid,"bot_ai_reply","ok",f"model={used_model}; {answer[:180]}")
    except Exception as e:
        log.exception("bot AI chat")
        await m.answer("❌ AI sedang mengalami kendala sementara. Sistem sudah mencatat error untuk Admin. Silakan coba lagi sebentar.")
        await notify_owner_ai_error("bot_ai_reply", e)

@dp.message(Command("image"))
async def image_cmd(m: Message):
    uid=m.from_user.id
    if not await gate(uid): return
    prompt=m.text.partition(" ")[2].strip()
    if not prompt:
        await m.answer("Gunakan: /image deskripsi gambar")
        return
    try:
        result=await oa.images.generate(model="gpt-image-1",prompt=prompt,size="1024x1024")
        b64=result.data[0].b64_json
        data=base64.b64decode(b64)
        await m.answer_photo(BufferedInputFile(data,"cowok-ai.png"),caption="🖼️ COWOK AI")
        await log_action(uid,"image_generate","ok",prompt[:200])
    except Exception as e:
        await m.answer("❌ Gagal membuat gambar. Error sudah dicatat untuk Admin.")
        await notify_owner_ai_error("image_generate", e)

@dp.callback_query(F.data=="account:disconnect")
async def disconnect(q: CallbackQuery):
    uid=q.from_user.id
    async with db() as c:
        a=await db_fetchone(c, "SELECT tg_id FROM ai_accounts WHERE owner_id=?",(uid,))
        await c.execute("DELETE FROM ai_accounts WHERE owner_id=?",(uid,))
        await c.commit()
    cl=clients.pop(uid,None)
    if cl:
        try: await cl.disconnect()
        except: pass
    await q.answer("Akun diputuskan.")
    await account_panel(uid)

@dp.callback_query(F.data=="account:reconnect")
async def reconnect(q: CallbackQuery):
    uid=q.from_user.id
    await q.answer("Mencoba reconnect…")
    await attach_client(uid)
    await account_panel(uid)

@dp.callback_query(F.data.startswith("back:"))
async def back(q: CallbackQuery):
    uid=q.from_user.id
    await q.answer()
    await send_panel(uid,"🤖 COWOK AI\n\nYour Personal Telegram AI Assistant\n\nPilih menu:",uid,main_kb(uid,await role(uid)))

@dp.callback_query(F.data.startswith("set:"))
async def settings_cb(q: CallbackQuery):
    uid=q.from_user.id; action=q.data.split(":")[1]
    if action=="personality":
        async with db() as c:
            await c.execute("UPDATE settings SET personality=CASE WHEN personality='helpful' THEN 'professional' ELSE 'helpful' END WHERE owner_id=?",(uid,))
            await c.commit()
        await q.answer("Personality diganti.")
    elif action=="mode":
        async with db() as c:
            await c.execute("UPDATE settings SET mode=CASE WHEN mode='smart' THEN 'fast' ELSE 'smart' END WHERE owner_id=?",(uid,))
            await c.commit()
        await q.answer("AI mode diganti.")
    elif action=="memory":
        async with db() as c:
            await c.execute("UPDATE settings SET memory_on=1-memory_on WHERE owner_id=?",(uid,))
            await c.commit()
        await q.answer("Memory diubah.")
    elif action=="tools":
        async with db() as c:
            await c.execute("UPDATE settings SET tools_on=1-tools_on WHERE owner_id=?",(uid,))
            await c.commit()
        await q.answer("Tools diubah.")
    elif action=="mention":
        async with db() as c:
            await c.execute("UPDATE settings SET mentions_on=1-mentions_on WHERE owner_id=?",(uid,))
            await c.commit()
        await q.answer("Mention diubah.")
    # Refresh the AI settings panel directly instead of constructing a fake
    # CallbackQuery object (which has no message and can fail on aiogram 3).
    await send_panel(uid, "🤖 AI ASSISTANT\n\nAI berjalan melalui akun Telegram yang terhubung.\n\nPilih pengaturan:", uid,
                     kb([[('🧠 Personality','set:personality'),('🎯 AI Mode','set:mode')],
                         [('💾 Memory','set:memory'),('🛠️ Tools','set:tools')],
                         [('🔔 Mention','set:mention')],[('🔙 Kembali','back:main')]]))

@dp.callback_query(F.data=="admin:stats")
async def stats(q: CallbackQuery):
    if await role(q.from_user.id) not in ("owner","admin"): return
    async with db() as c:
        users=(await db_fetchone(c, "SELECT COUNT(*) FROM users"))[0]
        accounts=(await db_fetchone(c, "SELECT COUNT(*) FROM ai_accounts WHERE connected=1"))[0]
        logs=(await db_fetchone(c, "SELECT COUNT(*) FROM logs"))[0]
    await q.answer()
    await q.message.answer(f"📊 Dashboard\n\nUsers: {users}\nConnected AI: {accounts}\nActivity logs: {logs}",
                            reply_markup=kb([[("🔙 Kembali","back:main")]]))

@dp.callback_query(F.data=="admin:users")
async def users(q: CallbackQuery):
    if await role(q.from_user.id) not in ("owner","admin"): return
    async with db() as c:
        rows=await db_fetchall(c, "SELECT tg_id,role,blocked FROM users ORDER BY created_at DESC LIMIT 30")
    text="👥 USERS\n\n"+"\n".join(f"{x[0]} — {x[1]} — {'blocked' if x[2] else 'active'}" for x in rows)
    await q.answer(); await q.message.answer(text,reply_markup=kb([[("🔙 Kembali","back:main")]]))

@dp.callback_query(F.data=="admin:logs")
async def logs_cb(q: CallbackQuery):
    if await role(q.from_user.id) not in ("owner","admin"): return
    async with db() as c:
        rows=await db_fetchall(c, "SELECT actor_id,action,status,created_at FROM logs ORDER BY id DESC LIMIT 30")
    text="📋 LOGS\n\n"+"\n".join(f"{r[3]} | {r[0]} | {r[1]} | {r[2]}" for r in rows)
    await q.answer(); await q.message.answer(text,reply_markup=kb([[("🔙 Kembali","back:main")]]))

@dp.callback_query(F.data=="admin:join")
async def join_cb(q: CallbackQuery):
    uid=q.from_user.id
    if await role(uid) not in ("owner","admin"): return
    targets=await get_join_targets()
    active=[r for r in targets if r[4]]
    lines=[]
    for i,r in enumerate(targets,1):
        status="🟢 Aktif" if r[4] else "🔴 Nonaktif"
        lines.append(f"{i}. {r[2] or r[1]} — {status}\n   ID: {r[1]}" + (f"\n   Link: {r[3]}" if r[3] else "\n   ⚠️ Belum ada link join"))
    text=("📢 MANDATORY JOIN\n\n"
          f"Status: {'🟢 Aktif' if active else '🔴 Belum dikonfigurasi'}\n"
          f"Target: {len(active)}\n\n"
          +("\n\n".join(lines) if lines else "Belum ada grup/channel wajib."))
    await q.answer()
    rows=[[('➕ Tambah Target','join:add')]]
    if targets:
        rows += [[('🗑️ Hapus Target','join:remove')],[('🔄 Aktif/Nonaktif','join:toggle')]]
    rows += [[('🔙 Kembali','back:main')]]
    await q.message.answer(text,reply_markup=kb(rows))

@dp.callback_query(F.data=="join:add")
async def join_add(q: CallbackQuery):
    uid=q.from_user.id
    if await role(uid) != "owner": return
    awaiting_join.add(uid)
    await q.answer()
    await q.message.answer(
        "➕ TAMBAH MANDATORY JOIN\n\n"
        "Kirim data dengan format:\n"
        "CHAT_ID | NAMA | LINK_JOIN\n\n"
        "Contoh:\n-1001234567890 | COWOK AI GROUP | https://t.me/namagrup\n\n"
        "Bot harus bisa melihat member grup/channel tersebut. Untuk channel, bot perlu akses admin yang sesuai agar verifikasi member dapat berjalan.",
        reply_markup=kb([[('🔙 Kembali','back:main')]])
    )

@dp.callback_query(F.data=="join:remove")
async def join_remove(q: CallbackQuery):
    uid=q.from_user.id
    if await role(uid) != "owner": return
    targets=await get_join_targets()
    if not targets:
        await q.answer("Belum ada target.",show_alert=True); return
    rows=[]
    for r in targets:
        rows.append([(f"🗑️ {r[2] or r[1]}",f"join:del:{r[1]}")])
    rows.append([('🔙 Kembali','back:main')])
    await q.answer()
    await q.message.answer("🗑️ Pilih target yang ingin dihapus:",reply_markup=kb(rows))

@dp.callback_query(F.data.startswith("join:del:"))
async def join_delete(q: CallbackQuery):
    uid=q.from_user.id
    if await role(uid) != "owner": return
    cid=int(q.data.rsplit(':',1)[1])
    async with db() as c:
        await c.execute("DELETE FROM join_targets WHERE chat_id=?",(cid,))
        await c.commit()
    await log_action(uid,"mandatory_join_delete","ok",str(cid))
    await q.answer("Target dihapus.")
    await join_cb(q)

@dp.callback_query(F.data=="join:toggle")
async def join_toggle(q: CallbackQuery):
    uid=q.from_user.id
    if await role(uid) != "owner": return
    targets=await get_join_targets()
    rows=[]
    for r in targets:
        label=f"{'🟢' if r[4] else '🔴'} {r[2] or r[1]}"
        rows.append([(label,f"join:flip:{r[1]}")])
    rows.append([('🔙 Kembali','back:main')])
    await q.answer()
    await q.message.answer("🔄 Aktif / Nonaktifkan target:",reply_markup=kb(rows))

@dp.callback_query(F.data.startswith("join:flip:"))
async def join_flip(q: CallbackQuery):
    uid=q.from_user.id
    if await role(uid) != "owner": return
    cid=int(q.data.rsplit(':',1)[1])
    async with db() as c:
        await c.execute("UPDATE join_targets SET enabled=1-enabled WHERE chat_id=?",(cid,))
        await c.commit()
    await log_action(uid,"mandatory_join_toggle","ok",str(cid))
    await q.answer("Status diperbarui.")
    await join_cb(q)

@dp.callback_query(F.data=="join:check")
async def join_check(q: CallbackQuery):
    uid=q.from_user.id
    if await mandatory_join_ok(uid):
        await q.answer("✅ Verifikasi berhasil!",show_alert=True)
        await send_panel(uid,"🤖 COWOK AI\n\nAkses berhasil dibuka. Pilih menu:",uid,main_kb(uid,await role(uid)))
    else:
        await q.answer("❌ Kamu belum join semuanya.",show_alert=True)
        await send_join_gate(uid)

@dp.callback_query(F.data=="admin:poster")
async def poster_admin(q: CallbackQuery):
    uid=q.from_user.id
    if await role(uid) != "owner":
        return
    poster=await get_active_poster()
    await q.answer()
    status="🟢 Aktif" if poster else "🔴 Belum ada poster"
    await q.message.answer(
        f"🎨 POSTER & BRANDING\n\nStatus poster: {status}\n\n"
        "Poster ini digunakan sebagai poster utama pada panel COWOK AI.\n"
        "Pengaturan hanya dapat dilakukan oleh Owner.",
        reply_markup=kb([[('🖼️ Set Poster','poster:set')],
                         [('👁️ Lihat Poster','poster:view'),('🔄 Ganti Poster','poster:set')],
                         [('🗑️ Hapus Poster','poster:delete')],
                         [('🔙 Kembali','back:main')]])
    )

@dp.callback_query(F.data=="poster:set")
async def poster_set_start(q: CallbackQuery):
    uid=q.from_user.id
    if await role(uid) != "owner":
        return
    awaiting_poster.add(uid)
    await q.answer()
    await q.message.answer(
        "🖼️ KIRIM POSTER\n\n"
        "Silakan kirim 1 foto yang ingin dijadikan poster COWOK AI.\n"
        "Setelah foto diterima, poster akan langsung menjadi poster aktif.\n\n"
        "🔙 Untuk membatalkan, tekan Kembali.",
        reply_markup=kb([[('🔙 Kembali','back:main')]])
    )

@dp.callback_query(F.data=="poster:view")
async def poster_view(q: CallbackQuery):
    uid=q.from_user.id
    if await role(uid) != "owner":
        return
    poster=await get_active_poster()
    await q.answer()
    if not poster:
        await q.message.answer("🖼️ Belum ada poster yang disetel.", reply_markup=kb([[('🔙 Kembali','back:main')]]))
        return
    try:
        await bot.send_photo(uid, poster, caption="🎨 POSTER AKTIF", reply_markup=kb([[('🔙 Kembali','back:main')]]))
    except Exception:
        await q.message.answer("❌ Poster tidak dapat ditampilkan. Silakan set poster baru.", reply_markup=kb([[('🔙 Kembali','back:main')]]))

@dp.callback_query(F.data=="poster:delete")
async def poster_delete(q: CallbackQuery):
    uid=q.from_user.id
    if await role(uid) != "owner":
        return
    await clear_active_poster()
    await q.answer("Poster dihapus.")
    await log_action(uid,"poster_delete","ok")
    await q.message.answer("🗑️ Poster berhasil dihapus.\n\nBot kembali menggunakan tampilan teks tanpa poster.", reply_markup=kb([[('🔙 Kembali','back:main')]]))

@dp.message(F.photo)
async def poster_photo(m: Message):
    uid=m.from_user.id
    if uid != OWNER_ID or uid not in awaiting_poster:
        return
    awaiting_poster.discard(uid)
    file_id=m.photo[-1].file_id
    await set_active_poster(file_id)
    await log_action(uid,"poster_set","ok",file_id)
    await m.answer_photo(file_id, caption="✅ POSTER AKTIF\n\nPoster COWOK AI berhasil disimpan dan akan digunakan pada panel bot berikutnya.", reply_markup=kb([[('🎨 Poster / Branding','admin:poster')],[('🔙 Kembali','back:main')]]))

@dp.callback_query(F.data=="admin:stop")
async def stop_cb(q: CallbackQuery):
    global STOP_ALL
    if await role(q.from_user.id) != "owner": return
    STOP_ALL=True; await q.answer("Emergency Stop aktif."); await log_action(q.from_user.id,"emergency_stop","ok")

@dp.callback_query(F.data=="admin:resume")
async def resume_cb(q: CallbackQuery):
    global STOP_ALL
    if await role(q.from_user.id) != "owner": return
    STOP_ALL=False; await q.answer("AI resumed."); await log_action(q.from_user.id,"emergency_resume","ok")

@dp.message(F.document)
async def document(m: Message):
    uid=m.from_user.id
    if not await gate(uid): return
    doc=m.document
    if doc.file_size and doc.file_size > 15*1024*1024:
        await m.answer("File terlalu besar untuk mode dasar.")
        return
    f=await bot.get_file(doc.file_id)
    data=io.BytesIO()
    await bot.download_file(f.file_path,data)
    data.seek(0)
    text=""
    if doc.file_name.lower().endswith(".pdf"):
        reader=PdfReader(data)
        text="\n".join((p.extract_text() or "") for p in reader.pages)
    else:
        try: text=data.read().decode("utf-8","ignore")
        except: text=""
    if not text.strip():
        await m.answer("Saya tidak dapat mengekstrak teks dari file ini.")
        return
    text=text[:30000]
    try:
        ans, used_model, error=await ai_response(
            instructions="Analisis dokumen berikut dan jawab secara ringkas dalam bahasa Indonesia.",
            input_text=text, use_web=False
        )
        if error or not ans:
            raise error or RuntimeError("AI returned no answer")
        await m.answer(ans[:4000])
        await log_action(uid,"document_analysis","ok",f"model={used_model}; {doc.file_name}")
    except Exception as e:
        await m.answer("❌ Gagal menganalisis dokumen. Error sudah dicatat untuk Admin.")
        await notify_owner_ai_error("document_analysis", e)

async def startup():
    await init_db()
    async with db() as c:
        rows=await db_fetchall(c, "SELECT owner_id FROM ai_accounts WHERE connected=1")
    for (uid,) in rows:
        await attach_client(uid)
    scheduler.start()

async def main():
    await startup()
    try:
        await dp.start_polling(bot)
    finally:
        for cl in clients.values():
            try: await cl.disconnect()
            except: pass
        await bot.session.close()

if __name__=="__main__":
    asyncio.run(main())
