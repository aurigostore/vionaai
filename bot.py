"""Entry point: bot Telegram untuk asisten pribadi."""

import asyncio
import base64
import contextlib
import html
import json
import logging
import re
from datetime import datetime, time as dtime

import anthropic
from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
import health
import tools
from agent import Agent
from db import Database, now

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpx2").setLevel(logging.WARNING)
log = logging.getLogger("bot")

TELEGRAM_LIMIT = 4000
TEXT_DOC_EXT = (".txt", ".md", ".csv", ".json", ".log", ".py", ".html", ".xml", ".yaml", ".yml")

db = Database(config.DB_PATH)
agent = Agent(db)


# ---------------------------------------------------------------------------
# Format & kirim balasan
# ---------------------------------------------------------------------------

def md_to_html(text: str) -> str:
    """Ubah markdown sederhana dari Claude ke HTML Telegram."""
    blocks: list[str] = []

    def stash(m: re.Match) -> str:
        blocks.append(f"<pre>{html.escape(m.group(1).strip())}</pre>")
        return f"\x00{len(blocks) - 1}\x00"

    text = re.sub(r"```(?:[\w+-]*\n)?(.*?)```", stash, text, flags=re.S)
    text = html.escape(text, quote=False)
    text = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.S)
    text = re.sub(r"(?<![\w*])\*(?!\s)([^*\n]+?)\*(?![\w*])", r"<i>\1</i>", text)
    text = re.sub(r"(?<!\w)_(?!\s)([^_\n]+?)_(?!\w)", r"<i>\1</i>", text)
    text = re.sub(r"^#{1,6}\s*(.+)$", r"<b>\1</b>", text, flags=re.M)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', text)
    return re.sub(r"\x00(\d+)\x00", lambda m: blocks[int(m.group(1))], text)


def split_text(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    parts = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return parts + [text] if text else parts


async def send_reply(bot, chat_id: int, text: str) -> None:
    for part in split_text(text):
        try:
            await bot.send_message(chat_id, md_to_html(part), parse_mode=ParseMode.HTML,
                                   disable_web_page_preview=True)
        except BadRequest:
            await bot.send_message(chat_id, part, disable_web_page_preview=True)


@contextlib.asynccontextmanager
async def typing(bot, chat_id: int):
    async def loop():
        while True:
            with contextlib.suppress(Exception):
                await bot.send_chat_action(chat_id, ChatAction.TYPING)
            await asyncio.sleep(4)

    task = asyncio.create_task(loop())
    try:
        yield
    finally:
        task.cancel()


# ---------------------------------------------------------------------------
# Pengingat
# ---------------------------------------------------------------------------

def schedule_reminder(app: Application, reminder: dict) -> None:
    name = f"reminder-{reminder['id']}"
    for job in app.job_queue.get_jobs_by_name(name):
        job.schedule_removal()

    if reminder["kind"] == "sekali":
        run_at = datetime.fromisoformat(reminder["run_at"])
        if run_at <= now():
            db.deactivate_reminder(reminder["user_id"], reminder["id"])
            return
        app.job_queue.run_once(fire_reminder, when=run_at, data=reminder, name=name)
    else:
        hh, mm = (int(x) for x in reminder["time_of_day"].split(":"))
        days = json.loads(reminder["days"]) if reminder["days"] else list(range(1, 8))
        ptb_days = tuple(sorted(d % 7 for d in days))  # PTB: 0=Minggu ... 6=Sabtu
        app.job_queue.run_daily(fire_reminder, time=dtime(hh, mm, tzinfo=config.TIMEZONE),
                                days=ptb_days, data=reminder, name=name)


def unschedule_reminder(app: Application, reminder_id: int) -> None:
    for job in app.job_queue.get_jobs_by_name(f"reminder-{reminder_id}"):
        job.schedule_removal()


async def fire_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    r = context.job.data
    try:
        if r["use_ai"]:
            ctx = make_tool_ctx(context.application, r["user_id"], r["chat_id"])
            prompt = (f"[PENGINGAT TERJADWAL #{r['id']}] Waktunya mengirim pengingat ke user. "
                      f"Instruksi: {r['message']}\nTulis pesan pengingatnya langsung (boleh cek data dengan tools).")
            text = await agent.respond(ctx, [{"type": "text", "text": prompt}], prompt)
        else:
            text = f"⏰ **Pengingat**\n{r['message']}"
            db.add_message(r["user_id"], "user", f"[pengingat terjadwal #{r['id']} terkirim]")
            db.add_message(r["user_id"], "assistant", text)
        await send_reply(context.bot, r["chat_id"], text)
    except Exception:
        log.exception("Gagal mengirim pengingat %s", r["id"])
        await context.bot.send_message(r["chat_id"], f"⏰ Pengingat: {r['message']}")
    finally:
        if r["kind"] == "sekali":
            db.deactivate_reminder(r["user_id"], r["id"])


def make_tool_ctx(app: Application, user_id: int, chat_id: int) -> tools.ToolContext:
    return tools.ToolContext(
        db=db,
        user_id=user_id,
        chat_id=chat_id,
        schedule_reminder=lambda r: schedule_reminder(app, r),
        unschedule_reminder=lambda rid: unschedule_reminder(app, rid),
    )


# ---------------------------------------------------------------------------
# Handler pesan
# ---------------------------------------------------------------------------

async def process(update: Update, context: ContextTypes.DEFAULT_TYPE, content: list[dict], history_text: str):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    ctx = make_tool_ctx(context.application, user_id, chat_id)
    try:
        async with typing(context.bot, chat_id):
            reply = await agent.respond(ctx, content, history_text)
    except anthropic.AuthenticationError:
        reply = "⚠️ API key AI ditolak. Cek AI_API_KEY di .env."
    except anthropic.RateLimitError:
        reply = "⚠️ Kena batas pemakaian API. Coba lagi sebentar lagi."
    except anthropic.APIStatusError as e:
        log.error("API error %s: %s", e.status_code, e.message)
        reply = f"⚠️ Server AI error ({e.status_code}). Coba lagi nanti."
    except anthropic.APIConnectionError:
        reply = "⚠️ Tidak bisa terhubung ke server AI."
    except Exception:
        log.exception("Error memproses pesan")
        reply = "⚠️ Terjadi error di bot. Cek log server."
    await send_reply(context.bot, chat_id, reply)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    await process(update, context, [{"type": "text", "text": text}], text)


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo = update.message.photo[-1]
    data = await (await photo.get_file()).download_as_bytearray()
    caption = update.message.caption or "Tolong lihat gambar ini."
    content = [
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                     "data": base64.standard_b64encode(bytes(data)).decode()}},
        {"type": "text", "text": caption},
    ]
    await process(update, context, content, f"[mengirim foto] {caption}")


