"""
runner.py — Campaign execution engine.

Exports:
  progress_bar(done, total, width)  — ASCII progress bar string
  execute_campaign(...)             — Run a campaign against a list of accounts
  run_campaign(application, uid, idx) — Scheduler entry point
"""

import re
import os
import asyncio
import random
import time
import logging
import hashlib
from urllib.parse import parse_qs, urlparse
from typing import Optional, Callable, Awaitable

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

WORKERS = int(os.environ.get('RUNNER_WORKERS', '5'))
MIN_DELAY = 1.5
MAX_DELAY = 4.0
ACTION_PAUSE = 0.5

_API_ID   = int(os.environ.get('PYROGRAM_API_ID')   or os.environ.get('API_ID')   or 0)
_API_HASH =     os.environ.get('PYROGRAM_API_HASH')  or os.environ.get('API_HASH')  or ''

SPEED_PRESETS = {
    'slow':   {'workers': 1, 'min_delay': 4.0,  'max_delay': 8.0},
    'normal': {'workers': 3, 'min_delay': 2.0,  'max_delay': 5.0},
    'fast':   {'workers': 5, 'min_delay': 1.0,  'max_delay': 3.0},
    'ultra':  {'workers': 8, 'min_delay': 0.5,  'max_delay': 1.5},
    'smart':  {'workers': 4, 'min_delay': 1.5,  'max_delay': 4.0},
}

_FATAL_ERRORS = (
    'AUTH_KEY_UNREGISTERED',
    'AUTH_KEY_INVALID',
    'USER_DEACTIVATED',
    'USER_DEACTIVATED_BAN',
    'SESSION_REVOKED',
    'SESSION_EXPIRED',
    'ACCOUNT_BANNED',
)


def _is_fatal_error(exc: Exception) -> bool:
    msg = str(exc).upper()
    return any(fe in msg for fe in _FATAL_ERRORS)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def progress_bar(done: int, total: int, width: int = 20) -> str:
    """Return an ASCII progress bar of given width."""
    filled = int(width * done / total) if total else 0
    return '█' * filled + '░' * (width - filled)


def parse_post_url(url: str) -> tuple:
    """Parse a Telegram post URL into (channel, message_id)."""
    raw = (url or '').strip().strip('`')
    parsed = urlparse(raw if re.match(r'https?://', raw, re.IGNORECASE)
                      else f'https://t.me/{raw.lstrip("/")}')
    if parsed.netloc.lower() not in {
        't.me', 'www.t.me', 'telegram.me', 'www.telegram.me'
    }:
        return None, None

    parts = [part for part in parsed.path.split('/') if part]
    if len(parts) >= 2 and parts[0].lower() == 'c' and parts[1].isdigit():
        if len(parts) >= 3 and parts[2].isdigit():
            return int(f'-100{parts[1]}'), int(parts[2])
        return None, None

    if len(parts) >= 2 and parts[1].isdigit():
        channel = parts[0]
        if channel.startswith('@'):
            channel = channel[1:]
        return f'@{channel}', int(parts[1])

    return None, None


def normalize_invite_link(link: str) -> str:
    """Normalize only private invite links; leave public channel links intact."""
    link = (link or '').strip().strip('`')
    if re.fullmatch(r'\+[A-Za-z0-9_-]+', link):
        return f'https://t.me/joinchat/{link[1:]}'

    match = re.fullmatch(
        r'https?://(?:www\.)?t\.me/(?:joinchat/|\+)([A-Za-z0-9_-]+)',
        link,
        re.IGNORECASE,
    )
    if match:
        return f'https://t.me/joinchat/{match.group(1)}'

    return link


def parse_channel(target: str) -> str:
    """Return a channel identifier usable by Pyrogram."""
    return parse_chat_target(target)


