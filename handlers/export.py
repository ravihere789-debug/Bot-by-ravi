import csv
import io
from datetime import datetime
from telegram import Update, InputFile
from telegram.ext import ContextTypes
from storage import get_accounts, get_campaigns, get_schedules, get_broadcasts
from keyboards import export_menu, back_button


async def export_home(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id

    accounts = get_accounts(user_id)
    campaigns = get_campaigns(user_id)
    schedules = get_schedules(user_id)
    broadcasts = get_broadcasts(user_id)

    text = (
        "📤 *EXPORT DATA*\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Download your data as a CSV file.\n\n"
        f"📦 Accounts: *{len(accounts)}* records\n"
        f"🚀 Campaigns: *{len(campaigns)}* records\n"
        f"⏰ Schedules: *{len(schedules)}* records\n"
        f"📢 Broadcasts: *{len(broadcasts)}* records\n\n"
        "Choose what to export:"
    )
    await query.edit_message_text(text, reply_markup=export_menu(), parse_mode="Markdown")


async def export_accounts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer("Generating CSV...", show_alert=False)
    user_id = update.effective_user.id
    accounts = get_accounts(user_id)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Name", "Username", "Status", "Added"])
    if not accounts:
        writer.writerow(["No accounts yet", "", "", ""])
    else:
        for acc in accounts:
            writer.writerow([
                acc.get("name", ""),
                acc.get("username", ""),
                acc.get("status", "active"),
                acc.get("added", ""),
            ])

    filename = f"accounts_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    buf.seek(0)
    file_bytes = buf.getvalue().encode("utf-8")

    await query.edit_message_text(
        f"✅ *Accounts Export Ready*\n\n{len(accounts)} record(s) exported.",
        reply_markup=back_button("export_home"),
        parse_mode="Markdown"
    )
    await context.bot.send_document(
        chat_id=user_id,
        document=InputFile(io.BytesIO(file_bytes), filename=filename),
        caption=f"📦 Accounts — {len(accounts)} record(s)\n📅 {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    )


async def export_campaigns(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer("Generating CSV...", show_alert=False)
    user_id = update.effective_user.id
    campaigns = get_campaigns(user_id)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Name", "Target", "Message", "Status", "Actions", "Created"])
    if not campaigns:
        writer.writerow(["No campaigns yet", "", "", "", "", ""])
    else:
        for camp in campaigns:
            writer.writerow([
                camp.get("name", ""),
                camp.get("target", ""),
                camp.get("message", ""),
                "Active" if camp.get("active") else "Paused",
                camp.get("actions", 0),
                camp.get("created", ""),
            ])

    filename = f"campaigns_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    buf.seek(0)
    file_bytes = buf.getvalue().encode("utf-8")

    await query.edit_message_text(
        f"✅ *Campaigns Export Ready*\n\n{len(campaigns)} record(s) exported.",
        reply_markup=back_button("export_home"),
        parse_mode="Markdown"
    )
    await context.bot.send_document(
        chat_id=user_id,
        document=InputFile(io.BytesIO(file_bytes), filename=filename),
        caption=f"🚀 Campaigns — {len(campaigns)} record(s)\n📅 {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    )


async def export_schedules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer("Generating CSV...", show_alert=False)
    user_id = update.effective_user.id
    schedules = get_schedules(user_id)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Name", "Time", "Action", "Created"])
    if not schedules:
        writer.writerow(["No schedules yet", "", "", ""])
    else:
        for sch in schedules:
            writer.writerow([
                sch.get("name", ""),
                sch.get("time", ""),
                sch.get("action", ""),
                sch.get("created", ""),
            ])

    filename = f"schedules_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    buf.seek(0)
    file_bytes = buf.getvalue().encode("utf-8")

    await query.edit_message_text(
        f"✅ *Schedules Export Ready*\n\n{len(schedules)} record(s) exported.",
        reply_markup=back_button("export_home"),
        parse_mode="Markdown"
    )
    await context.bot.send_document(
        chat_id=user_id,
        document=InputFile(io.BytesIO(file_bytes), filename=filename),
        caption=f"⏰ Schedules — {len(schedules)} record(s)\n📅 {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    )


async def export_broadcasts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer("Generating CSV...", show_alert=False)
    user_id = update.effective_user.id
    broadcasts = get_broadcasts(user_id)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Date", "Target Label", "Sent To", "Recipients", "Message"])
    if not broadcasts:
        writer.writerow(["No broadcasts yet", "", "", "", ""])
    else:
        for b in broadcasts:
            writer.writerow([
                b.get("date", ""),
                b.get("target_label", ""),
                b.get("sent_to", 0),
                ", ".join(b.get("targets", [])),
                b.get("message", ""),
            ])

    filename = f"broadcasts_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    buf.seek(0)
    file_bytes = buf.getvalue().encode("utf-8")

    await query.edit_message_text(
        f"✅ *Broadcast History Export Ready*\n\n{len(broadcasts)} record(s) exported.",
        reply_markup=back_button("export_home"),
        parse_mode="Markdown"
    )
    await context.bot.send_document(
        chat_id=user_id,
        document=InputFile(io.BytesIO(file_bytes), filename=filename),
        caption=f"📢 Broadcast History — {len(broadcasts)} record(s)\n📅 {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    )


async def export_run_logs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Export all campaign run logs as a CSV."""
    query = update.callback_query
    await query.answer("Generating CSV...", show_alert=False)
    user_id = update.effective_user.id
    campaigns = get_campaigns(user_id)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Campaign", "Timestamp", "Done", "Failed", "Elapsed"])
    total_rows = 0
    for camp in campaigns:
        camp_name = camp.get("name", "")
        for r in camp.get("run_log", []):
            writer.writerow([
                camp_name,
                r.get("ts", ""),
                r.get("done", 0),
                r.get("failed", 0),
                r.get("elapsed", ""),
            ])
            total_rows += 1
    if total_rows == 0:
        writer.writerow(["No run logs yet", "", "", "", ""])

    filename = f"run_logs_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    buf.seek(0)
    file_bytes = buf.getvalue().encode("utf-8")

    await query.edit_message_text(
        f"✅ *Run Logs Export Ready*\n\n{total_rows} run record(s) across {len(campaigns)} campaign(s).",
        reply_markup=back_button("export_home"),
        parse_mode="Markdown",
    )
    await context.bot.send_document(
        chat_id=user_id,
        document=InputFile(io.BytesIO(file_bytes), filename=filename),
        caption=f"📈 Campaign Run Logs — {total_rows} record(s)\n📅 {datetime.now().strftime('%Y-%m-%d %H:%M')}",
    )


async def export_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer("Generating full export...", show_alert=False)
    user_id = update.effective_user.id

    accounts = get_accounts(user_id)
    campaigns = get_campaigns(user_id)
    schedules = get_schedules(user_id)
    broadcasts = get_broadcasts(user_id)

    buf = io.StringIO()
    writer = csv.writer(buf)

    writer.writerow(["=== ACCOUNTS ==="])
    writer.writerow(["Name", "Username", "Status", "Added"])
    for acc in accounts:
        writer.writerow([acc.get("name", ""), acc.get("username", ""), acc.get("status", ""), acc.get("added", "")])
    if not accounts:
        writer.writerow(["No accounts"])

    writer.writerow([])
    writer.writerow(["=== CAMPAIGNS ==="])
    writer.writerow(["Name", "Target", "Message", "Status", "Actions", "Created"])
    for camp in campaigns:
        writer.writerow([
            camp.get("name", ""), camp.get("target", ""), camp.get("message", ""),
            "Active" if camp.get("active") else "Paused", camp.get("actions", 0), camp.get("created", "")
        ])
    if not campaigns:
        writer.writerow(["No campaigns"])

    writer.writerow([])
    writer.writerow(["=== SCHEDULES ==="])
    writer.writerow(["Name", "Time", "Action", "Created"])
    for sch in schedules:
        writer.writerow([sch.get("name", ""), sch.get("time", ""), sch.get("action", ""), sch.get("created", "")])
    if not schedules:
        writer.writerow(["No schedules"])

    writer.writerow([])
    writer.writerow(["=== BROADCAST HISTORY ==="])
    writer.writerow(["Date", "Target Label", "Sent To", "Recipients", "Message"])
    for b in broadcasts:
        writer.writerow([
            b.get("date", ""), b.get("target_label", ""), b.get("sent_to", 0),
            ", ".join(b.get("targets", [])), b.get("message", "")
        ])
    if not broadcasts:
        writer.writerow(["No broadcasts"])

    filename = f"full_export_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    buf.seek(0)
    file_bytes = buf.getvalue().encode("utf-8")

    total = len(accounts) + len(campaigns) + len(schedules) + len(broadcasts)
    await query.edit_message_text(
        f"✅ *Full Export Ready*\n\n{total} total records across all sections.",
        reply_markup=back_button("export_home"),
        parse_mode="Markdown"
    )
    await context.bot.send_document(
        chat_id=user_id,
        document=InputFile(io.BytesIO(file_bytes), filename=filename),
        caption=(
            f"📤 Full Export\n"
            f"📦 Accounts: {len(accounts)}  🚀 Campaigns: {len(campaigns)}\n"
            f"⏰ Schedules: {len(schedules)}  📢 Broadcasts: {len(broadcasts)}\n"
            f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        )
    )
