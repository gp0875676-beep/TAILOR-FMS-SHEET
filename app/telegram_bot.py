import os
import logging
import asyncio
from datetime import datetime

from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters
)
from telegram.request import HTTPXRequest

from app.config import settings, business_now
from app.db import get_session
from app.validator import validate_extension
from app.pipeline import process_upload, ProcessingError
from app.models import Upload, RecordSnapshot, Anomaly
from app.scheduler import evaluate_pending_records
from app.alert_engine import mark_alert_failed
from app.message_renderer import render_full_list

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


def _is_saree_item(item_name) -> bool:
    """Condition 6 (30-Sep-2026): defines the SAREE category carved out of
    the whole FMS into its own /saree command. Confirmed with user:
    -- Matches any item whose name STARTS WITH the word SAREE, whatever the
       exact type (SAREE, SAREE STITCH, SAREE BLOUSE SET, SAREE(NO LESS)...).
    -- ALSO matches BLOUSE STITCH specifically (goes together with sarees).
    -- Explicitly does NOT match RMN-D.SAREE, RM SAREE, or any other
       RMN/RM-prefixed variant -- those are Ready-Made items, not "real"
       sarees for this purpose, per user's explicit correction. Since the
       match is "starts with SAREE", these are naturally excluded already
       (they start with RMN/RM, not SAREE) -- no extra exclusion needed."""
    if not item_name:
        return False
    name = str(item_name).strip().upper()
    if name == "BLOUSE STITCH":
        return True
    return name.startswith("SAREE")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        await update.message.reply_text("❌ Unauthorized.")
        return
    await update.message.reply_text(
        "🤖 FMS Bot ready.\nSend me the FMS Excel file (.xlsx) to process an upload.\n"
        "Commands: /status /pending /urgent /overdue /tailor /packing /order /saree "
        "/summary /lastupload /health /rules /anomalies"
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


async def cmd_tailor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Confirmed with user 24-Aug-2026 (Condition 2 revision): replaces
    RULE_002's 3 deleted individual alerts. Shows every piece currently
    PENDING at the TAILOR stage (received by tailor, not yet TAILOR COMPLETE)
    whose ALTERATION SLIP DATE is within the last RECENT_ALERT_WINDOW_DAYS
    (default 7) -- no item-count cap.
    REVISED 25-Aug-2026 (Condition 5): order-tagged pieces are excluded
    entirely -- they now live only in /order.
    REVISED 30-Sep-2026 (Condition 6): SAREE-category pieces are excluded
    entirely -- they now live only in /saree."""
    if not _is_authorized(update):
        await update.message.reply_text("❌ Unauthorized.")
        return
    import json
    import pandas as pd
    from datetime import timedelta

    session = get_session()
    try:
        rows = session.query(RecordSnapshot).filter(
            RecordSnapshot.stage == "TAILOR",
            RecordSnapshot.status == "PENDING",
            RecordSnapshot.is_removed == False,  # noqa: E712
            RecordSnapshot.order_ocs.is_(None),
        ).all()
        rows = [r for r in rows if not _is_saree_item(r.item_name)]

        cutoff = business_now() - timedelta(days=settings.RECENT_ALERT_WINDOW_DAYS)
        recent_rows = []
        for r in rows:
            try:
                data = json.loads(r.raw_json) if r.raw_json else {}
                slip_date = pd.to_datetime(data.get("slip_date"), errors="coerce")
                if pd.notna(slip_date) and slip_date >= cutoff:
                    recent_rows.append(r)
            except Exception:
                continue

        if not recent_rows:
            await update.message.reply_text("No pieces currently pending at Tailor in the last 7 days.")
            return

        recent_rows.sort(key=lambda r: (r.slip_no or ""))
        lines = [f"Slip {r.slip_no} — {r.item_name}" for r in recent_rows]

        chunks = render_full_list("✂️ TAILOR — PENDING (last 7 days)", lines, total_count=len(recent_rows))
        for chunk in chunks:
            await update.message.reply_text(chunk)
    finally:
        session.close()


async def cmd_packing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Confirmed with user 25-Aug-2026 (Condition 4 revision): completely
    replaces RULE_011 (deleted, all lead-time alerts removed). Shows every
    piece currently PENDING at the PACKING stage (finishing complete, packing
    not yet complete) -- NO date filter, ALL such pieces regardless of age,
    no item-count cap.
    REVISED 25-Aug-2026 (Condition 5): order-tagged pieces are excluded
    entirely -- they now live only in /order.
    REVISED 30-Sep-2026 (Condition 6): SAREE-category pieces are excluded
    entirely -- they now live only in /saree."""
    if not _is_authorized(update):
        await update.message.reply_text("❌ Unauthorized.")
        return
    session = get_session()
    try:
        rows = session.query(RecordSnapshot).filter(
            RecordSnapshot.stage == "PACKING",
            RecordSnapshot.status == "PENDING",
            RecordSnapshot.is_removed == False,  # noqa: E712
            RecordSnapshot.order_ocs.is_(None),
        ).all()
        rows = [r for r in rows if not _is_saree_item(r.item_name)]

        if not rows:
            await update.message.reply_text("No pieces currently pending at Packing.")
            return

        rows.sort(key=lambda r: (r.slip_no or ""))
        lines = [f"Slip {r.slip_no} — {r.item_name}" for r in rows]

        chunks = render_full_list("📦 PACKING — PENDING", lines, total_count=len(rows))
        for chunk in chunks:
            await update.message.reply_text(chunk)
    finally:
        session.close()


async def cmd_urgent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """REVISED 25-Aug-2026 (Condition 5): order-tagged pieces are excluded
    entirely -- they now live only in /order.
    REVISED 30-Sep-2026 (Condition 6): SAREE-category pieces are excluded
    entirely -- they now live only in /saree."""
    if not _is_authorized(update):
        await update.message.reply_text("❌ Unauthorized.")
        return
    session = get_session()
    try:
        rows = session.query(RecordSnapshot).filter(
            RecordSnapshot.status == "PENDING",
            RecordSnapshot.slip_type == "Urgent",
            RecordSnapshot.is_removed == False,  # noqa: E712
            RecordSnapshot.order_ocs.is_(None),
        ).all()
        rows = [r for r in rows if not _is_saree_item(r.item_name)]

        if not rows:
            await update.message.reply_text("No urgent pending items.")
            return

        rows.sort(key=lambda r: (r.slip_no or ""))
        lines = [f"Slip {r.slip_no} — {r.stage}" for r in rows]

        chunks = render_full_list("🚨 URGENT PENDING", lines, total_count=len(rows))
        for chunk in chunks:
            await update.message.reply_text(chunk)
    finally:
        session.close()


async def cmd_overdue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        await update.message.reply_text("❌ Unauthorized.")
        return
    from app.models import AlertHistory
    session = get_session()
    try:
        rows = session.query(AlertHistory).filter_by(alert_stage="OVERDUE", status="ACTIVE").all()
        if not rows:
            await update.message.reply_text("No active overdue alerts.")
            return
        lines = [f"{r.record_id} — rule {r.rule_id}" for r in rows]
        chunks = render_full_list("🔴 OVERDUE", lines, total_count=len(rows))
        for chunk in chunks:
            await update.message.reply_text(chunk)
    finally:
        session.close()


async def _stage_list(update: Update, status_filter: str):
    """REVISED 25-Aug-2026 (Condition 5): order-tagged pieces are excluded
    entirely from /pending -- they now live only in /order.
    REVISED 29-Sep-2026 (user request): /pending should stop at PACKING
    COMPLETE -- a piece that has packing_complete set (stage == DELIVERY,
    still waiting only on DELIVERED TO CUSTOMER) should no longer count as
    "pending" here. Confirmed with user: packing complete is the actual
    end of the process for /pending's purposes; DELIVERY/delivered_customer
    should not keep a piece showing up. This filter is scoped to /pending
    only -- it does not touch the underlying stage/status fields used by
    RecordSnapshot elsewhere (RULE_007's upstream guard, DQ checks, /order,
    etc.), so nothing else changes behavior.
    REVISED 30-Sep-2026 (Condition 6): SAREE-category pieces (see
    _is_saree_item) are excluded entirely -- they now live only in /saree."""
    if not _is_authorized(update):
        await update.message.reply_text("❌ Unauthorized.")
        return
    session = get_session()
    try:
        rows = session.query(RecordSnapshot).filter(
            RecordSnapshot.status == status_filter,
            RecordSnapshot.is_removed == False,  # noqa: E712
            RecordSnapshot.order_ocs.is_(None),
            RecordSnapshot.stage != "DELIVERY",
        ).all()
        rows = [r for r in rows if not _is_saree_item(r.item_name)]

        if not rows:
            await update.message.reply_text("Nothing to show.")
            return

        # Confirmed with user 24-Aug-2026: no more silent 30/60-item caps --
        # show EVERY matching piece.
        rows.sort(key=lambda r: (r.slip_no or ""))
        lines = [f"Slip {r.slip_no} — {r.stage} ({r.slip_type})" for r in rows]

        chunks = render_full_list(f"⏳ {status_filter}", lines, total_count=len(rows))
        for chunk in chunks:
            await update.message.reply_text(chunk)
    finally:
        session.close()


async def cmd_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """NEW 25-Aug-2026 (Condition 5): completely replaces RULE_009 (deleted,
    no more delivery-reminder/overdue push alerts). Shows every PENDING
    order-tagged piece (ORDER OCS is set) across ANY stage -- no date filter,
    no item-count cap. Fully completed/delivered order pieces are excluded
    entirely (status == PENDING filters those out). TAILOR-stage order
    pieces are shown minimally (just Order + Slip, per user spec -- these
    are still mid-stitching and don't need a stage label); every other
    stage is shown with its stage label. These pieces are excluded from
    /pending, /urgent, /tailor and /packing -- /order is their only home.
    REVISED 30-Sep-2026 (user request, same treatment as /pending): stop at
    PACKING COMPLETE -- an order piece with packing_complete already set
    (stage == DELIVERY, only waiting on DELIVERED TO CUSTOMER) no longer
    counts as pending here either. Ready-to-pack (or earlier) is what
    matters; delivered_customer doesn't keep it showing up.
    REVISED 30-Sep-2026 (Condition 6): SAREE-category pieces are excluded
    entirely, even here -- they now live only in /saree (no overlap exists
    in the real workbook today, but this keeps the "no other report" rule
    true if it ever does)."""
    if not _is_authorized(update):
        await update.message.reply_text("❌ Unauthorized.")
        return
    session = get_session()
    try:
        rows = session.query(RecordSnapshot).filter(
            RecordSnapshot.order_ocs.isnot(None),
            RecordSnapshot.status == "PENDING",
            RecordSnapshot.is_removed == False,  # noqa: E712
            RecordSnapshot.stage != "DELIVERY",
        ).all()
        rows = [r for r in rows if not _is_saree_item(r.item_name)]

        if not rows:
            await update.message.reply_text("No pending order pieces.")
            return

        rows.sort(key=lambda r: (r.stage or "", r.slip_no or ""))
        lines = []
        for r in rows:
            if r.stage == "TAILOR":
                lines.append(f"Order {r.order_ocs} — Slip {r.slip_no}")
            else:
                lines.append(f"Order {r.order_ocs} — Slip {r.slip_no} — {r.stage}")

        chunks = render_full_list("📋 ORDER PIECES — PENDING", lines, total_count=len(rows))
        for chunk in chunks:
            await update.message.reply_text(chunk)
    finally:
        session.close()


async def cmd_saree(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """NEW 30-Sep-2026 (Condition 6): carves the whole SAREE category (see
    _is_saree_item -- any item starting with the word SAREE, plus BLOUSE
    STITCH; explicitly NOT RMN-D.SAREE / RM-prefixed variants) out of every
    other report into its own command. Shows every SAREE-category piece
    that hasn't completed up to PACKING yet -- same "ready to pack" pending
    definition as /pending and /order (status PENDING, stage != DELIVERY)
    -- no date filter, no item-count cap. These pieces are excluded
    entirely from /pending, /urgent, /tailor, /packing and /order --
    /saree is their only home."""
    if not _is_authorized(update):
        await update.message.reply_text("❌ Unauthorized.")
        return
    session = get_session()
    try:
        rows = session.query(RecordSnapshot).filter(
            RecordSnapshot.status == "PENDING",
            RecordSnapshot.is_removed == False,  # noqa: E712
            RecordSnapshot.stage != "DELIVERY",
        ).all()
        rows = [r for r in rows if _is_saree_item(r.item_name)]

        if not rows:
            await update.message.reply_text("No pending saree pieces.")
            return

        rows.sort(key=lambda r: (r.stage or "", r.slip_no or ""))
        lines = [f"Slip {r.slip_no} — {r.item_name} — {r.stage}" for r in rows]

        chunks = render_full_list("🥻 SAREE — PENDING", lines, total_count=len(rows))
        for chunk in chunks:
            await update.message.reply_text(chunk)
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


async def cmd_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Re-shows the full upload summary (same format as what gets auto-sent
    after every upload) for the most recent upload, on demand."""
    if not _is_authorized(update):
        await update.message.reply_text("❌ Unauthorized.")
        return
    session = get_session()
    try:
        u = session.query(Upload).order_by(Upload.id.desc()).first()
        if not u:
            await update.message.reply_text("No uploads yet — send me an Excel file first.")
            return

        from app.message_renderer import render_upload_summary
        summary = {
            "filename": u.filename,
            "row_count": u.row_count,
            "new_records": u.new_records or 0,
            "updated_records": u.updated_records or 0,
            "completed_records": u.completed_records or 0,
            "new_alerts": u.new_alerts or 0,
            "duplicates_suppressed": u.duplicates_suppressed or 0,
            "anomaly_count": u.anomaly_count or 0,
            "validation_status": u.validation_status,
        }
        text = render_upload_summary(summary)
        text += f"\n\n(from upload at {u.upload_timestamp}, processing: {u.processing_status})"
        await update.message.reply_text(text)
    finally:
        session.close()


async def cmd_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        await update.message.reply_text("❌ Unauthorized.")
        return
    from app.config import load_rules_config

    # Only these categories are actually evaluated by rule_engine.py -- rules
    # in other categories (PROCESS_PENDING, ESCALATION) don't currently
    # generate alerts on their own, they're informational/reserved for future
    # use. Labeling this explicitly so /rules doesn't imply they're live.
    ALERTING_CATEGORIES = {"AGENCY_TO_TAILOR_SLA", "STAGE_DEADLINE_TIERED", "MISSING_DEADLINE_ALERT", "DEADLINE"}

    cfg = load_rules_config()
    active_lines, info_lines = [], []
    for r in cfg["rules"]:
        if not r.get("enabled", True):
            continue
        line = f"{r['id']}: {r['name']}"
        if r.get("category") in ALERTING_CATEGORIES:
            active_lines.append(line)
        else:
            info_lines.append(line)

    lines = ["📋 Active Rules (generate alerts)", ""] + active_lines
    if info_lines:
        lines += ["", "ℹ️ Informational only (no alert yet)", ""] + info_lines
    await update.message.reply_text("\n".join(lines))


async def cmd_anomalies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Confirmed real gap (23-Aug-2026 audit): detect_anomalies() finds data
    quality issues (DQ_001-DQ_006) every upload and stores them, but the
    Telegram side never surfaced the details -- only a count in the upload
    summary. This gives access to the actual list on demand, without
    flooding the chat (which sending each one individually as it's detected
    would do -- can be 2000+ on a big backfill)."""
    if not _is_authorized(update):
        await update.message.reply_text("❌ Unauthorized.")
        return
    from app.models import Anomaly
    session = get_session()
    try:
        rows = session.query(Anomaly).filter_by(status="OPEN").order_by(Anomaly.id.desc()).limit(30).all()
        if not rows:
            await update.message.reply_text("No open data-quality issues.")
            return
        total_open = session.query(Anomaly).filter_by(status="OPEN").count()
        lines = [f"⚠️ DATA QUALITY ISSUES ({len(rows)} shown of {total_open} open)", ""]
        for r in rows:
            lines.append(f"[{r.dq_rule_id}] {r.description}")
        await update.message.reply_text("\n".join(lines))
    finally:
        session.close()


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

            # Collect messages instead of firing them off immediately -- sending
            # hundreds of alerts as concurrent fire-and-forget tasks (the old
            # behavior) floods Telegram's connection pool and times most of
            # them out. We queue here and send sequentially afterward instead.
            # fingerprint (record_id, rule_id, alert_stage) travels alongside
            # each alert message so a failed send can be rolled back below.
            pending_messages = []

            def send_fn(chat_id, text, fingerprint=None):
                pending_messages.append((chat_id, text, fingerprint))

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

            # Send the summary / stopped-items report first so the user gets
            # confirmation quickly, then trickle the rest (individual rule
            # alerts) at a safe pace -- Telegram throttles ~1 msg/sec per chat,
            # and a large backfill upload can generate hundreds of alerts.
            priority = [m for m in pending_messages if "UPLOAD SUMMARY" in m[1] or "STOPPED ITEMS" in m[1]]
            rest = [m for m in pending_messages if m not in priority]

            for chat_id, text, fingerprint in priority + rest:
                delivered = False
                for attempt in range(2):  # one retry on transient timeout
                    try:
                        await context.bot.send_message(chat_id=chat_id, text=text)
                        delivered = True
                        break
                    except Exception:
                        if attempt == 0:
                            await asyncio.sleep(1)
                        else:
                            logger.exception(f"Failed to deliver message to {chat_id} after retry")

                if not delivered and fingerprint is not None:
                    # Confirmed real bug 23-Aug-2026: without this rollback, a
                    # failed send was permanently marked "already alerted" in
                    # the DB and would never be retried on a future upload or
                    # periodic check -- the user would just silently never get it.
                    rollback_session = get_session()
                    try:
                        mark_alert_failed(rollback_session, *fingerprint)
                        logger.info(f"Rolled back failed alert {fingerprint} for retry")
                    finally:
                        rollback_session.close()

                await asyncio.sleep(0.35)  # stay under Telegram's per-chat rate limit

        finally:
            if os.path.exists(local_path):
                os.remove(local_path)  # temp file cleanup -- DB is the persistent record


async def periodic_check_job(context: ContextTypes.DEFAULT_TYPE):
    """Runs every TIME_BASED_CHECK_INTERVAL_MINUTES (default 5) via PTB's
    JobQueue, on the SAME asyncio event loop as the bot -- no extra thread
    needed. Re-checks every pending record's deadlines against the current
    time and sends any newly-crossed-threshold alerts, independent of
    whether a new Excel was uploaded."""
    if not settings.ENABLE_TIME_BASED_MONITORING:
        return
    if not settings.AUTHORIZED_CHAT_IDS:
        return

    session = get_session()
    try:
        results = evaluate_pending_records(session)
    finally:
        session.close()

    if not results:
        return

    if settings.DRY_RUN:
        logger.info(f"Periodic check (DRY_RUN): would send {len(results)} alert(s), skipping actual send")
        return

    logger.info(f"Periodic check: sending {len(results)} alert(s)")
    for chat_id in settings.AUTHORIZED_CHAT_IDS:
        for msg, fingerprint in results:
            delivered = False
            for attempt in range(2):
                try:
                    await context.bot.send_message(chat_id=chat_id, text=msg)
                    delivered = True
                    break
                except Exception:
                    if attempt == 0:
                        await asyncio.sleep(1)
                    else:
                        logger.exception(f"Failed to deliver periodic alert to {chat_id}")

            if not delivered:
                rollback_session = get_session()
                try:
                    mark_alert_failed(rollback_session, *fingerprint)
                    logger.info(f"Rolled back failed periodic alert {fingerprint} for retry")
                finally:
                    rollback_session.close()

            await asyncio.sleep(0.35)  # stay under Telegram's per-chat rate limit


def build_app() -> Application:
    if not settings.TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

    # Default connection pool is small (historically 1 in python-telegram-bot),
    # shared between the long-polling getUpdates() loop and every outgoing
    # send_message() call. Under a busy upload (hundreds of trickled alerts,
    # see app/telegram_bot.py's handle_document) this causes
    # "PoolTimeout: All connections in the connection pool are occupied" --
    # explicitly widen it and give some breathing room on timeouts.
    request = HTTPXRequest(
        connection_pool_size=16,
        connect_timeout=20,
        read_timeout=20,
        write_timeout=20,
        pool_timeout=20,
    )

    application = (
        Application.builder()
        .token(settings.TELEGRAM_BOT_TOKEN)
        .request(request)
        .build()
    )

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("health", cmd_health))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("pending", cmd_pending))
    application.add_handler(CommandHandler("urgent", cmd_urgent))
    application.add_handler(CommandHandler("tailor", cmd_tailor))
    application.add_handler(CommandHandler("packing", cmd_packing))
    application.add_handler(CommandHandler("order", cmd_order))
    application.add_handler(CommandHandler("saree", cmd_saree))
    application.add_handler(CommandHandler("overdue", cmd_overdue))
    application.add_handler(CommandHandler("lastupload", cmd_lastupload))
    application.add_handler(CommandHandler("summary", cmd_summary))
    application.add_handler(CommandHandler("rules", cmd_rules))
    application.add_handler(CommandHandler("anomalies", cmd_anomalies))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    if settings.ENABLE_TIME_BASED_MONITORING and application.job_queue:
        interval_seconds = settings.TIME_BASED_CHECK_INTERVAL_MINUTES * 60
        application.job_queue.run_repeating(
            periodic_check_job, interval=interval_seconds, first=30
        )
        logger.info(
            f"Periodic time-based monitoring enabled: every "
            f"{settings.TIME_BASED_CHECK_INTERVAL_MINUTES} minute(s)"
        )

    return application