def parse_bot_referral(target: str) -> tuple[str | None, str | None]:
    """Return (bot_username, start_payload) from a Telegram bot link."""
    target = (target or '').strip()
    if not target:
        return None, None

    if not re.match(r'https?://', target, re.IGNORECASE):
        target = f'https://t.me/{target.lstrip("@")}'

    parsed = urlparse(target)
    if parsed.netloc.lower() not in {'t.me', 'www.t.me', 'telegram.me', 'www.telegram.me'}:
        return None, None

    parts = [part for part in parsed.path.split('/') if part]
    if not parts:
        return None, None

    bot_username = parts[0]
    if bot_username.startswith('@'):
        bot_username = bot_username[1:]
    if not re.fullmatch(r'[A-Za-z0-9_]{5,32}', bot_username):
        return None, None

    query = parse_qs(parsed.query)
    payload = (query.get('start') or query.get('startapp') or [None])[0]
    return bot_username, payload


def parse_chat_target(target: str):
    """Return a Pyrogram chat identifier from a public channel target.

    Telegram's ``/c/<id>`` links contain the internal channel id rather than
    a username.  Treat numeric ids as integers too; passing them through
    ``parse_channel`` would incorrectly turn them into ``@-100...``.
    """
    target = (target or '').strip().strip('`')
    if not target:
        return None

    parsed = urlparse(target if re.match(r'https?://', target, re.IGNORECASE)
                      else f'https://t.me/{target.lstrip("@")}')
    path_parts = [part for part in parsed.path.split('/') if part]

    if (
        parsed.netloc.lower() in {'t.me', 'www.t.me', 'telegram.me', 'www.telegram.me'}
        and path_parts
        and path_parts[0].lower() == 'c'
        and len(path_parts) >= 2
        and path_parts[1].isdigit()
    ):
        return int(f'-100{path_parts[1]}')

    if extract_invite_hash(target):
        # An invite hash identifies an invitation, not the already-joined
        # chat.  It cannot be passed to leave_chat().
        return None

    if parsed.netloc.lower() in {'t.me', 'www.t.me', 'telegram.me', 'www.telegram.me'}:
        target = path_parts[0] if path_parts else ''

    numeric = target.lstrip('@')
    if re.fullmatch(r'-?\d+', numeric):
        return int(numeric)

    target = target.lstrip('@').strip()
    return f'@{target}' if target else None


def extract_invite_hash(target: str) -> str | None:
    """Extract a Telegram invite hash from a public invite-link format."""
    target = (target or '').strip()
    match = re.fullmatch(
        r'(?:https?://)?t\.me/(?:joinchat/|\+)([A-Za-z0-9_-]+)',
        target,
        re.IGNORECASE,
    )
    if match:
        return match.group(1)
    if re.fullmatch(r'\+[A-Za-z0-9_-]+', target):
        return target[1:]
    return None


async def resolve_leave_target(client, target: str):
    """Resolve a leave target to a peer known by this account.

    Private invite links do not contain the channel ID.  CheckChatInvite
    returns the joined chat for accounts that are already members.  Numeric
    IDs are checked against the account's dialogs first so Telegram does not
    receive an unknown peer and return ``PEER_ID_INVALID``.
    """
    target = (target or '').strip()
    invite_hash = extract_invite_hash(target)
    if invite_hash:
        from pyrogram.raw.functions.messages import CheckChatInvite

        try:
            invite = await client.invoke(CheckChatInvite(hash=invite_hash))
        except Exception as exc:
            return None, f'Could not resolve invite link: {exc}'

        chat = getattr(invite, 'chat', None)
        chat_id = getattr(chat, 'id', None)
        if chat_id is not None:
            return chat_id, None
        return None, (
            'This account is not a member of the channel from that invite link, '
            'so there is nothing for it to leave.'
        )

    peer = parse_chat_target(target)
    if peer is None:
        return None, 'No valid channel target was provided.'

    if isinstance(peer, int):
        async for dialog in client.get_dialogs():
            chat = getattr(dialog, 'chat', None)
            if getattr(chat, 'id', None) == peer:
                return peer, None
        return None, (
            f'This account is not a member of {peer}, or Telegram cannot resolve '
            'that numeric channel ID for this account.'
        )

    try:
        chat = await client.get_chat(peer)
        return getattr(chat, 'id', peer), None
    except Exception as exc:
        return None, _friendly_peer_error(exc, peer)


