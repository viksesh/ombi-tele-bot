"""aiohttp server backing the Telegram mini app.

Serves the static frontend from webapp/ and a small JSON API on top of
media_service. Every API call is authenticated with Telegram WebApp initData
(HMAC-validated against the bot token) plus the same group-membership rules
as the messaging bot.

Runs inside the bot process on the python-telegram-bot event loop (started
from bot.py's post_init hook), so no extra process or deployment is needed.
"""

import asyncio
import logging
import os
import time

from aiohttp import web

import media_service
import telegram_auth

logger = logging.getLogger(__name__)

WEBAPP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'webapp')
WEBAPP_PORT = int(os.getenv('WEBAPP_PORT', '8080'))

# Per-user last search results (raw Ombi items), so /api/request can run the
# same auto-approve checks as the bot. Mirrors PTB's context.user_data.
SESSION_TTL_SECONDS = 3600
_sessions: dict = {}  # user_id -> {'ts': float, 'movie': [...], 'tv': [...]}


def _get_session(user_id: int) -> dict:
    now = time.time()
    # Evict stale sessions to keep memory bounded
    for uid in [uid for uid, s in _sessions.items() if now - s['ts'] > SESSION_TTL_SECONDS]:
        _sessions.pop(uid, None)
    session = _sessions.setdefault(user_id, {'ts': now})
    session['ts'] = now
    return session


def _json_error(message: str, status: int = 400) -> web.Response:
    return web.json_response({'ok': False, 'error': message}, status=status)


@web.middleware
async def auth_middleware(request: web.Request, handler):
    """Authenticate API requests via Telegram WebApp initData."""
    if not request.path.startswith('/api/'):
        return await handler(request)

    bot_token = request.app['bot_token']
    init_data = request.headers.get('X-Telegram-Init-Data', '')
    user = telegram_auth.validate_init_data(init_data, bot_token)
    if not user:
        return _json_error("Authentication failed. Please reopen the app from Telegram.", status=401)

    # Group membership check (same rules as the bot), off the event loop
    is_member, error_msg = await asyncio.to_thread(
        telegram_auth.is_group_member, bot_token, user['id']
    )
    if not is_member:
        return _json_error(error_msg, status=403)

    request['user'] = user
    return await handler(request)


