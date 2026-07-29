import re
import os
import asyncio
import random
import time as _time
from typing import Optional, Callable, Awaitable

WORKERS = 5           # parallel accounts at once
MIN_DELAY = 3.0       # min seconds between each account start (jitter)
MAX_DELAY = 6.0       # max seconds between each account start (jitter)
ACTION_PAUSE = 1.5    # brief pause between actions within one session

# Speed presets — (workers, min_delay, max_delay, action_pause)
# Lower workers + longer delays = far fewer bans.
# min_delay / max_delay control the inter-account gap HELD inside the semaphore,
# so each slot stays occupied for action_time + [min,max] seconds before the
# next account can start — not just a startup jitter.
SPEED_PRESETS = {
    # 1 account at a time, very long gaps — maximum account safety
    "slow":   {"workers": 1,  "min_delay": 25.0, "max_delay": 45.0, "action_pause": 5.0},
    # 3 concurrent, 8-18 s gap — safe for most use cases
    "normal": {"workers": 3,  "min_delay": 8.0,  "max_delay": 18.0, "action_pause": 2.5},
    # 5 concurrent, 3-7 s gap — moderate risk
    "fast":   {"workers": 5,  "min_delay": 3.0,  "max_delay": 7.0,  "action_pause": 1.5},
    # 8 concurrent, 1.5-4 s gap — higher risk, use on aged/trusted accounts only
    "ultra":  {"workers": 8,  "min_delay": 1.5,  "max_delay": 4.0,  "action_pause": 1.0},
    # Balanced safe default: 4 concurrent with human-like jitter
    "smart":  {"workers": 4,  "min_delay": 6.0,  "max_delay": 14.0, "action_pause": 2.5},
}

# Pyrogram credentials — loaded once at import time
_API_ID   = int(os.environ.get("PYROGRAM_API_ID", "0") or "0")
_API_HASH = os.environ.get("PYROGRAM_API_HASH", "") or ""

# Pyrogram RPC error names that mean the session is permanently dead.
# Frozen and banned accounts are treated the same as expired sessions.
_FATAL_ERRORS = {
    "AUTH_KEY_UNREGISTERED",
    "AUTH_KEY_INVALID",
    "AUTH_KEY_DUPLICATED",
    "SESSION_EXPIRED",
    "SESSION_REVOKED",
    "USER_DEACTIVATED",
    "USER_DEACTIVATED_BAN",
    "FROZEN_METHOD_INVALID",   # frozen account
    "ACCOUNT_BANNED",          # banned account
    "USER_BANNED",             # banned account
}

def _is_fatal_error(exc: Exception) -> bool:
    """Return True if the exception is a permanent session/account death."""
    upper = str(exc).upper().replace(" ", "_")
    return any(k in upper for k in _FATAL_ERRORS)


def parse_post_url(url: str) -> tuple[Optional[str], Optional[int]]:
    url = url.strip()
    # Accept both t.me and telegram.me
    m = re.match(r"https?://(?:t|telegram)\.me/c/(\d+)/(\d+)", url)
    if m:
        return str(int("-100" + m.group(1))), int(m.group(2))
    m = re.match(r"https?://(?:t|telegram)\.me/([a-zA-Z0-9_]+)/(\d+)", url)
    if m:
        return "@" + m.group(1), int(m.group(2))
    return None, None


def normalize_invite_link(link: str) -> str:
    """
    Ensure invite links are in the full-URL form that Pyrogram's INVITE_LINK_RE
    requires.  Pyrogram matches ^(https?://)?(www\\.)?(t|telegram)\\.(org|me|dog)/
    (joinchat/|+)(hash)$ — a bare '+hash' does NOT match, so join_chat() falls
    through to resolve_peer() and raises PeerIdInvalid.

    Converts:
        +AbCdEfGh          →  https://t.me/+AbCdEfGh
        AbCdEfGh (bare)    →  https://t.me/+AbCdEfGh   (if looks like hash)
        full URL            →  unchanged
    """
    link = link.strip()
    if any(pat in link for pat in ("t.me/joinchat/", "t.me/+", "telegram.me/joinchat/", "telegram.me/+")):
        return link  # already a full URL
    if link.startswith("+") and len(link) > 5 and not link.lstrip("+").isdigit():
        return "https://t.me/" + link  # +Hash → full URL
    return link


