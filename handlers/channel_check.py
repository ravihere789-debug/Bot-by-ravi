from telegram import Update
from telegram.ext import ContextTypes
from storage import get_required_channels
from keyboards import force_join_menu


def _join_text(channels: list) -> str:
    channel_lines = "\n".join(f"• {c}" for c in channels)
    return (
        "*Join Required*\n\n"
        "You must join our channel(s) to use this bot.\n\n"
        f"*Channels:*\n{channel_lines}\n\n"
        "_After joining all channels, tap ✅ I Joined_"
    )


async def check_channel_membership(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Returns True if user is a member of all required channels (or none are required).
    Returns False and sends the join prompt if the user is missing any of them.
    """
    channels = get_required_channels()
    if not channels:
        return True

    user = update.effective_user
    if not user:
        return True

    missing = []
    for channel in channels:
        try:
            member = await context.bot.get_chat_member(chat_id=channel, user_id=user.id)
            if member.status not in ("member", "administrator", "creator", "restricted"):
                missing.append(channel)
        except Exception:
            continue

    if not missing:
        return True

    text = _join_text(channels)

    if update.message:
        await update.message.reply_text(text, reply_markup=force_join_menu(channels), parse_mode="Markdown")
    elif update.callback_query:
        await update.callback_query.answer("⚠️ You must join all required channels first!", show_alert=True)
        await update.callback_query.edit_message_text(text, reply_markup=force_join_menu(channels), parse_mode="Markdown")

    return False


async def check_joined_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    channels = get_required_channels()

    if not channels:
        await query.answer()
        from handlers.start import start_handler
        await start_handler(update, context)
        return

    user_id = update.effective_user.id
    missing = []
    for channel in channels:
        try:
            member = await context.bot.get_chat_member(chat_id=channel, user_id=user_id)
            if member.status not in ("member", "administrator", "creator", "restricted"):
                missing.append(channel)
        except Exception:
            continue

    if not missing:
        await query.answer("✅ Verified! Welcome!", show_alert=True)
        from handlers.start import start_handler
        await start_handler(update, context)
    else:
        await query.answer(
            f"❌ You haven't joined {', '.join(missing)} yet. Please join and try again.",
            show_alert=True
        )
