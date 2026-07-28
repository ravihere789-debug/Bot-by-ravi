import asyncio
import io
import logging
import os

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes

from storage import get_all_user_ids, get_user, OWNER_ID

logger = logging.getLogger(__name__)

API_ID   = int(os.environ.get("PYROGRAM_API_ID",   "0"))
API_HASH = os.environ.get("PYROGRAM_API_HASH", "")

VERIFY_WORKERS = 5   # concurrent pyrogram connections


def _extract_country_code(phone: str) -> str:
    """Best-effort country code extraction from E.164 phone number."""
    if not phone:
        return "N/A"
    digits = phone.lstrip("+")
    # Try lengths 1-3 — return the raw prefix for display
    for length in (3, 2, 1):
        prefix = digits[:length]
        if prefix:
            return f"+{prefix}"
    return "N/A"


# Pyrogram RPC error names that definitively mean the session is dead.
_EXPIRED_ERRORS = {
    "AUTH_KEY_UNREGISTERED",
    "AUTH_KEY_INVALID",
    "AUTH_KEY_DUPLICATED",
    "SESSION_EXPIRED",
    "SESSION_REVOKED",
    "USER_DEACTIVATED",
    "USER_DEACTIVATED_BAN",
    # Frozen accounts — Telegram returns 420 FROZEN_METHOD_INVALID
    "FROZEN_METHOD_INVALID",
    # Banned accounts
    "ACCOUNT_BANNED",
    "USER_BANNED",
}

# STATUS constants
STATUS_ACTIVE     = "active"
STATUS_EXPIRED    = "expired"
STATUS_UNVERIFIED = "unverified"


_verify_counter = 0

async def _verify_session(session_string: str) -> tuple[str, dict]:
    """
    Try to connect with Pyrogram, retrying up to 3 times on timeout/network errors.
    Returns (status, info_dict) where status is one of:
      STATUS_ACTIVE     – connected OK and passed all probes
      STATUS_EXPIRED    – definitive Telegram auth error (frozen / banned / revoked)
      STATUS_UNVERIFIED – could not verify after all retries (truly unreachable)
    info_dict keys: name, username, phone, tg_id, error
    """
    global _verify_counter
    if not session_string or len(session_string) < 20 or " " in session_string:
        return STATUS_EXPIRED, {"error": "invalid/empty session string"}

    import sys

    RETRIES = 3
    last_err = "could not connect"

    for attempt in range(1, RETRIES + 1):
        # Each attempt gets its own unique client name so Pyrogram
        # never reuses a stale internal connection key.
        _verify_counter += 1
        client_name = f"shc_{_verify_counter}"

        async def _do():
            old_stdin = sys.stdin
            sys.stdin = open(os.devnull, "r")
            try:
                from pyrogram import Client
                async with Client(
                    name=client_name,
                    api_id=API_ID,
                    api_hash=API_HASH,
                    session_string=session_string,
                    no_updates=True,
                    in_memory=True,
                ) as c:
                    me = await c.get_me()
                    full = (me.first_name or "") + (" " + me.last_name if me.last_name else "")
                    info = {
                        "name":     full.strip(),
                        "username": f"@{me.username}" if me.username else "",
                        "phone":    f"+{me.phone_number}" if me.phone_number else "",
                        "tg_id":   str(me.id) if me.id else "",
                        "error":   "",
                    }

                    # Deleted / deactivated accounts are definitively dead
                    if getattr(me, "is_deleted", False):
                        return STATUS_EXPIRED, {**info, "error": "account deleted/deactivated"}

                    # ── Active probe ──────────────────────────────────────────
                    # get_me() succeeds for frozen / banned / session-revoked
                    # accounts — Telegram only blocks actual interactions.
                    # Two probes so no single bypassed check causes false-active:
                    #
                    # Probe 1 — account.UpdateStatus (set online/offline presence)
                    #   Server-side write. Frozen/banned accounts raise
                    #   FROZEN_METHOD_INVALID or USER_DEACTIVATED_BAN here.
                    #
                    # Probe 2 — messages.SendMessage to Saved Messages
                    #   Belt-and-suspenders for rare edge-cases.
                    #   Message is immediately deleted — no visible trace.

                    from pyrogram import raw as _pyro_raw

                    # ── Probe 1: presence write ───────────────────────────────
                    try:
                        await c.invoke(_pyro_raw.functions.account.UpdateStatus(offline=True))
                    except Exception as probe_exc:
                        err_str = str(probe_exc)
                        probe_upper = err_str.upper().replace(" ", "_").replace("-", "_")
                        if any(k in probe_upper for k in _EXPIRED_ERRORS):
                            return STATUS_EXPIRED, {**info, "error": err_str}
                        # FloodWait / transient — don't penalise; fall through

                    # ── Probe 2: self-message write ───────────────────────────
                    try:
                        test_msg = await c.send_message("me", "\u200b")
                        try:
                            await c.delete_messages("me", test_msg.id)
                        except Exception:
                            pass
                    except Exception as probe_exc:
                        err_str = str(probe_exc)
                        probe_upper = err_str.upper().replace(" ", "_").replace("-", "_")
                        if any(k in probe_upper for k in _EXPIRED_ERRORS):
                            return STATUS_EXPIRED, {**info, "error": err_str}
                        # Transient — don't penalise

                    return STATUS_ACTIVE, info
            finally:
                try:
                    sys.stdin.close()
                except Exception:
                    pass
                sys.stdin = old_stdin

        try:
            status, info = await asyncio.wait_for(_do(), timeout=25)
            # Definitive result — return immediately, no more retries needed
            if status in (STATUS_ACTIVE, STATUS_EXPIRED):
                return status, info
            last_err = info.get("error", "unknown")
        except asyncio.TimeoutError:
            last_err = f"timed out (attempt {attempt}/{RETRIES})"
        except EOFError:
            # Pyrogram rejected the session string — definitively expired
            return STATUS_EXPIRED, {"error": "invalid session string (Pyrogram rejected it)"}
        except Exception as e:
            err_str = str(e)
            upper = err_str.upper().replace(" ", "_")
            if any(k in upper for k in _EXPIRED_ERRORS):
                return STATUS_EXPIRED, {"error": err_str}
            last_err = err_str

        # Wait before retrying so we don't hammer a busy DC
        if attempt < RETRIES:
            await asyncio.sleep(3)

    return STATUS_UNVERIFIED, {"error": f"failed after {RETRIES} attempts — {last_err}"}


