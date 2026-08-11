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
    m = re.match(
        r'https?://t\.me/(?:c/)?([^/]+)/(\d+)', url.strip()
    )
    if m:
        chan = m.group(1)
        mid  = int(m.group(2))
        if chan.isdigit():
            chan = int('-100' + chan)
        return chan, mid
    return None, None


def normalize_invite_link(link: str) -> str:
    """Normalize t.me/joinchat or t.me/+ links."""
    link = link.strip()
    m = re.match(r'https?://t\.me/(?:joinchat/|\+)?(.+)', link)
    if m:
        return 'https://t.me/joinchat/' + m.group(1)
    return link


def parse_channel(target: str) -> str:
    """Return a channel identifier usable by Pyrogram."""
    target = target.strip()
    if target.startswith(('https://t.me/', 'http://t.me/')):
        target = target.split('t.me/', 1)[-1].split('/')[0]
    if not target.startswith('@'):
        target = '@' + target
    return target


def parse_chat_target(target: str):
    """Return a Pyrogram chat identifier from a public channel target.

    Telegram's ``/c/<id>`` links contain the internal channel id rather than
    a username.  Treat numeric ids as integers too; passing them through
    ``parse_channel`` would incorrectly turn them into ``@-100...``.
    """
    target = (target or '').strip()
    if not target:
        return None

    match = re.match(
        r'https?://t\.me/c/(\d+)(?:/\d+)?/?$', target, re.IGNORECASE
    )
    if match:
        return int(f'-100{match.group(1)}')

    if target.startswith(('+', 'https://t.me/+', 'http://t.me/+',
                           'https://t.me/joinchat/', 'http://t.me/joinchat/')):
        # An invite hash identifies an invitation, not the already-joined
        # chat.  It cannot be passed to leave_chat().
        return None

    if re.fullmatch(r'-?\d+', target):
        return int(target)

    return parse_channel(target)


# ---------------------------------------------------------------------------
# Per-account action
# ---------------------------------------------------------------------------

async def run_campaign_on_account(
    acc: dict,
    camp: dict,
    *,
    api_id: int,
    api_hash: str,
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

    session_str = acc.get('session') or acc.get('session_string')
    session_file = acc.get('session_file')
    identifier   = acc.get('phone') or acc.get('id') or acc.get('user_id', 'unknown')

    os.makedirs(workdir, exist_ok=True)

    try:
        if session_file and os.path.exists(session_file):
            client = Client(session_file.rstrip('.session'), api_id=api_id, api_hash=api_hash, no_updates=True)
        elif session_str:
            client = Client('_session', session_string=session_str, api_id=api_id, api_hash=api_hash, no_updates=True)
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
    target = camp.get('target', camp.get('camp_target', ''))

    try:
        async with client:
            if action in ('dm', 'camp_action_dm'):
                message = camp.get('message') or camp.get('camp_dm_message', '')
                if not message:
                    return {'ok': False, 'error': 'No DM message configured', 'fatal': False}
                peer = parse_channel(target)
                await client.send_message(peer, message)

            elif action in ('react', 'camp_action_react'):
                channel, msg_id = parse_post_url(target)
                if not channel:
                    channel = parse_channel(target)
                    msg_id = None
                reactions = camp.get('reactions') or camp.get('camp_reactions', ['👍'])
                reaction  = random.choice(reactions) if reactions else '👍'
                if msg_id:
                    await client.send_reaction(channel, msg_id, emoji=reaction)

            elif action in ('join', 'camp_action_join'):
                join_link = camp.get('join_link') or camp.get('camp_join_link', '')
                if join_link:
                    link = normalize_invite_link(join_link)
                    await client.join_chat(link)
                else:
                    peer = parse_channel(target)
                    await client.join_chat(peer)

            elif action in ('leave', 'camp_action_leave'):
                if not target:
                    return {'ok': False, 'error': 'No channel configured', 'fatal': False}

                peer = parse_chat_target(target)
                if peer is None:
                    return {
                        'ok': False,
                        'error': (
                            'Invite links cannot identify an already joined channel. '
                            'Use the channel username or numeric ID instead.'
                        ),
                        'fatal': False,
                    }
                await client.leave_chat(peer)

            elif action in ('leave_all', 'camp_action_leave_all'):
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

            elif action in ('view', 'camp_action_view'):
                channel, msg_id = parse_post_url(target)
                if channel and msg_id:
                    history = [msg_id]
                    await client.invoke(
                        __import__('pyrogram.raw.functions.messages', fromlist=['GetMessagesViews']).GetMessagesViews(
                            peer=await client.resolve_peer(channel),
                            id=history,
                            increment=True,
                        )
                    )
                # view is best-effort; always counts as ok
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
        return {'ok': False, 'error': str(e), 'fatal': fatal}


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
                   if str(a.get('phone') or a.get('user_id') or a.get('id', '')) in id_set]
    elif retry_ids:
        id_set = set(str(r) for r in retry_ids)
        working = [a for a in working
                   if str(a.get('phone') or a.get('user_id') or a.get('id', '')) in id_set]

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
            identifier = acc.get('phone') or str(acc.get('user_id') or acc.get('id', idx))

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
