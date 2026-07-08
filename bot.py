import os
import logging
from telegram import (Update, InlineKeyboardButton, InlineKeyboardMarkup,
                      WebAppInfo, MenuButtonWebApp)
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from telegram.error import TelegramError

# Load environment variables from .env file if available
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv not installed, use system env vars

# Shared business logic lives in media_service (also used by the mini app in
# webapp_server.py). The bot itself is mini-app only and no longer runs the
# in-chat search/request flow, so it needs almost none of it directly.
# get_item_year and should_auto_approve are re-exported here for test_auto_approve.py.
from media_service import get_item_year, should_auto_approve  # noqa: F401
from telegram_auth import ENABLE_GROUP_AUTH, AUTHORIZED_GROUP_CHAT_IDS

# Configure logging with rotation to prevent excessive storage usage
import logging.handlers
import os

# Get log level from environment (default: INFO, can be DEBUG/WARNING/ERROR)
LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO').upper()
log_level = getattr(logging, LOG_LEVEL, logging.INFO)

# Configure root logger
logger = logging.getLogger()
logger.setLevel(log_level)

# Remove existing handlers to avoid duplicates
logger.handlers.clear()

# Console handler (for Docker logs)
console_handler = logging.StreamHandler()
console_handler.setLevel(log_level)
console_formatter = logging.Formatter(
    '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
console_handler.setFormatter(console_formatter)
logger.addHandler(console_handler)

# Optional: File handler with rotation (if LOG_FILE is set)
log_file = os.getenv('LOG_FILE')
if log_file:
    # Rotate logs: 10MB max, keep 5 backup files
    file_handler = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=10 * 1024 * 1024,  # 10MB
        backupCount=5,  # Keep 5 backup files
        encoding='utf-8'
    )
    file_handler.setLevel(log_level)
    file_handler.setFormatter(console_formatter)
    logger.addHandler(file_handler)

# Get logger for this module
logger = logging.getLogger(__name__)

# List view button threshold - show "List View" button when more than this many results
LIST_VIEW_BUTTON_THRESHOLD = int(os.getenv('LIST_VIEW_BUTTON_THRESHOLD', '5'))

# Get environment variables
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
# Strip quotes if present (common when setting env vars in Docker/shell scripts)
if TELEGRAM_BOT_TOKEN:
    TELEGRAM_BOT_TOKEN = TELEGRAM_BOT_TOKEN.strip('"\' ')

if not TELEGRAM_BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN environment variable is required")

# Mini app URL (public HTTPS URL pointing at the webapp server, e.g. behind a
# reverse proxy). When set, the bot shows an "Open Mini App" button and runs
# the mini app web server alongside polling. (Group auth config is shared with
# the mini app and lives in telegram_auth.py.)
WEBAPP_URL = os.getenv('WEBAPP_URL', '').strip().strip('"\' ')


def is_message_from_authorized_group(update: Update) -> bool:
    """Check if message is from an authorized group chat.
    
    Returns True if the message is from a group/supergroup that's in AUTHORIZED_GROUP_CHAT_IDS.
    These messages should be completely ignored by the bot.
    """
    if not ENABLE_GROUP_AUTH or not AUTHORIZED_GROUP_CHAT_IDS:
        return False
    
    chat = update.effective_chat
    if not chat or chat.type not in ('group', 'supergroup'):
        return False
    
    chat_id_str = str(chat.id)
    if chat_id_str in AUTHORIZED_GROUP_CHAT_IDS:
        logger.info(f"Ignoring message from authorized group chat {chat_id_str}")
        return True
    
    return False