async def session_health_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from storage import is_owner
    query = update.callback_query
    await query.answer()

    if not is_owner(update.effective_user.id):
        await query.answer("⛔ Owner only.", show_alert=True)
        return

    # Collect every session across all users
    all_sessions: list[dict] = []   # {uid, idx, name, username, identifier}
    for uid_str in get_all_user_ids():
        try:
            user = get_user(int(uid_str))
        except Exception:
            continue
        for idx, acc in enumerate(user.get("accounts", [])):
            identifier = acc.get("identifier", "")
            if not identifier:
                continue
            all_sessions.append({
                "uid":        uid_str,
                "idx":        idx,
                "name":       acc.get("name", f"Account {idx + 1}"),
                "username":   acc.get("username", ""),
                "identifier": identifier,
            })

    total = len(all_sessions)
    await query.edit_message_text(
        f"⏳ *Session Health Check*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🔍 Checking *{total}* sessions across all users...\n"
        f"This may take a moment. Expired ones\nwill be marked automatically.",
        parse_mode="Markdown",
    )

    if not total:
        await query.edit_message_text(
            "ℹ️ No sessions found across any users.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Owner Panel", callback_data="owner_panel", style="primary")
            ]]),
        )
        return

    # results preserved in order: list of (status, sess, info_dict)
    ordered_results: list[tuple[str, dict, dict]] = [None] * total  # type: ignore
    semaphore = asyncio.Semaphore(VERIFY_WORKERS)

    async def check_one(pos: int, sess: dict):
        async with semaphore:
            status, info = await _verify_session(sess["identifier"])
            ordered_results[pos] = (status, sess, info)

    await asyncio.gather(*[check_one(i, s) for i, s in enumerate(all_sessions)])

    # Auto-mark expired (frozen/banned/revoked) accounts as dead in storage
    # so they no longer appear active in My Accounts or get used in campaigns.
    from storage import set_account_status as _set_status
    for _res_status, _sess, _info in ordered_results:
        if _res_status == STATUS_EXPIRED:
            try:
                _set_status(int(_sess["uid"]), _sess["idx"], "dead")
            except Exception:
                pass

    def _clean(s: str, max_len: int) -> str:
        """Strip non-ASCII chars, truncate, return 'N/A' if nothing left."""
        cleaned = "".join(c for c in (s or "") if 32 <= ord(c) <= 126).strip()
        if not cleaned:
            return "N/A"
        return cleaned if len(cleaned) <= max_len else cleaned[:max_len - 1] + "…"

    def _row(num: int, sess: dict, info: dict, include_error: bool) -> str:
        # Prefer live data from Pyrogram; fall back to stored values.
        phone = info.get("phone") or sess.get("phone", "")
        if not phone:
            stored_user = sess.get("username", "")
            if stored_user and stored_user.lstrip("+").isdigit():
                phone = stored_user if stored_user.startswith("+") else "+" + stored_user

        tg_id    = _clean(info.get("tg_id")    or str(sess.get("tg_id", "")), 15)
        name     = _clean(info.get("name")     or sess.get("name",     ""), 20)
        username = _clean(info.get("username") or sess.get("username", ""), 20)
        cc       = _extract_country_code(phone) if phone else "N/A"

        uid     = sess.get("uid", "?")
        acc_idx = sess.get("idx", "?")
        parts = [
            f"{num:>4}.",
            f"UID:{uid}  Acc#{acc_idx}",
            f"TgID: {tg_id:<15}",
            f"Phone: {(phone or 'N/A'):<16}",
            f"CC: {cc:<6}",
            f"Username: {username:<20}",
            f"Name: {name}",
        ]
        if include_error:
            parts.append(f"| Error: {_clean(info.get('error', 'unknown'), 60)}")
        return "  ".join(parts)

    SEP = "=" * 120
    COL_HDR      = f"{'No.':<6}  {'UID / Acc#':<22}  {'TgID':<17}  {'Phone':<18}  {'CC':<8}  {'Username':<22}  Name"
    COL_HDR_ERR  = f"{'No.':<6}  {'UID / Acc#':<22}  {'TgID':<17}  {'Phone':<18}  {'CC':<8}  {'Username':<22}  {'Name':<22}  Error"

    active_lines:     list[str] = []
    expired_lines:    list[str] = []
    unverified_lines: list[str] = []
    a_num = e_num = u_num = 0

    for status, sess, info in ordered_results:
        if status == STATUS_ACTIVE:
            a_num += 1
            active_lines.append(_row(a_num, sess, info, include_error=False))
        elif status == STATUS_EXPIRED:
            e_num += 1
            expired_lines.append(_row(e_num, sess, info, include_error=True))
        else:  # STATUS_UNVERIFIED
            u_num += 1
            unverified_lines.append(_row(u_num, sess, info, include_error=True))

    def _build_doc(title: str, count_label: str, lines: list[str], with_error_col: bool) -> str:
        hdr = COL_HDR_ERR if with_error_col else COL_HDR
        body = "\n".join(lines) if lines else "— none —"
        return f"{title}\nGenerated by Session Health Check\n{count_label}\n{SEP}\n{hdr}\n{SEP}\n{body}\n"

    active_doc = io.BytesIO(_build_doc(
        "Active Sessions Report",
        f"Total active: {len(active_lines)}",
        active_lines, False,
    ).encode("utf-8"))
    await context.bot.send_document(
        chat_id=update.effective_chat.id,
        document=active_doc,
        filename="active_sessions.txt",
        caption=f"✅ Active Sessions — {len(active_lines)} accounts",
    )

    expired_doc = io.BytesIO(_build_doc(
        "Expired/Invalid Sessions Report",
        f"Total expired: {len(expired_lines)}",
        expired_lines, True,
    ).encode("utf-8"))
    await context.bot.send_document(
        chat_id=update.effective_chat.id,
        document=expired_doc,
        filename="expired_sessions.txt",
        caption=f"🔴 Expired Sessions — {len(expired_lines)} accounts",
    )

    unverified_doc = io.BytesIO(_build_doc(
        "Unverified Sessions Report\n(Could not connect — may be OK; re-run health check to retry)",
        f"Total unverified: {len(unverified_lines)}",
        unverified_lines, True,
    ).encode("utf-8"))
    await context.bot.send_document(
        chat_id=update.effective_chat.id,
        document=unverified_doc,
        filename="unverified_sessions.txt",
        caption=f"❓ Unverified Sessions — {len(unverified_lines)} accounts (connection issue, not removed)",
    )

    kb = []
    if expired_lines:
        kb.append([InlineKeyboardButton("🗑 Remove All Expired",    callback_data="remove_all_expired",   style="danger")])
    if unverified_lines:
        kb.append([InlineKeyboardButton("⚠️ Remove Unverified Too", callback_data="remove_all_unverified", style="danger")])
    kb.append([InlineKeyboardButton("📊 Owner Panel", callback_data="owner_panel", style="primary")])

    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=(
            f"✅ *Health Check Complete*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"✅ Active:      *{len(active_lines)}*\n"
            f"🔴 Expired:     *{len(expired_lines)}*\n"
            f"❓ Unverified:  *{len(unverified_lines)}* _(connection issue — not removed)_\n"
            f"📦 Total:       *{total}*"
        ),
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown",
    )


