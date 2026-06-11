"""Shared Telegram authorization config and helpers.

Used by both the bot (bot.py) and the mini app web server (webapp_server.py):
- group-auth environment parsing
- Telegram WebApp initData signature validation
- a synchronous group-membership check (Bot API over HTTP) with a small cache
"""

import hashlib
import hmac
import json
import logging
import os
import time
from urllib.parse import parse_qsl

import requests

# Load environment variables before reading any config
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv not installed, use system env vars

logger = logging.getLogger(__name__)

# Group authorization feature flag (shared between bot and mini app)
ENABLE_GROUP_AUTH = os.getenv('ENABLE_GROUP_AUTH', '').lower() in ('true', '1', 'yes')
AUTHORIZED_GROUP_CHAT_IDS_STR = os.getenv('AUTHORIZED_GROUP_CHAT_ID', '').strip() or os.getenv('AUTHORIZED_GROUP_CHAT_IDS', '').strip()
# Parse comma-separated list of group chat IDs, supporting both singular and plural env var names
AUTHORIZED_GROUP_CHAT_IDS = [gid.strip() for gid in AUTHORIZED_GROUP_CHAT_IDS_STR.split(',') if gid.strip()] if AUTHORIZED_GROUP_CHAT_IDS_STR else []

# Chat member statuses that count as "member of the group"
ALLOWED_MEMBER_STATUSES = ('member', 'administrator', 'creator', 'restricted')

# initData older than this is rejected (Telegram recommends validating auth_date)
INIT_DATA_MAX_AGE_SECONDS = int(os.getenv('WEBAPP_INIT_DATA_MAX_AGE', str(24 * 3600)))

# Cache membership lookups so the mini app doesn't hit the Bot API on every request
MEMBERSHIP_CACHE_TTL_SECONDS = 300
_membership_cache: dict = {}  # user_id -> (timestamp, is_member)


def validate_init_data(init_data: str, bot_token: str) -> dict | None:
    """Validate Telegram WebApp initData and return the parsed user dict.

    Implements the algorithm from
    https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app

    Returns the user dict (with at least 'id') on success, None on failure.
    """
    if not init_data or not bot_token:
        return None

    try:
        params = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = params.pop('hash', None)
        if not received_hash:
            logger.warning("initData missing hash")
            return None

        # signature is used for third-party validation; not part of the hash check
        params.pop('signature', None)

        data_check_string = '\n'.join(f"{k}={v}" for k, v in sorted(params.items()))
        secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

        if not hmac.compare_digest(computed_hash, received_hash):
            logger.warning("initData hash mismatch - rejecting request")
            return None

        # Reject stale initData (replay protection)
        auth_date = int(params.get('auth_date', 0))
        if auth_date and (time.time() - auth_date) > INIT_DATA_MAX_AGE_SECONDS:
            logger.warning("initData is too old - rejecting request")
            return None

        user = json.loads(params.get('user', '{}'))
        if not user.get('id'):
            logger.warning("initData has no user id")
            return None
        return user
    except Exception as e:
        logger.warning(f"Failed to validate initData: {e}")
        return None


def is_group_member(bot_token: str, user_id: int) -> tuple[bool, str]:
    """Check if a user is a member of any authorized group (synchronous Bot API call).

    Mirrors the bot's is_user_authorized logic. Returns (is_authorized, error_message).
    """
    if not ENABLE_GROUP_AUTH:
        return (True, "")

    if not AUTHORIZED_GROUP_CHAT_IDS:
        logger.warning("ENABLE_GROUP_AUTH is enabled but AUTHORIZED_GROUP_CHAT_ID/AUTHORIZED_GROUP_CHAT_IDS is not set. Allowing access.")
        return (True, "")

    cached = _membership_cache.get(user_id)
    if cached and (time.time() - cached[0]) < MEMBERSHIP_CACHE_TTL_SECONDS:
        if cached[1]:
            return (True, "")
        return (False, "You are not authorized to use this bot. Please join the authorized group to request content.")

    groups_checked = 0
    for group_chat_id in AUTHORIZED_GROUP_CHAT_IDS:
        groups_checked += 1
        try:
            resp = requests.get(
                f"https://api.telegram.org/bot{bot_token}/getChatMember",
                params={'chat_id': group_chat_id, 'user_id': user_id},
                timeout=10,
            )
            data = resp.json()
            if data.get('ok'):
                status = data['result'].get('status')
                if status in ALLOWED_MEMBER_STATUSES:
                    logger.debug(f"User {user_id} is authorized in group {group_chat_id} (status: {status})")
                    _membership_cache[user_id] = (time.time(), True)
                    return (True, "")
                logger.debug(f"User {user_id} is not a member of group {group_chat_id} (status: {status})")
            else:
                logger.debug(f"getChatMember failed for user {user_id} in group {group_chat_id}: {data.get('description')}")
        except requests.RequestException as e:
            logger.warning(f"Error checking group membership for user {user_id} in group {group_chat_id}: {e}")
            continue

    if groups_checked > 0:
        logger.info(f"User {user_id} is not a member of any authorized group (checked {groups_checked} group(s))")
        _membership_cache[user_id] = (time.time(), False)
        return (False, "You are not authorized to use this bot. Please join the authorized group to request content.")

    return (False, "Bot configuration error. Please contact the administrator.")