def _friendly_peer_error(exc: Exception, target=None) -> str:
    """Turn Telegram's opaque peer errors into an actionable campaign error."""
    message = str(exc)
    upper = message.upper()
    if 'PEER_ID_INVALID' in upper or 'USERNAME_NOT_OCCUPIED' in upper:
        suffix = f' for {target}' if target is not None else ''
        return (
            f'Telegram could not resolve the chat{suffix}. '
            'Use the channel username or numeric ID, and make sure this account '
            'has opened or joined the channel first.'
        )
    return message


async def resolve_chat_target(client, target: str):
    """Resolve a target to a peer ID known to the current account."""
    peer = parse_chat_target(target)
    if peer is None:
        return None, 'Invite links cannot identify a chat until the account joins it.'

    if isinstance(peer, int):
        async for dialog in client.get_dialogs():
            chat = getattr(dialog, 'chat', None)
            if getattr(chat, 'id', None) == peer:
                return peer, None
        try:
            chat = await client.get_chat(peer)
            return getattr(chat, 'id', peer), None
        except Exception as exc:
            return None, _friendly_peer_error(exc, peer)

    try:
        chat = await client.get_chat(peer)
        return getattr(chat, 'id', peer), None
    except Exception as exc:
        return None, _friendly_peer_error(exc, peer)


async def resolve_post_target(client, target: str):
    """Resolve a t.me post URL and return (peer_id, message_id, error)."""
    channel, message_id = parse_post_url(target)
    if channel is None or message_id is None:
        return None, None, 'Use a Telegram post URL such as https://t.me/channel/123.'
    peer, error = await resolve_chat_target(client, channel)
    return peer, message_id, error


async def click_vote_button(client, peer, message_id: int, button_index: int) -> None:
    """Click an inline vote button or submit a native poll vote."""
    message = await client.get_messages(peer, message_id)
    poll = getattr(message, 'poll', None)
    if poll is not None and hasattr(client, 'vote_poll'):
        await client.vote_poll(peer, message_id, [button_index])
        return

    markup = getattr(message, 'reply_markup', None)
    keyboard = getattr(markup, 'inline_keyboard', None) if markup else None
    buttons = [button for row in (keyboard or []) for button in row]
    if button_index < 0 or button_index >= len(buttons):
        raise ValueError(
            f'Button #{button_index + 1} was not found on the Telegram post.'
        )

    callback_data = getattr(buttons[button_index], 'callback_data', None)
    if callback_data is None:
        raise ValueError(f'Button #{button_index + 1} is not a callback button.')
    if isinstance(callback_data, bytes):
        callback_data = callback_data.decode('utf-8', errors='ignore')
    await client.request_callback_answer(
        peer, message_id, callback_data=callback_data
    )


# ---------------------------------------------------------------------------
# Per-account action
# ---------------------------------------------------------------------------