async def remove_all_unverified(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Remove every account that could not be verified (connection issues) across all users."""
    from storage import is_owner, remove_account
    query = update.callback_query
    await query.answer()

    if not is_owner(update.effective_user.id):
        await query.answer("⛔ Owner only.", show_alert=True)
        return

    await query.edit_message_text(
        "⚠️ *Removing all unverified sessions...*",
        parse_mode="Markdown",
    )

    removed = 0
    semaphore = asyncio.Semaphore(VERIFY_WORKERS)

    for uid_str in get_all_user_ids():
        try:
            user = get_user(int(uid_str))
        except Exception:
            continue

        accounts = user.get("accounts", [])
        results: dict[int, str] = {}
        lock = asyncio.Lock()

        async def check_u(idx: int, acc: dict):
            identifier = acc.get("identifier", "")
            if not identifier:
                async with lock:
                    results[idx] = STATUS_EXPIRED
                return
            async with semaphore:
                status, _ = await _verify_session(identifier)
            async with lock:
                results[idx] = status

        await asyncio.gather(*[check_u(i, a) for i, a in enumerate(accounts)])

        to_remove = sorted(
            [i for i, s in results.items() if s in (STATUS_EXPIRED, STATUS_UNVERIFIED)],
            reverse=True
        )
        for idx in to_remove:
            remove_account(int(uid_str), idx)
            removed += 1

    await query.edit_message_text(
        f"✅ *Done!*  Removed *{removed}* unverified session(s) across all users.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("📊 Owner Panel", callback_data="owner_panel", style="primary")
        ]]),
        parse_mode="Markdown",
    )


async def remove_all_expired(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Remove every account that fails session verification across all users."""
    from storage import is_owner
    query = update.callback_query
    await query.answer()

    if not is_owner(update.effective_user.id):
        await query.answer("⛔ Owner only.", show_alert=True)
        return

    await query.edit_message_text(
        "🗑 *Removing all expired sessions...*",
        parse_mode="Markdown",
    )

    removed = 0
    semaphore = asyncio.Semaphore(VERIFY_WORKERS)

    for uid_str in get_all_user_ids():
        try:
            user = get_user(int(uid_str))
        except Exception:
            continue

        accounts = user.get("accounts", [])
        to_remove: list[int] = []

        results: dict[int, str] = {}
        lock = asyncio.Lock()

        async def check(idx: int, acc: dict):
            identifier = acc.get("identifier", "")
            if not identifier:
                async with lock:
                    results[idx] = STATUS_EXPIRED
                return
            async with semaphore:
                status, _ = await _verify_session(identifier)
            async with lock:
                results[idx] = status

        await asyncio.gather(*[check(i, a) for i, a in enumerate(accounts)])

        # Only remove definitively expired sessions — leave unverified ones alone
        to_remove = sorted(
            [i for i, s in results.items() if s == STATUS_EXPIRED], reverse=True
        )
        for idx in to_remove:
            from storage import remove_account
            remove_account(int(uid_str), idx)
            removed += 1

    await query.edit_message_text(
        f"✅ *Done!*  Removed *{removed}* expired session(s) across all users.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("📊 Owner Panel", callback_data="owner_panel", style="primary")
        ]]),
        parse_mode="Markdown",
    )