async def handle_search(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return _json_error("Invalid request body.")

    item_type = body.get('type')
    raw_query = (body.get('query') or '').strip()

    if item_type not in ('movie', 'tv'):
        return _json_error("Invalid type. Must be 'movie' or 'tv'.")
    if not raw_query:
        return _json_error("Please enter a title or IMDb link.")

    user_id = request['user']['id']
    logger.info(f"Mini app search from user {user_id}: type={item_type}, query='{raw_query}'")

    try:
        results = await asyncio.to_thread(media_service.search_from_raw_query, item_type, raw_query)
    except media_service.ImdbLookupError as e:
        return _json_error(str(e))
    except RuntimeError as e:
        logger.error(f"Search failed: {e}")
        return _json_error("Server is not configured correctly. Please contact the administrator.", status=500)
    except Exception as e:
        logger.error(f"Error searching {item_type}: {e}", exc_info=True)
        return _json_error("An error occurred while searching. Please try again later.", status=500)

    # Store raw results for the follow-up /api/request auto-approve checks
    session = _get_session(user_id)
    session[item_type] = results

    normalized = [media_service.normalize_item(item, item_type) for item in results]
    # Drop items with no usable ID - they can't be requested or detailed
    normalized = [n for n in normalized if n['id']]
    return web.json_response({'ok': True, 'results': normalized})


def _find_session_item(user_id: int, item_type: str, item_id) -> dict | None:
    session = _sessions.get(user_id) or {}
    for item in session.get(item_type, []) or []:
        if str(media_service.get_item_id(item)) == str(item_id):
            return item
    return None


async def handle_request_item(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return _json_error("Invalid request body.")

    item_type = body.get('type')
    item_id = body.get('id')

    if item_type not in ('movie', 'tv'):
        return _json_error("Invalid type. Must be 'movie' or 'tv'.")
    if not item_id:
        return _json_error("Missing item id.")

    user_id = request['user']['id']
    item = _find_session_item(user_id, item_type, item_id)

    # Same guard as the bot's quick request: don't re-request available items
    if item:
        should_hide, status = media_service.get_item_status(item)
        if should_hide:
            return web.json_response({'ok': False, 'error': 'already_handled', 'status': status})

    logger.info(f"Mini app request from user {user_id}: type={item_type}, id={item_id}")

    try:
        success, auto_approved = await asyncio.to_thread(
            media_service.submit_request, item_type, item_id, item
        )
    except RuntimeError:
        return _json_error("Server is not configured correctly. Please contact the administrator.", status=500)
    except Exception as e:
        logger.error(f"Error requesting item: {e}", exc_info=True)
        return _json_error("An error occurred while submitting the request. Please try again.", status=500)

    if not success:
        return _json_error("Failed to submit request. Please try again.", status=502)

    return web.json_response({'ok': True, 'autoApproved': auto_approved})


async def handle_details(request: web.Request) -> web.Response:
    """Return an enriched item (e.g. with IMDB link) for the detail view."""
    item_type = request.query.get('type')
    item_id = request.query.get('id')

    if item_type not in ('movie', 'tv') or not item_id:
        return _json_error("Invalid parameters.")

    user_id = request['user']['id']
    item = _find_session_item(user_id, item_type, item_id)

    def _enrich():
        if item is not None:
            if item_type == 'movie':
                media_service.enrich_movie_imdb(item)
            return item
        # Session expired - fetch fresh details from Ombi
        if not media_service.ombi_client:
            return None
        if item_type == 'movie':
            return media_service.ombi_client.get_movie_info(item_id)
        return media_service.ombi_client.get_tv_info(item_id)

    try:
        enriched = await asyncio.to_thread(_enrich)
    except Exception as e:
        logger.error(f"Error fetching details: {e}", exc_info=True)
        return _json_error("Could not load details.", status=500)

    if not enriched:
        return _json_error("Item not found.", status=404)

    return web.json_response({'ok': True, 'item': media_service.normalize_item(enriched, item_type)})


def _build_index_html() -> str:
    """Read the frontend files and inline CSS/JS into a single self-contained page.

    Serving one document with no /static sub-requests avoids tunnel quirks
    (e.g. ngrok's free-tier interstitial serving warning pages for CSS/JS) and
    keeps the mini app to a single request. Source files stay separate on disk.
    """
    with open(os.path.join(WEBAPP_DIR, 'index.html'), encoding='utf-8') as f:
        html = f.read()
    with open(os.path.join(WEBAPP_DIR, 'style.css'), encoding='utf-8') as f:
        css = f.read()
    with open(os.path.join(WEBAPP_DIR, 'app.js'), encoding='utf-8') as f:
        js = f.read()

    html = html.replace(
        '<link rel="stylesheet" href="/static/style.css">',
        f'<style>\n{css}\n</style>',
    )
    html = html.replace(
        '<script src="/static/app.js"></script>',
        f'<script>\n{js}\n</script>',
    )
    return html


async def handle_index(request: web.Request) -> web.Response:
    # Rebuilt per request so edits show up on refresh during local testing;
    # the files are tiny so the cost is negligible.
    return web.Response(text=_build_index_html(), content_type='text/html')


def create_app(bot_token: str) -> web.Application:
    app = web.Application(middlewares=[auth_middleware])
    app['bot_token'] = bot_token
    app.router.add_get('/', handle_index)
    app.router.add_post('/api/search', handle_search)
    app.router.add_post('/api/request', handle_request_item)
    app.router.add_get('/api/details', handle_details)
    app.router.add_static('/static/', WEBAPP_DIR)
    return app


async def start_webapp_server(bot_token: str, port: int = WEBAPP_PORT) -> web.AppRunner:
    """Start the mini app server on the current event loop. Returns the runner for shutdown."""
    app = create_app(bot_token)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logger.info(f"Mini app server listening on port {port}")
    return runner
