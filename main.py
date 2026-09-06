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
AI_MODEL = os.getenv("AI_MODEL", "gpt-5.4")
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
STOP_ALL = False

def utcnow():
    return datetime.now(timezone.utc).isoformat()

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

async def db():
    return await aiosqlite.connect(DB_PATH)

async def init_db():
    async with await db() as c:
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
        """)
        await c.execute("INSERT OR IGNORE INTO users(tg_id,role,created_at) VALUES(?,?,?)",
                        (OWNER_ID, "owner", utcnow()))
        await c.commit()

async def ensure_user(uid):
    async with await db() as c:
        await c.execute("INSERT OR IGNORE INTO users(tg_id,role,created_at) VALUES(?,?,?)",
                        (uid, "user", utcnow()))
        await c.execute("INSERT OR IGNORE INTO memory(owner_id,text) VALUES(?,?)", (uid, ""))
        await c.execute("INSERT OR IGNORE INTO settings(owner_id) VALUES(?)", (uid,))
        await c.commit()

async def role(uid):
    await ensure_user(uid)
    async with await db() as c:
        r = await c.execute_fetchone("SELECT role FROM users WHERE tg_id=? AND blocked=0", (uid,))
        return r[0] if r else "blocked"

async def log_action(actor, action, status="ok", details=""):
    async with await db() as c:
        await c.execute("INSERT INTO logs(actor_id,action,status,details,created_at) VALUES(?,?,?,?,?)",
                        (actor, action, status, details[:1000], utcnow()))
        await c.commit()

async def mandatory_join_ok(uid):
    if not REQUIRED_CHAT_IDS:
        return True
    for cid in REQUIRED_CHAT_IDS:
        try:
            m = await bot.get_chat_member(cid, uid)
            if m.status in ("left", "kicked"):
                return False
        except Exception:
            # If the bot cannot verify membership, fail closed.
            return False
    return True

def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t,d in row] for row in rows
    ])

async def gate(uid):
    if await role(uid) == "blocked":
        return False
    if not await mandatory_join_ok(uid):
        await bot.send_message(uid, "📢 Silakan bergabung ke semua grup/channel wajib terlebih dahulu.")
        return False
    return True

def main_kb(uid, r):
    rows = [
        [("🤖 AI ASSISTANT","menu:ai"), ("🔗 AKUN AI","menu:account")],
        [("🖼️ IMAGE AI","menu:image"), ("🛠️ AI TOOLS","menu:tools")],
        [("👥 GROUP & CHANNEL","menu:telegram")],
        [("⚙️ SETTINGS","menu:settings")],
    ]
    if r in ("owner","admin"):
        rows.append([("👨‍💼 ADMIN","menu:admin")])
    rows.append([("📜 TERMS & CONDITIONS","menu:terms")])
    return kb(rows)

async def send_panel(chat_id, text, uid=None, keyboard=None):
    uid = uid or chat_id
    if POSTER_FILE_ID:
        try:
            await bot.send_photo(chat_id, POSTER_FILE_ID, caption=text, reply_markup=keyboard)
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
                             [("🛑 Emergency Stop","admin:stop"),("▶️ Resume","admin:resume")],
                             [("🔙 Kembali","back:main")]]))
    elif key=="terms":
        await send_panel(uid,"📜 TERMS & CONDITIONS\n\nCOWOK AI adalah perangkat lunak AI yang bekerja dengan akun Telegram yang diotorisasi pemiliknya. Jangan gunakan untuk spam, penipuan, impersonasi, akses tanpa izin, atau aktivitas ilegal. Pemilik akun bertanggung jawab atas tindakan yang dilakukan melalui akun tersebut. Fitur dibatasi oleh API dan permission Telegram. Session dan secret harus dijaga aman.\n\n🔙 Kembali",uid,back)

async def account_panel(uid):
    async with await db() as c:
        a=await c.execute_fetchone("SELECT tg_id,name,username,connected FROM ai_accounts WHERE owner_id=?", (uid,))
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
        async with await db() as c:
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
    async with await db() as c:
        a=await c.execute_fetchone("SELECT session_enc FROM ai_accounts WHERE owner_id=? AND connected=1",(owner_id,))
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
    async with await db() as c:
        mem=(await c.execute_fetchone("SELECT text FROM memory WHERE owner_id=?",(owner_id,)))[0]
        settings=await c.execute_fetchone("SELECT personality,mode,memory_on,tools_on FROM settings WHERE owner_id=?",(owner_id,))
    system=f"You are COWOK AI, a Telegram AI assistant. Be helpful, concise and honest. Personality={settings[0]}; mode={settings[1]}. Clearly identify yourself as an AI when relevant. Do not impersonate a real person or organization."
    if settings[2]: system += f"\nLong-term memory supplied by owner: {mem[:6000]}"
    try:
        response=await oa.responses.create(
            model=AI_MODEL,
            instructions=system,
            input=text,
            tools=[{"type":"web_search"}] if settings[3] else []
        )
        answer=response.output_text or "Maaf, saya tidak mendapatkan jawaban."
        if len(answer)>4000:
            answer=answer[:3990]+"…"
        await ev.reply(answer)
        async with await db() as c:
            await c.execute("UPDATE ai_accounts SET last_active=? WHERE owner_id=?",(utcnow(),owner_id))
            await c.commit()
        await log_action(owner_id,"ai_reply","ok",answer[:200])
    except Exception as e:
        await ev.reply("Maaf, AI sedang mengalami kendala. Coba lagi sebentar.")
        await log_action(owner_id,"ai_reply","error",str(e))

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
        await m.answer("❌ Gagal membuat gambar.")
        await log_action(uid,"image_generate","error",str(e))

@dp.callback_query(F.data=="account:disconnect")
async def disconnect(q: CallbackQuery):
    uid=q.from_user.id
    async with await db() as c:
        a=await c.execute_fetchone("SELECT tg_id FROM ai_accounts WHERE owner_id=?",(uid,))
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
        async with await db() as c:
            await c.execute("UPDATE settings SET personality=CASE WHEN personality='helpful' THEN 'professional' ELSE 'helpful' END WHERE owner_id=?",(uid,))
            await c.commit()
        await q.answer("Personality diganti.")
    elif action=="mode":
        async with await db() as c:
            await c.execute("UPDATE settings SET mode=CASE WHEN mode='smart' THEN 'fast' ELSE 'smart' END WHERE owner_id=?",(uid,))
            await c.commit()
        await q.answer("AI mode diganti.")
    elif action=="memory":
        async with await db() as c:
            await c.execute("UPDATE settings SET memory_on=1-memory_on WHERE owner_id=?",(uid,))
            await c.commit()
        await q.answer("Memory diubah.")
    elif action=="tools":
        async with await db() as c:
            await c.execute("UPDATE settings SET tools_on=1-tools_on WHERE owner_id=?",(uid,))
            await c.commit()
        await q.answer("Tools diubah.")
    elif action=="mention":
        async with await db() as c:
            await c.execute("UPDATE settings SET mentions_on=1-mentions_on WHERE owner_id=?",(uid,))
            await c.commit()
        await q.answer("Mention diubah.")
    await menus(await FakeCallback(uid,"menu:ai"))

class FakeCallback:
    def __init__(self,uid,data): self.from_user=type("U",(),{"id":uid})(); self.data=data; self.message=None
    async def answer(self,*a,**k): pass

@dp.callback_query(F.data=="admin:stats")
async def stats(q: CallbackQuery):
    if await role(q.from_user.id) not in ("owner","admin"): return
    async with await db() as c:
        users=(await c.execute_fetchone("SELECT COUNT(*) FROM users"))[0]
        accounts=(await c.execute_fetchone("SELECT COUNT(*) FROM ai_accounts WHERE connected=1"))[0]
        logs=(await c.execute_fetchone("SELECT COUNT(*) FROM logs"))[0]
    await q.answer()
    await q.message.answer(f"📊 Dashboard\n\nUsers: {users}\nConnected AI: {accounts}\nActivity logs: {logs}",
                            reply_markup=kb([[("🔙 Kembali","back:main")]]))

@dp.callback_query(F.data=="admin:users")
async def users(q: CallbackQuery):
    if await role(q.from_user.id) not in ("owner","admin"): return
    async with await db() as c:
        rows=await c.execute_fetchall("SELECT tg_id,role,blocked FROM users ORDER BY created_at DESC LIMIT 30")
    text="👥 USERS\n\n"+"\n".join(f"{x[0]} — {x[1]} — {'blocked' if x[2] else 'active'}" for x in rows)
    await q.answer(); await q.message.answer(text,reply_markup=kb([[("🔙 Kembali","back:main")]]))

@dp.callback_query(F.data=="admin:logs")
async def logs_cb(q: CallbackQuery):
    if await role(q.from_user.id) not in ("owner","admin"): return
    async with await db() as c:
        rows=await c.execute_fetchall("SELECT actor_id,action,status,created_at FROM logs ORDER BY id DESC LIMIT 30")
    text="📋 LOGS\n\n"+"\n".join(f"{r[3]} | {r[0]} | {r[1]} | {r[2]}" for r in rows)
    await q.answer(); await q.message.answer(text,reply_markup=kb([[("🔙 Kembali","back:main")]]))

@dp.callback_query(F.data=="admin:join")
async def join_cb(q: CallbackQuery):
    if await role(q.from_user.id) not in ("owner","admin"): return
    await q.answer()
    await q.message.answer("📢 Mandatory Join\n\nTarget IDs saat ini:\n"+("\n".join(map(str,REQUIRED_CHAT_IDS)) if REQUIRED_CHAT_IDS else "Tidak ada"),
                            reply_markup=kb([[("🔙 Kembali","back:main")]]))

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
        r=await oa.responses.create(model=AI_MODEL,instructions="Analisis dokumen berikut dan jawab secara ringkas dalam bahasa Indonesia.",input=text)
        ans=r.output_text or "Tidak ada hasil."
        await m.answer(ans[:4000])
        await log_action(uid,"document_analysis","ok",doc.file_name)
    except Exception as e:
        await m.answer("❌ Gagal menganalisis dokumen.")
        await log_action(uid,"document_analysis","error",str(e))

async def startup():
    await init_db()
    async with await db() as c:
        rows=await c.execute_fetchall("SELECT owner_id FROM ai_accounts WHERE connected=1")
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