async def run_campaign_on_account(
    acc: dict,
    camp: dict,
    *,
    api_id: int,
    api_hash: str,
    reactions: Optional[list] = None,
    workdir: str = '/tmp/sessions',
) -> dict:
    """
    Run the campaign action for a single account.
    Returns {'ok': bool, 'error': str|None, 'fatal': bool}.
    """
    import os, tempfile
    from pyrogram import Client
    from pyrogram.errors import (
        FloodWait, UserPrivacyRestricted, PeerFlood, ChatWriteForbidden,
        InputUserDeactivated, UserBannedInChannel, UserNotParticipant,
    )

    # Account records created by the bot store the Pyrogram session string
    # under ``identifier``. Keep the explicit names for compatibility with
    # imported/legacy records, but use the stored identifier as the fallback.
    session_str = (
        acc.get('session')
        or acc.get('session_string')
        or acc.get('identifier')
    )
    session_file = acc.get('session_file')
    identifier   = acc.get('phone') or acc.get('id') or acc.get('user_id', 'unknown')

    os.makedirs(workdir, exist_ok=True)

    try:
        if not api_id or not api_hash:
            return {
                'ok': False,
                'error': 'Pyrogram API ID/API hash is not configured.',
                'fatal': False,
            }
        if session_file and os.path.exists(session_file):
            session_name = os.path.splitext(session_file)[0]
            client = Client(session_name, api_id=api_id, api_hash=api_hash,
                            no_updates=True)
        elif session_str:
            session_name = 'campaign_' + hashlib.sha256(
                str(session_str).encode('utf-8')
            ).hexdigest()[:16]
            client = Client(
                session_name,
                session_string=session_str,
                api_id=api_id,
                api_hash=api_hash,
                no_updates=True,
                in_memory=True,
            )
        else:
            return {'ok': False, 'error': 'No session available', 'fatal': False}
    except Exception as e:
        return {'ok': False, 'error': f'Client init: {e}', 'fatal': False}

    # Campaigns created by the regular campaign flow are stored with the
    # ``action_type`` key, while older/advanced campaigns use ``action`` or
    # ``camp_action``.  Accept all three formats.
    action = (
        camp.get('action')
        or camp.get('action_type')
        or camp.get('camp_action', '')
    )
    action = str(action).removeprefix('camp_action_')
    target = camp.get('target', camp.get('camp_target', ''))

    try:
        async with client:
            if action == 'dm':
                message = camp.get('message') or camp.get('camp_dm_message', '')
                if not message:
                    return {'ok': False, 'error': 'No DM message configured', 'fatal': False}
                peer, error = await resolve_chat_target(client, target)
                if error:
                    return {'ok': False, 'error': error, 'fatal': False}
                await client.send_message(peer, message)

            elif action in {
                'react', 'vote', 'react_vote', 'view', 'react_view',
                'vote_view', 'react_vote_view',
            }:
                peer, msg_id, error = await resolve_post_target(client, target)
                if error:
                    return {'ok': False, 'error': error, 'fatal': False}

                reaction_choices = (
                    reactions
                    or camp.get('reactions')
                    or camp.get('camp_reactions')
                    or ['👍']
                )
                if action in {'react', 'react_vote', 'react_view', 'react_vote_view'}:
                    await client.send_reaction(
                        peer, msg_id, emoji=random.choice(reaction_choices)
                    )

                if action in {'vote', 'react_vote', 'vote_view', 'react_vote_view'}:
                    button_index = int(
                        camp.get('button_index', camp.get('camp_button_index', 0)) or 0
                    )
                    await click_vote_button(client, peer, msg_id, button_index)

                if action in {'view', 'react_view', 'vote_view', 'react_vote_view'}:
                    from pyrogram.raw.functions.messages import GetMessagesViews
                    await client.invoke(
                        GetMessagesViews(
                            peer=await client.resolve_peer(peer),
                            id=[msg_id],
                            increment=True,
                        )
                    )

            elif action == 'join':
                join_link = camp.get('join_link') or camp.get('camp_join_link', '')
                # A Join campaign may store the invite directly in ``target``
                # (for example, https://t.me/+InviteHash). Do not send that
                # URL through parse_channel(), which turns it into an invalid
                # username and causes Telegram USERNAME_NOT_OCCUPIED.
                if not join_link and extract_invite_hash(target):
                    join_link = target
                if join_link:
                    link = normalize_invite_link(join_link)
                    await client.join_chat(link)
                else:
                    peer, error = await resolve_chat_target(client, target)
                    if error:
                        return {'ok': False, 'error': error, 'fatal': False}
                    await client.join_chat(peer)

            elif action == 'bot_referral':
                bot_username, payload = parse_bot_referral(target)
                if not bot_username:
                    return {
                        'ok': False,
                        'error': (
                            'Invalid bot referral link. Use '
                            'https://t.me/BotName?start=payload.'
                        ),
                        'fatal': False,
                    }

                # Telegram deep links are delivered to bots as /start
                # commands. ``startapp`` links are also sent with their
                # payload; the Bot API can then decide how to handle it.
                command = f'/start {payload}' if payload else '/start'
                await client.send_message(bot_username, command)

            elif action == 'leave':
                if not target:
                    return {'ok': False, 'error': 'No channel configured', 'fatal': False}

                peer, resolve_error = await resolve_leave_target(client, target)
                if peer is None:
                    return {
                        'ok': False,
                        'error': resolve_error or 'Could not resolve channel target.',
                        'fatal': False,
                    }
                await client.leave_chat(peer)

            elif action == 'leave_all':
                from pyrogram.enums import ChatType

                failures = []
                left_count = 0
                async for dialog in client.get_dialogs():
                    chat = dialog.chat
                    # Private and public channels both use CHANNEL. Include
                    # regular groups and supergroups as well. Direct-message
                    # chats are not leaveable Telegram group/channel chats.
                    if getattr(chat, 'type', None) not in (
                        ChatType.CHANNEL,
                        ChatType.GROUP,
                        ChatType.SUPERGROUP,
                    ):
                        continue
                    try:
                        await client.leave_chat(chat.id)
                        left_count += 1
                    except Exception as exc:
                        failures.append(f'{chat.id}: {exc}')

                if failures and left_count == 0:
                    return {
                        'ok': False,
                        'error': '; '.join(failures[:3]),
                        'fatal': False,
                    }

            else:
                return {'ok': False, 'error': f'Unknown action: {action}', 'fatal': False}

        return {'ok': True, 'error': None, 'fatal': False}

    except FloodWait as e:
        return {'ok': False, 'error': f'FloodWait {e.value}s', 'fatal': False}
    except (UserPrivacyRestricted, PeerFlood, ChatWriteForbidden,
            InputUserDeactivated, UserBannedInChannel) as e:
        fatal = _is_fatal_error(e) or isinstance(e, InputUserDeactivated)
        return {'ok': False, 'error': str(e), 'fatal': fatal}
    except Exception as e:
        fatal = _is_fatal_error(e)
        return {'ok': False, 'error': _friendly_peer_error(e), 'fatal': fatal}


