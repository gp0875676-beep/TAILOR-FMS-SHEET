import os
import logging
import asyncio
from datetime import datetime

from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters
)

from app.config import settings
from app.db import get_session
from app.validator import validate_extension
from app.pipeline import process_upload, ProcessingError
from app.models import Upload, RecordSnapshot, Anomaly

logger = logging.getLogger("fms_bot")

# simple in-process lock so two uploads never process concurrently (section 41)
_processing_lock = asyncio.Lock()


def _is_authorized(update: Update) -> bool:
    user_id = str(update.effective_user.id) if update.effective_user else ""
    chat_id = str(update.effective_chat.id) if update.effective_chat else ""
    if settings.AUTHORIZED_USER_IDS and user_id in settings.AUTHORIZED_USER_IDS:
        return True
    if settings.AUTHORIZED_CHAT_IDS and chat_id in settings.AUTHORIZED_CHAT_IDS:
        return True
    return False


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        await update.message.reply_text("❌ Unauthorized.")
        return
    await update.message.reply_text(
        "🤖 FMS Bot ready.\nSend me the FMS Excel file (.xlsx) to process an upload.\n"
        "Commands: /status /pending /urgent /overdue /summary /lastupload /health /rules"
    )


async def cmd_health(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        await update.message.reply_text("❌ Unauthorized.")
        return
    await update.message.reply_text("✅ Bot is online.")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        await update.message.reply_text("❌ Unauthorized.")
        return
    session = get_session()
    try:
        last_upload = session.query(Upload).order_by(Upload.id.desc()).first()
        pending = session.query(RecordSnapshot).filter(
            RecordSnapshot.status == "PENDING", RecordSnapshot.is_removed == False  # noqa: E712
        ).count()

        if not last_upload:
            await update.message.reply_text("🤖 FMS BOT STATUS\n\nBot: ONLINE\nNo uploads yet.")
            return

        text = (
            "🤖 FMS BOT STATUS\n\n"
            "Bot: ONLINE\n"
            f"Last Excel: {last_upload.upload_timestamp}\n"
            f"Last File: {last_upload.filename}\n"
            f"Rows: {last_upload.row_count}\n"
            f"Last Processing: {last_upload.processing_status}\n"
            f"Pending: {pending}\n"
            "Database: CONNECTED\n"
            f"Last Error: {last_upload.errors or 'None'}"
        )
        await update.message.reply_text(text)
    finally:
        session.close()


async def cmd_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _stage_list(update, status_filter="PENDING")


async def cmd_urgent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        await update.message.reply_text("❌ Unauthorized.")
        return
    session = get_session()
    try:
        rows = session.query(RecordSnapshot).filter(
            RecordSnapshot.status == "PENDING",
            RecordSnapshot.slip_type == "Urgent",
            RecordSnapshot.is_removed == False,  # noqa: E712
        ).limit(30).all()
        if not rows:
            await update.message.reply_text("No urgent pending items.")
            return
        lines = [f"🚨 URGENT PENDING ({len(rows)} shown, max 30)", ""]
        for r in rows:
            lines.append(f"Slip {r.slip_no} — {r.stage}")
        await update.message.reply_text("\n".join(lines))
    finally:
        session.close()


async def cmd_overdue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        await update.message.reply_text("❌ Unauthorized.")
        return
    from app.models import AlertHistory
    session = get_session()
    try:
        rows = session.query(AlertHistory).filter_by(alert_stage="OVERDUE", status="ACTIVE").limit(30).all()
        if not rows:
            await update.message.reply_text("No active overdue alerts.")
            return
        lines = [f"🔴 OVERDUE ({len(rows)} shown, max 30)", ""]
        for r in rows:
            lines.append(f"{r.record_id} — rule {r.rule_id}")
        await update.message.reply_text("\n".join(lines))
    finally:
        session.close()


async def _stage_list(update: Update, status_filter: str):
    if not _is_authorized(update):
        await update.message.reply_text("❌ Unauthorized.")
        return
    session = get_session()
    try:
        rows = session.query(RecordSnapshot).filter(
            RecordSnapshot.status == status_filter, RecordSnapshot.is_removed == False  # noqa: E712
        ).limit(30).all()
        if not rows:
            await update.message.reply_text("Nothing to show.")
            return
        lines = [f"⏳ {status_filter} ({len(rows)} shown, max 30)", ""]
        for r in rows:
            lines.append(f"Slip {r.slip_no} — {r.stage} ({r.slip_type})")
        await update.message.reply_text("\n".join(lines))
    finally:
        session.close()


async def cmd_lastupload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        await update.message.reply_text("❌ Unauthorized.")
        return
    session = get_session()
    try:
        u = session.query(Upload).order_by(Upload.id.desc()).first()
        if not u:
            await update.message.reply_text("No uploads yet.")
            return
        await update.message.reply_text(
            f"📁 Last Upload\n\nFile: {u.filename}\nAt: {u.upload_timestamp}\n"
            f"Rows: {u.row_count}\nStatus: {u.processing_status} ({u.validation_status})"
        )
    finally:
        session.close()


async def cmd_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        await update.message.reply_text("❌ Unauthorized.")
        return
    from app.config import load_rules_config
    cfg = load_rules_config()
    lines = ["📋 Active Rules", ""]
    for r in cfg["rules"]:
        if r.get("enabled", True):
            lines.append(f"{r['id']}: {r['name']}")
    await update.message.reply_text("\n".join(lines))


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        await update.message.reply_text("❌ Unauthorized upload.")
        return

    doc = update.message.document
    if not doc or not validate_extension(doc.file_name):
        await update.message.reply_text("❌ Please send a .xlsx or .xlsm file.")
        return

    if _processing_lock.locked():
        await update.message.reply_text("⏳ Another upload is currently being processed. Please wait and retry shortly.")
        return

    async with _processing_lock:
        os.makedirs(settings.TEMP_DIR, exist_ok=True)
        local_path = os.path.join(settings.TEMP_DIR, f"{doc.file_unique_id}_{doc.file_name}")

        try:
            tg_file = await doc.get_file()
            await tg_file.download_to_drive(local_path)

            await update.message.reply_text(f"📥 Received {doc.file_name}, processing...")

            session = get_session()

            def send_fn(chat_id, text):
                asyncio.create_task(context.bot.send_message(chat_id=chat_id, text=text))

            try:
                process_upload(
                    session=session,
                    file_path=local_path,
                    filename=doc.file_name,
                    telegram_user_id=str(update.effective_user.id),
                    telegram_chat_id=str(update.effective_chat.id),
                    send_fn=send_fn,
                )
            except ProcessingError as pe:
                await update.message.reply_text(pe.user_message)
                logger.exception("Processing failed")
            finally:
                session.close()

        finally:
            if os.path.exists(local_path):
                os.remove(local_path)  # temp file cleanup -- DB is the persistent record


def build_app() -> Application:
    if not settings.TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

    application = Application.builder().token(settings.TELEGRAM_BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("health", cmd_health))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("pending", cmd_pending))
    application.add_handler(CommandHandler("urgent", cmd_urgent))
    application.add_handler(CommandHandler("overdue", cmd_overdue))
    application.add_handler(CommandHandler("lastupload", cmd_lastupload))
    application.add_handler(CommandHandler("rules", cmd_rules))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    return application