async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    name = doc.file_name or "file"
    mime = doc.mime_type or ""
    caption = update.message.caption or f"Tolong baca file {name}."
    if doc.file_size and doc.file_size > 20 * 1024 * 1024:
        await update.message.reply_text("File terlalu besar (maks 20 MB).")
        return

    data = bytes(await (await doc.get_file()).download_as_bytearray())
    b64 = base64.standard_b64encode(data).decode()
    if mime in ("image/jpeg", "image/png", "image/gif", "image/webp"):
        block = {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}}
    elif mime == "application/pdf":
        block = {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": b64},
                 "title": name}
    elif mime.startswith("text/") or name.lower().endswith(TEXT_DOC_EXT):
        block = {"type": "text", "text": f"Isi file {name}:\n\n{data.decode('utf-8', errors='replace')[:150_000]}"}
    else:
        await update.message.reply_text("Format file ini belum didukung. Kirim PDF, gambar, atau file teks.")
        return
    await process(update, context, [block, {"type": "text", "text": caption}], f"[mengirim file {name}] {caption}")


async def on_unsupported(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Untuk sekarang aku baru bisa baca teks, foto, PDF, dan file teks. "
                                    "Pesan suara belum didukung 🙏")


async def on_stranger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_message:
        await update.effective_message.reply_text(
            f"Bot ini privat. ID Telegram kamu: {update.effective_user.id}")


# ---------------------------------------------------------------------------
# Perintah
# ---------------------------------------------------------------------------

HELP = """Halo! Aku asisten pribadimu 🤖

Ngobrol saja seperti biasa. Aku akan mengingat hal penting tentangmu, mencatat kesehatan, membuat pengingat, mencari info di internet, dan membaca foto/PDF.

Contoh:
- "Aku pria, lahir 1998, tinggi 172, berat 75, mau turunin berat"
- "Berapa kebutuhan air & protein harianku?"
- "Barusan minum 500ml" / "Tadi gym: squat 80kg 4x6"
- "Ingetin minum tiap 2 jam dari jam 8 sampai 20"
- "Buatkan program gym 4 hari seminggu"

Perintah:
/memori - lihat semua yang kuingat
/lupa <id> - hapus satu memori
/profil - profil & kebutuhan kesehatan
/pengingat - daftar pengingat aktif
/reset - hapus riwayat obrolan (memori tetap)
/id - lihat ID Telegram kamu"""


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP)


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"ID Telegram kamu: {update.effective_user.id}")