def parse_channel(target: str) -> str:
    """
    Normalise a user-supplied channel identifier into something Pyrogram accepts.

    Supported inputs
    ----------------
    Public  : @username  |  https://t.me/username
    Private : https://t.me/+HASH  |  https://t.me/joinchat/HASH  |  +HASH
    Numeric : -1001234567890  |  1234567890
    """
    target = target.strip()

    # ── Private invite links ──────────────────────────────────────────────────
    # Always normalise to full URL so Pyrogram's INVITE_LINK_RE matches.
    if any(pat in target for pat in ("t.me/joinchat/", "t.me/+", "telegram.me/joinchat/", "telegram.me/+")):
        return target                      # already full URL — pass through
    if target.startswith("+") and len(target) > 5 and not target.lstrip("+").isdigit():
        return "https://t.me/" + target    # bare +Hash → full URL Pyrogram needs

    # ── Numeric channel / chat ID ─────────────────────────────────────────────
    try:
        int(target)                        # raises if not numeric
        return target                      # keep as string; caller converts to int
    except ValueError:
        pass

    # ── Public URL  ──────────────────────────────────────────────────────────
    if target.startswith("https://t.me/"):
        slug = target.replace("https://t.me/", "").split("/")[0]
        return "@" + slug

    # ── Bare username ─────────────────────────────────────────────────────────
    return target if target.startswith("@") else "@" + target


def progress_bar(done: int, total: int, width: int = 12) -> str:
    filled = int(width * done / total) if total > 0 else 0
    bar = "█" * filled + "░" * (width - filled)
    pct = int(100 * done / total) if total > 0 else 0
    return f"[{bar}] {pct}%"