async def is_user_authorized(update: Update, context: ContextTypes.DEFAULT_TYPE) -> tuple[bool, str]:
    """Check if user is authorized to use the bot.
    
    Checks if user is a member of any of the authorized group chats.
    Only called for direct/private messages (group messages are handled separately).
    
    Returns:
        tuple: (is_authorized: bool, error_message: str)
        - If feature is disabled, returns (True, "")
        - If authorized (member of any group), returns (True, "")
        - If not authorized, returns (False, error_message)
    """
    # If feature is disabled, allow all users
    if not ENABLE_GROUP_AUTH:
        return (True, "")
    
    # If group chat IDs are not configured, log warning and allow access
    if not AUTHORIZED_GROUP_CHAT_IDS:
        logger.warning("ENABLE_GROUP_AUTH is enabled but AUTHORIZED_GROUP_CHAT_ID/AUTHORIZED_GROUP_CHAT_IDS is not set. Allowing access.")
        return (True, "")
    
    # Get user ID from update
    user = update.effective_user
    if not user:
        logger.warning("Could not get user from update")
        return (False, "❌ Unable to verify your identity. Please try again.")
    
    user_id = user.id
    
    # Track errors for logging
    last_error = None
    groups_checked = 0
    
    try:
        # Get the bot instance from the application
        bot = context.bot
        
        # Check if user is a member of ANY of the authorized groups
        for group_chat_id in AUTHORIZED_GROUP_CHAT_IDS:
            groups_checked += 1
            try:
                chat_member = await bot.get_chat_member(
                    chat_id=group_chat_id,
                    user_id=user_id
                )
                
                # Check if user is a valid member (not left, not kicked)
                # Valid statuses: 'member', 'administrator', 'creator'
                # Invalid statuses: 'left', 'kicked'
                # Note: 'left' status is returned both for users who left and users who were never in the group
                # Restricted members are allowed but logged
                status = chat_member.status
                if status in ('member', 'administrator', 'creator'):
                    logger.debug(f"User {user_id} ({user.username or 'no username'}) is authorized in group {group_chat_id} (status: {status})")
                    return (True, "")
                elif status == 'restricted':
                    # Restricted members might still be able to use the bot depending on permissions
                    # For now, we'll allow them but log it
                    logger.info(f"User {user_id} ({user.username or 'no username'}) is restricted member in group {group_chat_id}, allowing access")
                    return (True, "")
                elif status in ('left', 'kicked'):
                    # User left, was kicked, or was never in this group
                    # Continue checking other groups
                    logger.debug(f"User {user_id} ({user.username or 'no username'}) is not a member of group {group_chat_id} (status: {status})")
                    continue
                else:
                    # Unknown status - continue checking other groups
                    logger.warning(f"User {user_id} ({user.username or 'no username'}) has unknown status in group {group_chat_id}: {status}")
                    continue
            
            except TelegramError as e:
                error_msg = str(e).lower()
                last_error = e
                
                # Handle specific error cases
                if "chat not found" in error_msg or "group chat was upgraded to a supergroup" in error_msg:
                    logger.warning(f"Authorized group chat not found or upgraded. Chat ID: {group_chat_id}. Error: {e}")
                    # Continue checking other groups
                    continue
                elif "user not found" in error_msg or "user is not a member" in error_msg:
                    # User was never in this group or left before bot could track them
                    # Continue checking other groups
                    logger.debug(f"User {user_id} ({user.username or 'no username'}) not found in group {group_chat_id} (never joined or left)")
                    continue
                elif "bot is not a member" in error_msg:
                    logger.warning(f"Bot is not a member of group {group_chat_id}. Skipping this group.")
                    # Continue checking other groups
                    continue
                else:
                    # Unknown error - log but continue checking other groups
                    logger.warning(f"Error checking group membership for user {user_id} in group {group_chat_id}: {e}")
                    continue
        
        # User is not a member of any authorized group
        if groups_checked > 0:
            logger.info(f"User {user_id} ({user.username or 'no username'}) is not a member of any authorized group (checked {groups_checked} group(s))")
            return (False, "❌ You are not authorized to use this bot. Contact admin and please join the authorized group to request content.")
        else:
            # No groups were successfully checked (all had errors)
            logger.error(f"Could not check any authorized groups for user {user_id}. Last error: {last_error}")
            return (False, "❌ Bot configuration error. Please contact the administrator.")
    
    except Exception as e:
        # Unexpected error
        logger.error(f"Unexpected error checking authorization for user {user_id}: {e}", exc_info=True)
        return (False, "❌ An error occurred while checking authorization. Please try again later.")


def mini_app_markup(chat=None) -> InlineKeyboardMarkup | None:
    """Build a keyboard with a single button that opens the mini app.

    Telegram only allows web_app inline buttons in private chats, so in group
    chats (or when WEBAPP_URL is unset) there's no button to show.
    """
    if WEBAPP_URL and (chat is None or chat.type == 'private'):
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton("✨ Open Mini App", web_app=WebAppInfo(url=WEBAPP_URL))]]
        )
    return None