async def cmd_memori(update: Update, context: ContextTypes.DEFAULT_TYPE):
    memories = db.list_memories(update.effective_user.id)
    if not memories:
        await update.message.reply_text("Belum ada memori. Ceritakan tentang dirimu 😊")
        return
    lines, current = [], None
    for m in memories:
        if m["category"] != current:
            current = m["category"]
            lines.append(f"\n**{current.capitalize()}**")
        lines.append(f"[{m['id']}] {m['content']}")
    await send_reply(context.bot, update.effective_chat.id,
                     "🧠 Yang kuingat tentangmu:" + "\n".join(lines) + "\n\nHapus dengan /lupa <id>")


async def cmd_lupa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Pakai: /lupa <id>  (lihat id di /memori)")
        return
    ok = db.delete_memory(update.effective_user.id, int(context.args[0]))
    await update.message.reply_text("Sudah kulupakan ✅" if ok else "Memori dengan id itu tidak ada.")


async def cmd_profil(update: Update, context: ContextTypes.DEFAULT_TYPE):
    profile = db.get_health_profile(update.effective_user.id)
    if not profile:
        await update.message.reply_text("Profil kesehatan belum ada. Ceritakan jenis kelamin, tahun lahir, "
                                        "tinggi, berat, aktivitas, dan targetmu ya.")
        return
    needs = health.compute_needs(profile)
    lines = ["🩺 **Profil kesehatan**"] + [f"- {k.replace('_', ' ')}: {v}" for k, v in profile.items()]
    if "error" not in needs:
        lines += [
            "", "📊 **Kebutuhan harian (estimasi)**",
            f"- BMI: {needs['bmi']} ({needs['kategori_bmi']})",
            f"- Kalori target: {needs['target_kalori_kkal']} kkal (TDEE {needs['tdee_kkal']})",
            f"- Protein: {needs['protein_gram_per_hari'][0]}-{needs['protein_gram_per_hari'][1]} g",
            f"- Air: ±{needs['air_ml_per_hari_dasar']} ml (+500-750 ml per jam olahraga)",
        ]
    else:
        lines += ["", f"Data kurang untuk hitung kebutuhan: {', '.join(needs['field_kurang'])}"]
    await send_reply(context.bot, update.effective_chat.id, "\n".join(lines))


async def cmd_pengingat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reminders = db.list_reminders(update.effective_user.id)
    if not reminders:
        await update.message.reply_text("Tidak ada pengingat aktif.")
        return
    day_names = {1: "Sen", 2: "Sel", 3: "Rab", 4: "Kam", 5: "Jum", 6: "Sab", 7: "Min"}
    lines = ["⏰ **Pengingat aktif**"]
    for r in reminders:
        if r["kind"] == "sekali":
            when = datetime.fromisoformat(r["run_at"]).strftime("%d %b %Y %H:%M")
        else:
            days = json.loads(r["days"]) if r["days"] else []
            when = f"tiap {', '.join(day_names[d] for d in days) if days else 'hari'} jam {r['time_of_day']}"
        lines.append(f"[{r['id']}] {when} - {r['message']}")
    lines.append("\nMinta aku hapus, mis. \"hapus pengingat 3\".")
    await send_reply(context.bot, update.effective_chat.id, "\n".join(lines))


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db.clear_history(update.effective_user.id)
    await update.message.reply_text("Riwayat obrolan dihapus. Memori jangka panjang tetap tersimpan 👍")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def post_init(app: Application) -> None:
    count = 0
    for r in db.all_active_reminders():
        schedule_reminder(app, r)
        count += 1
    log.info("Bot siap. %d pengingat dijadwalkan. Model: %s via %s", count, config.AI_MODEL, config.AI_BASE_URL)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Unhandled error", exc_info=context.error)


def main() -> None:
    config.validate()
    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).post_init(post_init).build()

    owner = filters.User(user_id=config.ALLOWED_USER_IDS)
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("start", cmd_start, filters=owner))
    app.add_handler(CommandHandler("help", cmd_start, filters=owner))
    app.add_handler(CommandHandler("memori", cmd_memori, filters=owner))
    app.add_handler(CommandHandler("lupa", cmd_lupa, filters=owner))
    app.add_handler(CommandHandler("profil", cmd_profil, filters=owner))
    app.add_handler(CommandHandler("pengingat", cmd_pengingat, filters=owner))
    app.add_handler(CommandHandler("reset", cmd_reset, filters=owner))
    app.add_handler(MessageHandler(owner & filters.TEXT & ~filters.COMMAND, on_text, block=False))
    app.add_handler(MessageHandler(owner & filters.PHOTO, on_photo, block=False))
    app.add_handler(MessageHandler(owner & filters.Document.ALL, on_document, block=False))
    app.add_handler(MessageHandler(owner & (filters.VOICE | filters.AUDIO | filters.VIDEO), on_unsupported))
    app.add_handler(MessageHandler(~owner, on_stranger))
    app.add_error_handler(on_error)

    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)


if __name__ == "__main__":
    main()