async def run_campaign_on_account(session_string: str, action: str, target: str, message: str = "", reactions: list = None, action_pause: float = ACTION_PAUSE, join_link: str = "", button_index: int = 0) -> dict:
    try:
        from pyrogram import Client
        from pyrogram.enums import ChatType
        from pyrogram.errors import (
            FloodWait, UserAlreadyParticipant, UserNotParticipant,
            ChannelInvalid, UsernameInvalid, MessageIdInvalid, PeerIdInvalid,
        )
    except ImportError:
        return {"ok": False, "detail": "pyrogram not installed"}

    if not _API_ID or not _API_HASH:
        return {"ok": False, "detail": "PYROGRAM_API_ID / PYROGRAM_API_HASH not set"}

    client = Client(
        "runner",
        api_id=_API_ID,
        api_hash=_API_HASH,
        session_string=session_string,
        no_updates=True,
        in_memory=True,
    )

    # ── Connect (12 s hard limit) ─────────────────────────────────────────────
    try:
        await asyncio.wait_for(client.start(), timeout=12)
    except asyncio.TimeoutError:
        asyncio.ensure_future(client.stop())
        return {"ok": False, "detail": "connect timed out"}
    except UnicodeDecodeError:
        # Pyrogram decodes the session string as utf-16-le internally.
        # This can be transient (DC hiccup / momentary overload) so do NOT
        # mark the account dead — just report the failure and let it retry.
        return {"ok": False, "detail": "session decode error (transient, will retry)"}
    except Exception as e:
        if _is_fatal_error(e):
            return {"ok": False, "detail": str(e)[:80], "expired": True}
        return {"ok": False, "detail": str(e)[:80]}

    # ── Run the action (15 s hard limit), always stop client after ───────────
    async def _do_action() -> dict:
        try:
            # Auto-join via join_link before any action
            if join_link:
                _jl = normalize_invite_link(join_link)
                # Extract the raw invite hash (needed for CheckChatInvite fallback)
                _jl_hash_m = client.INVITE_LINK_RE.match(_jl)
                _jl_hash = _jl_hash_m.group(1) if _jl_hash_m else None
                try:
                    await client.join_chat(_jl)
                except UserAlreadyParticipant:
                    # Account is already a member but the in-memory session has
                    # an empty peer cache — resolve_peer() will fail with
                    # PeerIdInvalid when we try to react/vote/view.
                    # Fix: call CheckChatInvite which returns the Channel object
                    # (with access_hash) even for existing members, then store
                    # it so resolve_peer() can find it.
                    if _jl_hash:
                        try:
                            from pyrogram.raw import functions as _raw_fn, types as _raw_types
                            _ci = await client.invoke(
                                _raw_fn.messages.CheckChatInvite(hash=_jl_hash)
                            )
                            if hasattr(_ci, "chat"):
                                await client.fetch_peers([_ci.chat])
                        except Exception:
                            pass  # best-effort; main action may still work
                except FloodWait as _jl_fw:
                    # Must NOT silently continue — account never joined, main
                    # action would fail with a confusing PeerIdInvalid error.
                    return {
                        "ok": False,
                        "detail": f"flood wait {_jl_fw.value}s (auto-join)",
                        "flood_until": _time.time() + min(_jl_fw.value, 60),
                    }
                except Exception as jl_exc:
                    if _is_fatal_error(jl_exc):
                        return {"ok": False, "detail": str(jl_exc)[:80], "expired": True}
                    # Surface the real error so the user knows why the
                    # campaign failed (e.g. expired invite link, banned, etc.)
                    # instead of seeing a misleading PeerIdInvalid on the main action.
                    return {"ok": False, "detail": f"auto-join failed: {str(jl_exc)[:60]}"}

            # Warm the peer cache for private numeric channels with a single
            # targeted get_chat() call instead of iterating all dialogs.
            # get_chat() resolves both @username and numeric IDs in one RPC.
            if action in ("react", "vote", "view", "react_vote", "react_view",
                          "vote_view", "react_vote_view"):
                raw_chat, _ = parse_post_url(target)
                if raw_chat:
                    try:
                        await client.get_chat(_cid(raw_chat) if raw_chat.lstrip("-").isdigit() else raw_chat)
                    except Exception:
                        pass

            def _cid(raw: str):
                try:
                    return int(raw)
                except (ValueError, TypeError):
                    return raw

            if action in ("react", "react_vote", "react_view", "react_vote_view"):
                chat, msg_id = parse_post_url(target)
                if not chat:
                    return {"ok": False, "detail": "Invalid post URL"}
                chosen = random.choice(reactions if reactions else ["👍"])
                await client.send_reaction(chat_id=_cid(chat), message_id=msg_id, emoji=chosen)
                await asyncio.sleep(action_pause)

            if action in ("vote", "react_vote", "vote_view", "react_vote_view"):
                chat, msg_id = parse_post_url(target)
                if not chat:
                    return {"ok": False, "detail": "Invalid post URL"}
                cid = _cid(chat)
                msg = await client.get_messages(chat_id=cid, message_ids=msg_id)
                if msg and msg.reply_markup:
                    # Flatten all buttons that have callback_data
                    all_btns = [
                        btn for row in msg.reply_markup.inline_keyboard
                        for btn in row if btn.callback_data
                    ]
                    if all_btns:
                        pick = all_btns[min(button_index, len(all_btns) - 1)]
                        try:
                            await client.request_callback_answer(
                                chat_id=cid, message_id=msg_id,
                                callback_data=pick.callback_data,
                            )
                        except Exception:
                            pass
                await asyncio.sleep(action_pause)

            if action in ("view", "react_view", "vote_view", "react_vote_view"):
                chat, msg_id = parse_post_url(target)
                if not chat:
                    return {"ok": False, "detail": "Invalid post URL"}
                from pyrogram.raw import functions as _raw_fn
                cid = _cid(chat)
                # Populate the peer cache (required for in-memory sessions).
                # get_chat resolves both @username and numeric channel IDs.
                try:
                    await client.get_chat(cid)
                except Exception:
                    pass
                try:
                    peer = await client.resolve_peer(cid)
                    await client.invoke(
                        _raw_fn.messages.GetMessagesViews(
                            peer=peer,
                            id=[msg_id],
                            increment=True,
                        )
                    )
                except Exception as _ve:
                    _ve_str = str(_ve)[:120]
                    # For pure view action, propagate the error so the caller
                    # knows it didn't work; for combined actions the other
                    # sub-actions already ran so we still report success.
                    if action == "view":
                        return {"ok": False, "detail": f"view: {_ve_str}"}
                    # else: react/vote already executed — log and continue
                    import logging as _log
                    _log.getLogger(__name__).warning("GetMessagesViews failed (non-fatal): %s", _ve_str)
                await asyncio.sleep(action_pause)

            if action == "join":
                ch = parse_channel(target)
                # Keep as string — passing an integer numeric ID to join_chat()
                # fails with PeerIdInvalid on in-memory sessions because the peer
                # cache is empty. The string form lets Pyrogram attempt resolution.
                try:
                    await client.join_chat(ch)
                except UserAlreadyParticipant:
                    pass
                except PeerIdInvalid:
                    return {
                        "ok": False,
                        "detail": "Peer id invalid — use @username or invite link, not a numeric ID",
                    }

            if action == "leave":
                ch = parse_channel(target)
                try:
                    ch = int(ch)
                except (ValueError, TypeError):
                    pass
                try:
                    await client.leave_chat(ch)
                except UserNotParticipant:
                    pass

            if action == "leave_all":
                left = 0
                async for dialog in client.get_dialogs():
                    chat = dialog.chat
                    if chat.type in (ChatType.CHANNEL, ChatType.SUPERGROUP, ChatType.GROUP):
                        try:
                            await client.leave_chat(chat.id)
                            left += 1
                            await asyncio.sleep(action_pause)
                        except Exception:
                            pass
                return {"ok": True, "detail": f"left {left} chat(s)"}

            if action == "bulk_dm":
                await client.send_message(target.strip().lstrip("@"), message or "Hello!")

            if action == "bot_referral":
                import re as _re

                link = target.strip()

                # ── Parse bot username and start param ────────────────────────
                bot_un   = None
                st_param = None
                _m = _re.match(
                    r"https?://t\.me/([A-Za-z0-9_]+)(?:/\w+)?\?start(?:app)?=([^\s&]+)",
                    link,
                )
                if _m:
                    bot_un, st_param = _m.group(1), _m.group(2)
                else:
                    _m = _re.match(r"https?://t\.me/([A-Za-z0-9_]+)", link)
                    if _m:
                        bot_un = _m.group(1)
                    elif link.startswith("@"):
                        bot_un = link[1:]
                    else:
                        bot_un = link.strip("@")

                if not bot_un:
                    return {"ok": False, "detail": "Invalid bot referral link"}

                # ── Helper: extract channels + callback buttons ───────────────
                # Reads text, caption, entity URLs, and inline button URLs/callbacks.
                # Returns an ordered list (no dupes) of channel targets + callback list.
                def _extract(msgs):
                    chans     = []          # ordered, deduplicated
                    seen_ch   = set()
                    cbs       = []
                    seen_cb   = set()
                    _tme = _re.compile(
                        r"https?://(?:t|telegram)\.me/"
                        r"((?:\+|joinchat/)[A-Za-z0-9_+/]+"   # private invite
                        r"|[A-Za-z0-9_]{4,})"                  # public username
                    )
                    _at  = _re.compile(r"@([A-Za-z0-9_]{4,})")

                    def _add(raw: str):
                        """Normalise raw channel/invite and add if not seen."""
                        if raw.lower() == bot_un.lower():
                            return
                        if raw.startswith("+") or raw.lower().startswith("joinchat/"):
                            key = f"https://t.me/{raw}"
                        elif raw.startswith("@"):
                            key = raw
                        else:
                            key = f"@{raw}"
                        if key not in seen_ch:
                            seen_ch.add(key)
                            chans.append(key)

                    for msg in msgs:
                        # text + caption (covers photo/video/document messages)
                        for body in (msg.text, msg.caption):
                            if body:
                                for ch in _tme.findall(body):
                                    _add(ch)
                                for un in _at.findall(body):
                                    _add(un)

                        # entity URLs — hidden links inside clickable text
                        for ent_list in (
                            getattr(msg, "entities", None) or [],
                            getattr(msg, "caption_entities", None) or [],
                        ):
                            for ent in ent_list:
                                url = getattr(ent, "url", None)
                                if url:
                                    for ch in _tme.findall(url):
                                        _add(ch)

                        # inline keyboard — URL buttons and callback buttons
                        rm = getattr(msg, "reply_markup", None)
                        if rm and hasattr(rm, "inline_keyboard"):
                            for row in rm.inline_keyboard:
                                for btn in row:
                                    if getattr(btn, "url", None):
                                        for ch in _tme.findall(btn.url):
                                            _add(ch)
                                    elif getattr(btn, "callback_data", None):
                                        if btn.callback_data not in seen_cb:
                                            cbs.append(
                                                (msg.id, btn.callback_data, btn.text or "")
                                            )
                                            seen_cb.add(btn.callback_data)
                    return chans, cbs

                # ── Helper: join one channel silently ─────────────────────────
                async def _join_ch(ch: str) -> bool:
                    try:
                        await client.join_chat(ch)
                        await asyncio.sleep(action_pause)
                        return True
                    except UserAlreadyParticipant:
                        return True
                    except Exception:
                        return False

                # ── STEP 1: Snapshot history before /start ────────────────────
                # Needed as fallback if the bot was already started and won't
                # send a fresh reply.
                pre_msgs = []
                try:
                    async for msg in client.get_chat_history(bot_un, limit=25):
                        if not msg.outgoing:
                            pre_msgs.append(msg)
                        if len(pre_msgs) >= 15:
                            break
                except Exception:
                    pass
                pre_ids = {msg.id for msg in pre_msgs}

                # ── STEP 2: Send /start via MTProto StartBot (real deep-link) ─
                try:
                    from pyrogram.raw.functions.messages import StartBot as _StartBot
                    import random as _rnd
                    _bot_peer  = await client.resolve_peer(bot_un)
                    _self_peer = await client.resolve_peer("me")
                    await client.invoke(
                        _StartBot(
                            bot=_bot_peer,
                            peer=_self_peer,
                            random_id=_rnd.randint(0, 2**63 - 1),
                            start_param=st_param or "",
                        )
                    )
                except Exception:
                    # Fallback to plain-text /start
                    start_text = f"/start {st_param}" if st_param else "/start"
                    try:
                        await client.send_message(bot_un, start_text)
                    except Exception:
                        pass

                # Give the bot enough time to respond (many need 2-3 s)
                await asyncio.sleep(2.5)

                # ── STEP 3: Read new messages from the bot ────────────────────
                new_after_start = []
                try:
                    async for msg in client.get_chat_history(bot_un, limit=20):
                        if not msg.outgoing and msg.id not in pre_ids:
                            new_after_start.append(msg)
                        if len(new_after_start) >= 10:
                            break
                except Exception:
                    pass

                bot_msgs = new_after_start if new_after_start else pre_msgs[:5]
                seen_msg_ids = pre_ids | {msg.id for msg in new_after_start}

                channels_found, cb_buttons = _extract(bot_msgs)

                # ── STEP 4: Join ALL channels first ──────────────────────────
                # Bots verify real membership before rewarding — join everything
                # before clicking any check button.
                joined = 0
                for ch in channels_found:
                    if await _join_ch(ch):
                        joined += 1

                # Wait for Telegram to propagate membership to the bot
                if channels_found:
                    await asyncio.sleep(2.5)

                # ── STEP 5: Click all verify/check buttons ────────────────────
                verify_rounds = 0
                for msg_id, cb_data, _ in cb_buttons[:10]:
                    try:
                        await client.request_callback_answer(
                            chat_id=bot_un,
                            message_id=msg_id,
                            callback_data=cb_data,
                        )
                        verify_rounds += 1
                        await asyncio.sleep(action_pause)
                    except Exception:
                        pass

                # ── STEP 6: Read verification response + join new channels ────
                await asyncio.sleep(2.5)

                new_msgs = []
                try:
                    async for msg in client.get_chat_history(bot_un, limit=20):
                        if not msg.outgoing and msg.id not in seen_msg_ids:
                            new_msgs.append(msg)
                        if len(new_msgs) >= 10:
                            break
                except Exception:
                    pass

                if new_msgs:
                    seen_msg_ids.update(msg.id for msg in new_msgs)
                    new_chans, new_cbs = _extract(new_msgs)

                    # Join newly mentioned channels
                    existing = set(channels_found)
                    for ch in new_chans:
                        if ch not in existing:
                            if await _join_ch(ch):
                                joined += 1
                            channels_found.append(ch)

                    # If new channels were joined, wait before re-checking
                    if new_chans:
                        await asyncio.sleep(2.5)

                    # Click any new verify buttons from the response
                    for msg_id, cb_data, _ in new_cbs[:10]:
                        try:
                            await client.request_callback_answer(
                                chat_id=bot_un,
                                message_id=msg_id,
                                callback_data=cb_data,
                            )
                            verify_rounds += 1
                            await asyncio.sleep(action_pause)
                        except Exception:
                            pass

                return {
                    "ok": True,
                    "detail": (
                        f"started · {joined} ch joined"
                        f" · {verify_rounds} verify done"
                    ),
                }

            return {"ok": True, "detail": "success"}

        except FloodWait as e:
            # Return immediately with flood metadata — sleeping here would eat
            # into the outer asyncio.wait_for budget and trigger a spurious
            # "action timed out" even for healthy accounts.
            flood_until = _time.time() + min(e.value, 60)
            return {"ok": False, "detail": f"flood wait {e.value}s", "flood_until": flood_until}
        except (ChannelInvalid, UsernameInvalid):
            return {"ok": False, "detail": "Invalid channel/username"}
        except PeerIdInvalid:
            return {"ok": False, "detail": "Peer id invalid — use @username or invite link, not a numeric ID"}
        except MessageIdInvalid:
            return {"ok": False, "detail": "Invalid message ID"}
        except UnicodeDecodeError:
            # Pyrogram decodes the session key lazily on the first RPC call.
            # This can be transient — do NOT mark the account dead.
            return {"ok": False, "detail": "session decode error (transient, will retry)"}
        except Exception as e:
            if _is_fatal_error(e):
                return {"ok": False, "detail": str(e)[:80], "expired": True}
            return {"ok": False, "detail": str(e)[:80]}

    # Timeouts by action complexity:
    #   bot_referral  — multi-step bot interaction with AI picker (90 s)
    #   leave_all     — iterates every dialog the account is in (60 s)
    #   combined      — react + vote + view, a few round-trips (20 s)
    #   simple        — single RPC call (15 s)
    _COMPLEX_COMBINED = {"react_vote_view", "react_vote", "react_view", "vote_view"}
    if action == "bot_referral":
        _timeout = 90
    elif action == "leave_all":
        _timeout = 60
    elif action in _COMPLEX_COMBINED:
        _timeout = 20
    else:
        _timeout = 15
    try:
        result = await asyncio.wait_for(_do_action(), timeout=_timeout)
    except asyncio.TimeoutError:
        result = {"ok": False, "detail": "action timed out"}
    finally:
        asyncio.ensure_future(client.stop())   # non-blocking cleanup

    return result