async def prompt_open_mini_app(update: Update):
    """Reply telling the user to open the mini app to make requests.

    This bot is mini-app only: all searching and requesting happens inside the
    mini app, so every message is answered with a nudge to open it.
    """
    reply_markup = mini_app_markup(update.effective_chat)

    if reply_markup is not None:
        text = (
            "🎬 <b>Sparky Requests</b>\n\n"
            "Tap the button below (or the <b>Request</b> menu button next to the "
            "message box) to browse and request movies &amp; TV shows."
        )
    elif WEBAPP_URL:
        # Group chat: web_app buttons aren't allowed, so link the URL directly.
        text = (
            "🎬 <b>Sparky Requests</b>\n\n"
            f"Open the mini app to make requests: {WEBAPP_URL}"
        )
    else:
        text = (
            "🎬 <b>Sparky Requests</b>\n\n"
            "The mini app isn't configured yet. Please contact the administrator."
        )

    await update.effective_message.reply_text(
        text,
        reply_markup=reply_markup,
        parse_mode='HTML',
        disable_web_page_preview=True,
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command - point the user to the mini app."""
    # Ignore messages from authorized group chats
    if is_message_from_authorized_group(update):
        return

    # Check authorization
    is_authorized, error_msg = await is_user_authorized(update, context)
    if not is_authorized:
        await update.message.reply_text(error_msg, parse_mode='HTML')
        return

    await prompt_open_mini_app(update)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle callback queries.

    The bot is mini-app only, so it no longer sends inline request buttons. This
    only fires for stale buttons left in old chat history; answer them by
    pointing the user back to the mini app.
    """
    # Ignore callbacks from authorized group chats
    if is_message_from_authorized_group(update):
        return

    query = update.callback_query

    # Check authorization
    is_authorized, error_msg = await is_user_authorized(update, context)
    if not is_authorized:
        await query.answer(error_msg, show_alert=True)
        return

    await query.answer()
    await prompt_open_mini_app(update)


async def handle_search_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle any text message - point the user to the mini app.

    This bot is mini-app only, so plain messages don't trigger an in-chat
    search; they just nudge the user to open the mini app and request there.
    """
    # Ignore messages from authorized group chats
    if is_message_from_authorized_group(update):
        return

    # Check authorization
    is_authorized, error_msg = await is_user_authorized(update, context)
    if not is_authorized:
        await update.message.reply_text(error_msg, parse_mode='HTML')
        return

    await prompt_open_mini_app(update)


async def post_init(application: Application) -> None:
    """Start the mini app server and set the chat menu button (when configured)."""
    if not WEBAPP_URL:
        logger.info("WEBAPP_URL not set - mini app disabled, messaging only")
        return

    # Run the mini app server on the bot's event loop (no separate process)
    try:
        from webapp_server import start_webapp_server
        application.bot_data['webapp_runner'] = await start_webapp_server(TELEGRAM_BOT_TOKEN)
    except Exception as e:
        logger.error(f"Failed to start mini app server: {e}", exc_info=True)

    # Show the mini app in the chat menu button (next to the message box)
    try:
        await application.bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text="Request", web_app=WebAppInfo(url=WEBAPP_URL))
        )
        logger.info("Chat menu button set to open the mini app")
    except TelegramError as e:
        logger.warning(f"Could not set chat menu button: {e}")


async def post_shutdown(application: Application) -> None:
    """Stop the mini app server on shutdown."""
    runner = application.bot_data.get('webapp_runner')
    if runner:
        await runner.cleanup()


def main():
    """Start the bot."""

    # Create application
    application = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(handle_callback))
    
    # Handle text messages (search queries)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_search_message))
    
    # Start the bot
    # Polling is efficient for up to ~500 concurrent users
    # For 100 users, this is more than sufficient
    logger.info("Starting bot...")
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,  # Ignore old updates on restart
        poll_interval=1.0,          # Check every 1 second (good balance)
        timeout=20,                  # Request timeout
        bootstrap_retries=5,         # Retry on startup failures
    )


if __name__ == '__main__':
    main()