# ---------------------------------------------------------------------------
# Main execute_campaign
# ---------------------------------------------------------------------------

async def execute_campaign(
    camp: dict,
    accounts: list,
    user_id: int,
    camp_index: int,
    on_progress: Optional[Callable] = None,
    resume_ids: Optional[list] = None,
    retry_ids: Optional[list] = None,
    dry_run: bool = False,
) -> dict:
    """
    Execute a campaign against a list of accounts.

    Returns a result dict:
      {'done': int, 'failed': int, 'skipped': int, 'errors': list,
       'stopped': bool, 'paused': bool, 'paused_remaining': list,
       'dead_alerts': list}
    """
    import storage

    speed  = storage.get_settings(user_id).get('speed', 'smart')
    preset = SPEED_PRESETS.get(speed, SPEED_PRESETS['smart'])
    workers     = preset['workers']
    min_delay   = preset['min_delay']
    max_delay   = preset['max_delay']

    # Per-user or global cooldown override
    user_cd   = storage.get_cooldown_minutes(user_id) * 60
    global_cd = storage.get_global_cooldown_minutes() * 60
    effective_cd = max(user_cd, global_cd)

    # Determine working set
    label_filter = camp.get('label') or camp.get('label_filter')
    if label_filter:
        working = [a for a in accounts if label_filter in a.get('labels', [])]
    else:
        working = list(accounts)

    # Filter to only active accounts
    working = [a for a in working if a.get('status', 'active') == 'active']

    # Limit to max_accounts if set
    max_accts = camp.get('max_accounts') or camp.get('camp_max_accounts')
    if max_accts and str(max_accts).isdigit():
        working = working[:int(max_accts)]

    # Resume from a subset if requested
    if resume_ids:
        id_set = set(str(r) for r in resume_ids)
        working = [a for a in working
                   if str(a.get('identifier') or a.get('phone') or
                          a.get('user_id') or a.get('id', '')) in id_set]
    elif retry_ids:
        id_set = set(str(r) for r in retry_ids)
        working = [a for a in working
                   if str(a.get('identifier') or a.get('phone') or
                          a.get('user_id') or a.get('id', '')) in id_set]

    total = len(working)
    result = {
        'done': 0, 'failed': 0, 'skipped': 0,
        'errors': [], 'stopped': False, 'paused': False,
        'paused_remaining': [], 'dead_alerts': [],
    }

    if dry_run or not working:
        return result

    api_id   = _API_ID
    api_hash = _API_HASH

    semaphore = asyncio.Semaphore(workers)

    async def process_one(idx: int, acc: dict):
        async with semaphore:
            identifier = (
                acc.get('identifier')
                or acc.get('phone')
                or str(acc.get('user_id') or acc.get('id', idx))
            )

            if storage.is_campaign_stop_requested(user_id, camp_index):
                result['stopped'] = True
                result['paused_remaining'].append(identifier)
                return

            if storage.is_campaign_pause_requested(user_id, camp_index):
                result['paused'] = True
                result['paused_remaining'].append(identifier)
                return

            res = await run_campaign_on_account(acc, camp, api_id=api_id, api_hash=api_hash)

            if res['ok']:
                result['done'] += 1
                storage.update_account_last_used(user_id, identifier, int(time.time()))
                storage.increment_campaign_actions(user_id, camp_index, 1)
                if effective_cd:
                    storage.set_account_throttle(user_id, idx, int(time.time()) + effective_cd)
            else:
                result['failed'] += 1
                result['errors'].append(f'{identifier}: {res["error"]}')
                storage.increment_account_fail_count(user_id, identifier)
                if res['fatal']:
                    storage.set_account_status(user_id, idx, 'dead')
                    result['dead_alerts'].append(identifier)

            if on_progress:
                done_so_far = result['done'] + result['failed'] + result['skipped']
                try:
                    await on_progress(done_so_far, total, result)
                except Exception:
                    pass

            # Inter-account delay
            await asyncio.sleep(random.uniform(min_delay, max_delay))

    tasks = [process_one(i, acc) for i, acc in enumerate(working)]
    await asyncio.gather(*tasks)

    # Clean up stop/pause flags
    if result['stopped']:
        try:
            storage.clear_campaign_stop(user_id, camp_index)
        except Exception:
            pass
    if result['paused']:
        try:
            storage.set_campaign_paused_remaining(user_id, camp_index, result['paused_remaining'])
            storage.set_campaign_pause(user_id, camp_index)
        except Exception:
            pass
    else:
        try:
            storage.clear_campaign_pause(user_id, camp_index)
            storage.clear_campaign_paused_remaining(user_id, camp_index)
        except Exception:
            pass

    return result