async def execute_campaign(
    user_id: int,
    camp_index: int,
    on_progress: Optional[Callable[[int, int, int, int, list], Awaitable[None]]] = None,
    max_accounts_override: int = 0,
    resume_identifiers: list = None,
) -> dict:
    from storage import get_accounts, get_campaigns, get_settings

    # Load speed preset for this user
    speed_key = get_settings(user_id).get("speed", "smart")
    preset = SPEED_PRESETS.get(speed_key, SPEED_PRESETS["normal"])
    _workers      = preset["workers"]
    _min_delay    = preset["min_delay"]
    _max_delay    = preset["max_delay"]
    _action_pause = preset["action_pause"]

    accounts = get_accounts(user_id)
    campaigns = get_campaigns(user_id)

    if camp_index >= len(campaigns):
        return {"done": 0, "failed": 0, "skipped": 0, "errors": ["Campaign not found"]}

    camp = campaigns[camp_index]
    action       = camp.get("action_type", "")
    target       = camp.get("target", "")
    dm_message   = camp.get("message", "")
    reactions    = camp.get("reactions") or ["👍"]
    join_link    = camp.get("join_link", "")
    button_index = camp.get("button_index", 0)
    max_accounts = max_accounts_override or camp.get("max_accounts", 0)

    # Apply label filter if set on this campaign
    label_filter = camp.get("label_filter", "")
    if label_filter:
        runnable = [
            a for a in accounts
            if a.get("status") == "active" and label_filter in a.get("labels", [])
        ]
    else:
        runnable = [a for a in accounts if a.get("status") == "active"]

    # Apply cooldown: skip accounts used more recently than cooldown_minutes ago
    from storage import get_cooldown_minutes
    cooldown_secs = get_cooldown_minutes(user_id) * 60
    if cooldown_secs > 0:
        _now = _time.time()
        runnable = [
            a for a in runnable
            if (_now - a.get("last_used", 0)) >= cooldown_secs
        ]

    # Resume-from-pause: restrict to the accounts that were skipped last time
    if resume_identifiers:
        id_set = set(resume_identifiers)
        runnable = [a for a in runnable if a.get("identifier") in id_set]

    if max_accounts > 0:
        runnable = runnable[:max_accounts]
    skipped_count = 0

    total = len(runnable)
    if total == 0:
        return {"done": 0, "failed": 0, "skipped": skipped_count, "errors": ["No accounts with valid session strings"]}

    # Pre-assign reactions evenly across accounts.
    # Remainder accounts go to the first selected reaction.
    if reactions and len(reactions) > 1:
        _assigned: list = []
        per, rem = divmod(total, len(reactions))
        for i, r in enumerate(reactions):
            _assigned.extend([r] * (per + (1 if i < rem else 0)))
        random.shuffle(_assigned)
    else:
        _assigned = [(reactions[0] if reactions else "👍")] * total

    # Shared counters (use a dict so closures can mutate)
    counters = {
        "done": 0, "failed": 0, "skipped": 0,
        "errors": [], "stopped": False, "paused": False,
        "failed_ids": [],       # identifiers of accounts that failed
        "dead_alerts": [],      # (name, reason) for accounts newly marked dead
        "paused_remaining": [], # identifiers skipped because of pause (for resume)
        "flood_waits": [],      # (account_name, flood_until_ts) for throttled accounts
    }
    lock = asyncio.Lock()
    semaphore = asyncio.Semaphore(_workers)

    async def run_one(acc: dict, slot_index: int) -> None:
        from storage import (
            get_accounts as _get_accounts, set_account_status,
            set_account_throttle, is_campaign_stop_requested,
            is_campaign_pause_requested,
        )

        # Stagger starts: small random jitter so accounts don't all hit Telegram
        # at the exact same millisecond.  Do NOT multiply by slot_index — that
        # would make account #50 wait 70 s of dead time inside the semaphore.
        await asyncio.sleep(random.uniform(0, _min_delay))

        # Honour stop / pause flags AFTER the stagger delay
        if is_campaign_stop_requested(user_id, camp_index):
            async with lock:
                counters["skipped"] += 1
                counters["stopped"] = True
            return

        if is_campaign_pause_requested(user_id, camp_index):
            async with lock:
                counters["skipped"] += 1
                counters["paused"] = True
                counters["paused_remaining"].append(acc.get("identifier", ""))
            return

        # Fail-fast: skip accounts with 3+ consecutive failures
        if acc.get("consecutive_failures", 0) >= 3:
            async with lock:
                counters["skipped"] += 1
            return

        identifier = acc.get("identifier", "")
        try:
            result = await run_campaign_on_account(
                identifier, action, target, dm_message,
                reactions=[_assigned[slot_index]],
                action_pause=_action_pause,
                join_link=join_link,
                button_index=button_index,
            )
        except UnicodeDecodeError:
            # Pyrogram can raise this transiently — do NOT mark the account dead.
            result = {"ok": False, "detail": "session decode error (transient, will retry)"}
        except Exception as _run_exc:
            result = {"ok": False, "detail": str(_run_exc)[:80]}

        # Auto-mark frozen/banned/expired accounts as dead
        if result.get("expired"):
            live_accounts = _get_accounts(user_id)
            for idx, a in enumerate(live_accounts):
                if a.get("identifier") == identifier:
                    set_account_status(user_id, idx, "dead")
                    async with lock:
                        counters["dead_alerts"].append(
                            (a.get("name", "?"), result.get("detail", "Session expired"))
                        )
                    break

        # Record throttle timestamp if FloodWait occurred
        flood_until = result.get("flood_until")
        if flood_until:
            live_accounts = _get_accounts(user_id)
            for idx, a in enumerate(live_accounts):
                if a.get("identifier") == identifier:
                    set_account_throttle(user_id, idx, flood_until)
                    async with lock:
                        counters["flood_waits"].append(
                            (acc.get("name", "?"), flood_until)
                        )
                    break

        async with lock:
            if result["ok"]:
                counters["done"] += 1
                # Reset consecutive failure counter on success; track last_used + success_count
                acc.pop("consecutive_failures", None)
                import time as _t
                _now = _t.time()
                # Persist last_used and success_count to storage
                try:
                    from storage import update_account_last_used
                    update_account_last_used(user_id, identifier, _now)
                except Exception:
                    pass
            else:
                counters["failed"] += 1
                counters["failed_ids"].append(identifier)
                phone = acc.get("phone") or acc.get("username", "?")
                counters["errors"].append(f"{acc.get('name', '?')} ({phone}): {result['detail']}")
                acc["consecutive_failures"] = acc.get("consecutive_failures", 0) + 1
                # Persist fail_count
                try:
                    from storage import increment_account_fail_count
                    increment_account_fail_count(user_id, identifier)
                except Exception:
                    pass

            if on_progress:
                await on_progress(
                    counters["done"],
                    counters["failed"],
                    counters["skipped"],
                    total,
                    counters["errors"],
                    counters["flood_waits"],
                )

    # Actions that carry higher ban risk — need extra spacing
    _HIGH_RISK = {"join", "leave", "bot_referral", "bulk_dm"}

    async def run_with_semaphore(acc: dict, slot_index: int) -> None:
        async with semaphore:
            await run_one(acc, slot_index)
            # Hold the semaphore slot for a random inter-account gap so
            # the next account cannot start until enough time has passed.
            # High-risk actions (join / DM / referral) use 2× the base gap.
            if action in _HIGH_RISK:
                _gap = random.uniform(_min_delay * 2.0, _max_delay * 2.0)
            else:
                _gap = random.uniform(_min_delay, _max_delay)
            await asyncio.sleep(_gap)

    # Launch all accounts concurrently, capped by semaphore
    tasks = [run_with_semaphore(acc, i) for i, acc in enumerate(runnable)]
    await asyncio.gather(*tasks)

    # Save updated action count
    from storage import increment_campaign_actions
    increment_campaign_actions(user_id, camp_index, counters["done"])

    # Auto-remove accounts that exceed the configured failure threshold
    from storage import get_auto_remove_threshold, remove_account
    _threshold = get_auto_remove_threshold()
    if _threshold > 0:
        _live = get_accounts(user_id)
        _to_remove = [
            i for i, a in enumerate(_live)
            if a.get("consecutive_failures", 0) >= _threshold
        ]
        for _idx in sorted(_to_remove, reverse=True):
            remove_account(user_id, _idx)

    return {
        "done": counters["done"],
        "failed": counters["failed"],
        "skipped": counters["skipped"] + skipped_count,
        "errors": counters["errors"][:5],
        "stopped": counters["stopped"],
        "paused": counters["paused"],
        "failed_ids": counters["failed_ids"],
        "dead_alerts": counters["dead_alerts"],
        "paused_remaining": counters["paused_remaining"],
    }