# ---------------------------------------------------------------------------
# Scheduler entry point
# ---------------------------------------------------------------------------

async def run_campaign(application, user_id: int, camp_index: int):
    """
    High-level entry point called by the scheduler and schedule runner.
    Runs the campaign and updates storage state; does not send any Telegram messages.
    """
    import storage

    if storage.is_campaign_running(user_id, camp_index):
        logger.info('Campaign %s/%s already running, skipping duplicate start', user_id, camp_index)
        return

    campaigns = storage.get_campaigns(user_id)
    if camp_index >= len(campaigns):
        logger.warning('Campaign index %s out of range for user %s', camp_index, user_id)
        return

    camp     = campaigns[camp_index]
    accounts = storage.get_accounts(user_id)

    storage.set_campaign_running(user_id, camp_index, True)
    start_ts = int(time.time())

    try:
        result = await execute_campaign(camp, accounts, user_id, camp_index)
    except Exception as exc:
        logger.exception('execute_campaign failed for user %s camp %s: %s', user_id, camp_index, exc)
        result = {'done': 0, 'failed': 0, 'skipped': 0, 'errors': [str(exc)],
                  'stopped': False, 'paused': False, 'paused_remaining': [], 'dead_alerts': []}
    finally:
        if not result.get('paused'):
            storage.set_campaign_running(user_id, camp_index, False)

    elapsed = int(time.time()) - start_ts
    record  = {
        'ts':      __import__('datetime').datetime.utcnow().strftime('%Y-%m-%d %H:%M'),
        'done':    result.get('done',    0),
        'failed':  result.get('failed',  0),
        'skipped': result.get('skipped', 0),
        'elapsed': elapsed,
        'stopped': result.get('stopped', False),
        'paused':  result.get('paused',  False),
    }
    try:
        storage.append_campaign_run_log(user_id, camp_index, record)
    except Exception as exc:
        logger.warning('append_campaign_run_log failed: %s', exc)

    if result.get('dead_alerts'):
        try:
            storage.set_campaign_last_failed(user_id, camp_index, result['dead_alerts'])
        except Exception:
            pass

    logger.info(
        'Campaign %s/%s finished: done=%s failed=%s stopped=%s paused=%s elapsed=%ss',
        user_id, camp_index,
        result['done'], result['failed'],
        result['stopped'], result['paused'], elapsed,
    )
