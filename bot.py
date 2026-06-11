import os
import logging
import re
from telegram import (Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto,
                      WebAppInfo, MenuButtonWebApp)
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from telegram.error import TelegramError

# Load environment variables from .env file if available
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv not installed, use system env vars

# Shared business logic (also used by the mini app in webapp_server.py)
import media_service
from media_service import (
    ombi_client,
    ImdbLookupError,
    get_item_status,
    get_item_year,
    get_item_rating,
    get_item_popularity,
    get_poster_url,
    should_auto_approve,
)
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


def format_item_description(item: dict, item_type: str) -> str:
    """Format item description for display."""
    should_hide, status = get_item_status(item)
    
    if item_type == 'movie':
        # Try multiple possible field names for title
        title = (item.get('title') or item.get('Title') or 
                item.get('movieTitle') or item.get('name') or 'Unknown')
        
        # Try multiple possible field names for year
        year = (item.get('releaseDate') or item.get('release_date') or 
               item.get('year') or item.get('Year') or 
               item.get('releaseYear'))
        if year:
            if isinstance(year, str) and len(year) >= 4:
                year = year[:4]
        
        # Try multiple possible field names for overview
        overview = (item.get('overview') or item.get('Overview') or 
                   item.get('description') or item.get('Description') or 
                   item.get('plot') or item.get('Plot') or 
                   'No description available.')
        
        # Try multiple possible field names for rating
        rating = (item.get('voteAverage') or item.get('vote_average') or 
                 item.get('rating') or item.get('Rating') or 
                 item.get('voteRating') or 0)
        if rating:
            try:
                rating = float(rating)
            except (ValueError, TypeError):
                rating = 0
        
        text = f"🎬 <b>{title}</b>"
        if year:
            text += f" ({year})"

        # Add IMDB link if available
        imdb_id = item.get('imdbId') or item.get('imdbid')
        if imdb_id:
            text += f" 🔗 <a href=\"https://www.imdb.com/title/{imdb_id}/\">IMDB</a>"

        # Add status indicator on new line
        if status == 'denied':
            text += "\n❌ <b>Not Available</b>"
        elif status == 'available':
            text += "\n✅ <b>Already Available</b>"
        elif status == 'partially_available':
            text += "\n✅ <b>Partially Available</b>"
        elif status == 'approved':
            text += "\n✅ <b>Approved - Scheduled to upload when available digitally</b>"
        elif status == 'requested':
            text += "\n⏳ <b>Requested - Pending approval</b>"

        if rating and rating > 0:
            text += f"\n\n⭐ Rating: {rating:.1f}/10\n\n"
        else:
            text += "\n\n"
        text += f"{overview[:300]}"
        if len(overview) > 300:
            text += "..."
        return text
    else:  # tv
        # Try multiple possible field names for title (Ombi uses 'title' for TV shows)
        title = (item.get('title') or item.get('Title') or 
                item.get('name') or item.get('Name') or 
                item.get('seriesName') or item.get('tvShowName') or 'Unknown')
        
        # Try multiple possible field names for year (Ombi uses 'firstAired')
        year = (item.get('firstAired') or item.get('firstAirDate') or 
               item.get('first_air_date') or item.get('year') or 
               item.get('Year') or item.get('releaseYear') or 
               item.get('firstAirYear'))
        if year:
            if isinstance(year, str) and len(year) >= 4:
                # Extract year from date string like "2025-11-07"
                year_match = re.search(r'(\d{4})', str(year))
                if year_match:
                    year = year_match.group(1)
                else:
                    year = str(year)[:4]
        
        # Try multiple possible field names for overview
        overview = (item.get('overview') or item.get('Overview') or 
                   item.get('description') or item.get('Description') or 
                   item.get('plot') or item.get('Plot') or 
                   'No description available.')
        
        # Try multiple possible field names for rating (Ombi uses 'rating' as string)
        rating = (item.get('rating') or item.get('Rating') or 
                 item.get('voteAverage') or item.get('vote_average') or 
                 item.get('siteRating') or item.get('voteRating') or 0)
        if rating:
            try:
                rating = float(rating)
                # Convert from 0-1 scale to 0-10 scale if needed (Ombi sometimes uses 0-1)
                if rating > 0 and rating <= 1:
                    rating = rating * 10
            except (ValueError, TypeError):
                rating = 0
        
        text = f"📺 <b>{title}</b>"
        if year:
            text += f" ({year})"

        # Add IMDB link if available
        imdb_id = item.get('imdbId') or item.get('imdbid')
        if imdb_id:
            text += f" 🔗 <a href=\"https://www.imdb.com/title/{imdb_id}/\">IMDB</a>"

        # Add status indicator on new line
        if status == 'denied':
            text += "\n❌ <b>Not Available</b>"
        elif status == 'available':
            text += "\n✅ <b>Already Available</b>"
        elif status == 'partially_available':
            text += "\n✅ <b>Partially Available</b>"
        elif status == 'approved':
            text += "\n✅ <b>Approved - Scheduled to upload when available digitally</b>"
        elif status == 'requested':
            text += "\n⏳ <b>Requested - Pending approval</b>"

        if rating and rating > 0:
            text += f"\n\n⭐ Rating: {rating:.1f}/10\n\n"
        else:
            text += "\n\n"
        text += f"{overview[:300]}"
        if len(overview) > 300:
            text += "..."
        return text


def main_menu_markup(chat=None) -> InlineKeyboardMarkup:
    """Build the main menu keyboard, including the mini app button when configured.

    Telegram only allows web_app inline buttons in private chats, so the mini
    app button is omitted for group chats.
    """
    keyboard = [
        [InlineKeyboardButton("🎬 Request Movie", callback_data="req_movie")],
        [InlineKeyboardButton("📺 Request TV Show", callback_data="req_tv")]
    ]
    if WEBAPP_URL and (chat is None or chat.type == 'private'):
        keyboard.append([InlineKeyboardButton("✨ Open Mini App", web_app=WebAppInfo(url=WEBAPP_URL))])
    return InlineKeyboardMarkup(keyboard)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command - show main menu."""
    # Ignore messages from authorized group chats
    if is_message_from_authorized_group(update):
        return

    # Check authorization
    is_authorized, error_msg = await is_user_authorized(update, context)
    if not is_authorized:
        await update.message.reply_text(error_msg, parse_mode='HTML')
        return

    reply_markup = main_menu_markup(update.effective_chat)

    await update.message.reply_text(
        "🎬 <b>Welcome to Sparky Requests Bot!</b>\n\n"
        "Choose what you'd like to request:",
        reply_markup=reply_markup,
        parse_mode='HTML'
    )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle callback queries."""
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
    
    data = query.data
    
    if data == "req_movie":
        # Store that user wants to request a movie
        context.user_data['request_type'] = 'movie'
        await query.edit_message_text(
            "🎬 <b>Request Movie</b>\n\n"
            "Please send me the movie title or IMDb link.\n\n"
            "Examples:\n"
            "• The Matrix\n"
            "• https://www.imdb.com/title/tt0133093/",
            parse_mode='HTML'
        )
        return
    
    elif data == "req_tv":
        # Store that user wants to request a TV show
        context.user_data['request_type'] = 'tv'
        await query.edit_message_text(
            "📺 <b>Request TV Show</b>\n\n"
            "Please send me the TV show title or IMDb link.\n\n"
            "Examples:\n"
            "• Breaking Bad\n"
            "• https://www.imdb.com/title/tt0903747/",
            parse_mode='HTML'
        )
        return
    
    elif data.startswith("show_"):
        # Show a specific result (format: show_TYPE_INDEX)
        parts = data.split("_")
        if len(parts) >= 3:
            item_type = parts[1]
            index = int(parts[2])
            await show_result(query, context, item_type, index)
        return
    
    elif data.startswith("request_"):
        # Request an item (format: request_TYPE_ID)
        parts = data.split("_")
        if len(parts) >= 3:
            item_type = parts[1]
            item_id = int(parts[2])
            await handle_request(query, context, item_type, item_id)
        return
    
    elif data.startswith("cancel_"):
        # Cancel and return to main menu
        await start_from_callback(query, context)
        return
    
    elif data.startswith("next_"):
        # Show next result (format: next_TYPE_CURRENTINDEX)
        parts = data.split("_")
        if len(parts) >= 3:
            item_type = parts[1]
            current_index = int(parts[2])
            await show_result(query, context, item_type, current_index + 1)
        return
    
    elif data.startswith("prev_"):
        # Show previous result (format: prev_TYPE_CURRENTINDEX)
        parts = data.split("_")
        if len(parts) >= 3:
            item_type = parts[1]
            current_index = int(parts[2])
            await show_result(query, context, item_type, current_index - 1)
        return

    elif data.startswith("sel_"):
        # Select item from list view to show details (format: sel_TYPE_INDEX)
        parts = data.split("_")
        if len(parts) >= 3:
            item_type = parts[1]
            index = int(parts[2])
            await show_result(query, context, item_type, index)
        return

    elif data.startswith("qreq_"):
        # Quick request from list view (format: qreq_TYPE_INDEX)
        parts = data.split("_")
        if len(parts) >= 3:
            item_type = parts[1]
            index = int(parts[2])
            await handle_quick_request(query, context, item_type, index)
        return

    elif data.startswith("listpage_"):
        # Pagination in list view (format: listpage_TYPE_PAGE)
        parts = data.split("_")
        if len(parts) >= 3:
            item_type = parts[1]
            page = int(parts[2])
            await show_results_list_from_callback(query, context, item_type, page)
        return

    elif data.startswith("status_"):
        # Status button clicked (format: status_TYPE_INDEX) - just show info
        parts = data.split("_")
        if len(parts) >= 3:
            item_type = parts[1]
            index = int(parts[2])
            results_key = f'{item_type}_results'
            if results_key in context.user_data:
                results = context.user_data[results_key]
                if 0 <= index < len(results):
                    item = results[index]
                    should_hide, status = get_item_status(item)
                    if status == 'available':
                        await query.answer("✅ Already available in library!", show_alert=True)
                    elif status == 'partially_available':
                        await query.answer("✅ Partially available - some content in library", show_alert=True)
                    elif status == 'approved':
                        await query.answer("✅ Already approved - will be added when available", show_alert=True)
                    elif status == 'requested':
                        await query.answer("⏳ Already requested - pending approval", show_alert=True)
                    elif status == 'denied':
                        await query.answer("❌ Request denied", show_alert=True)
        return

    elif data.startswith("backlist_"):
        # Back to list from detail view (format: backlist_TYPE)
        parts = data.split("_")
        if len(parts) >= 2:
            item_type = parts[1]
            page = context.user_data.get(f'{item_type}_list_page', 0)
            await show_results_list_from_callback(query, context, item_type, page)
        return

    elif data.startswith("tolist_"):
        # Switch to list view from detail view (format: tolist_TYPE)
        parts = data.split("_")
        if len(parts) >= 2:
            item_type = parts[1]
            # Set from_list flag so back button works correctly
            context.user_data[f'{item_type}_from_list'] = True
            await show_results_list_from_callback(query, context, item_type, 0)
        return


async def handle_quick_request(query, context: ContextTypes.DEFAULT_TYPE, item_type: str, index: int):
    """Handle quick request from list view."""
    results_key = f'{item_type}_results'
    if results_key not in context.user_data:
        await query.answer("❌ No search results found.", show_alert=True)
        return

    results = context.user_data[results_key]
    if index < 0 or index >= len(results):
        await query.answer("❌ Invalid selection.", show_alert=True)
        return

    item = results[index]
    item_id = (item.get('theTvDbId') or item.get('theMovieDbId') or
               item.get('tvDbId') or item.get('tvdbId') or
               item.get('id') or item.get('Id') or
               item.get('movieDbId') or item.get('seriesId'))

    if not item_id:
        await query.answer("❌ Invalid item.", show_alert=True)
        return

    # Check if already requested
    should_hide, status = get_item_status(item)
    if should_hide:
        if status == 'available':
            await query.answer("✅ Already available!", show_alert=True)
        elif status == 'requested':
            await query.answer("⏳ Already requested!", show_alert=True)
        elif status == 'approved':
            await query.answer("✅ Already approved!", show_alert=True)
        return

    try:
        # Shared request pipeline (season fetch, auto-approve checks, submit)
        success, auto_approved = media_service.submit_request(item_type, item_id, item)

        if success:
            await query.answer("✅ Request submitted!", show_alert=True)
            # Refresh the list view
            page = context.user_data.get(f'{item_type}_list_page', 0)
            await show_results_list_from_callback(query, context, item_type, page)
        else:
            await query.answer("❌ Failed to submit request.", show_alert=True)
    except Exception as e:
        logger.error(f"Error in quick request: {e}", exc_info=True)
        await query.answer("❌ An error occurred.", show_alert=True)


async def show_result(query, context: ContextTypes.DEFAULT_TYPE, item_type: str, index: int):
    """Show a specific search result."""
    results_key = f'{item_type}_results'
    if results_key not in context.user_data:
        await query.edit_message_text("❌ No search results found. Please start a new search.")
        return
    
    results = context.user_data[results_key]
    
    if index < 0 or index >= len(results):
        await query.answer("No more results!")
        return
    
    item = results[index]
    # Try multiple possible field names for ID (Ombi uses theTvDbId for TV shows)
    item_id = (item.get('theTvDbId') or item.get('theMovieDbId') or  # TV shows use theTvDbId
               item.get('tvDbId') or item.get('tvdbId') or 
               item.get('id') or item.get('Id') or 
               item.get('movieDbId') or item.get('movie_id') or 
               item.get('tmdbId') or item.get('tmdb_id') or
               item.get('seriesId'))  # Fallback to seriesId for TV
    
    if not item_id:
        await query.edit_message_text("❌ Invalid item. Please try again.")
        return

    # For movies, fetch detailed info to get IMDB ID (search results don't include it)
    if item_type == 'movie':
        media_service.enrich_movie_imdb(item)

    # Format description
    description = format_item_description(item, item_type)
    
    # Check if item should hide request button (available, approved, or requested)
    should_hide, status = get_item_status(item)
    logger.debug(f"Item status check: should_hide={should_hide}, status={status}, partlyAvailable={item.get('partlyAvailable')}")
    
    # Get poster URL
    poster_url = get_poster_url(item)
    
    # Check if we came from list view
    from_list = context.user_data.get(f'{item_type}_from_list', False)

    # Create navigation buttons
    keyboard = []

    # Navigation buttons (only show if not from list view, since list has its own nav)
    if not from_list:
        nav_buttons = []
        if index > 0:
            nav_buttons.append(InlineKeyboardButton("◀️ Previous", callback_data=f"prev_{item_type}_{index}"))
        if index < len(results) - 1:
            nav_buttons.append(InlineKeyboardButton("Next ▶️", callback_data=f"next_{item_type}_{index}"))
        # Add List View button when many results and not from list
        if len(results) > LIST_VIEW_BUTTON_THRESHOLD:
            nav_buttons.append(InlineKeyboardButton("📋 List View", callback_data=f"tolist_{item_type}"))
        if nav_buttons:
            keyboard.append(nav_buttons)

    # Action buttons - only show Request if not available/approved/requested
    action_buttons = []
    if not should_hide:
        action_buttons.append(InlineKeyboardButton("✅ Request", callback_data=f"request_{item_type}_{item_id}"))

    # Add Back to List button if from list view, otherwise Cancel
    if from_list:
        action_buttons.append(InlineKeyboardButton("📋 Back to List", callback_data=f"backlist_{item_type}"))
    action_buttons.append(InlineKeyboardButton("❌ Cancel", callback_data="cancel_main"))
    keyboard.append(action_buttons)

    reply_markup = InlineKeyboardMarkup(keyboard)
    
    # Send message with poster if available
    try:
        # Check if current message is a photo
        is_photo = query.message.photo is not None and len(query.message.photo) > 0
        
        if poster_url:
            if is_photo:
                # Edit existing photo
                await query.edit_message_media(
                    media=InputMediaPhoto(
                        media=poster_url,
                        caption=description + f"\n\n📊 Result {index + 1} of {len(results)}",
                        parse_mode='HTML'
                    ),
                    reply_markup=reply_markup
                )
            else:
                # Replace text message with photo
                await query.message.delete()
                await query.message.reply_photo(
                    photo=poster_url,
                    caption=description + f"\n\n📊 Result {index + 1} of {len(results)}",
                    reply_markup=reply_markup,
                    parse_mode='HTML'
                )
        else:
            # No poster, use text
            if is_photo:
                # Replace photo with text
                await query.message.delete()
                await query.message.reply_text(
                    description + f"\n\n📊 Result {index + 1} of {len(results)}",
                    reply_markup=reply_markup,
                    parse_mode='HTML'
                )
            else:
                # Edit text message
                await query.edit_message_text(
                    description + f"\n\n📊 Result {index + 1} of {len(results)}",
                    reply_markup=reply_markup,
                    parse_mode='HTML'
                )
    except Exception as e:
        logger.error(f"Error showing result: {e}", exc_info=True)
        # Fallback: try to edit as text
        try:
            await query.edit_message_text(
                description + f"\n\n📊 Result {index + 1} of {len(results)}",
                reply_markup=reply_markup,
                parse_mode='HTML'
            )
        except:
            # Last resort: send new message
            await query.message.reply_text(
                description + f"\n\n📊 Result {index + 1} of {len(results)}",
                reply_markup=reply_markup,
                parse_mode='HTML'
            )


async def handle_request(query, context: ContextTypes.DEFAULT_TYPE, item_type: str, item_id: int):
    """Handle request for an item."""
    if not ombi_client:
        await query.answer("❌ Ombi client not configured!", show_alert=True)
        return

    try:
        # Get the item from stored results to check for auto-approve conditions
        results_key = f'{item_type}_results'
        item = None
        if results_key in context.user_data:
            results = context.user_data[results_key]
            # Find the item with matching ID
            for result in results:
                result_id = (result.get('theTvDbId') or result.get('theMovieDbId') or
                             result.get('tvDbId') or result.get('tvdbId') or
                             result.get('id') or result.get('Id') or
                             result.get('movieDbId') or result.get('seriesId'))
                # Compare as strings to handle type mismatches
                if str(result_id) == str(item_id):
                    item = result
                    logger.debug(f"Found item in results: {result.get('title', 'Unknown')}")
                    break

            if not item:
                logger.warning(f"Could not find item with ID {item_id} in stored results")

        # Shared request pipeline (season fetch, auto-approve checks, submit)
        success, auto_approved = media_service.submit_request(item_type, item_id, item)

        if success:
            await query.answer("✅ Request submitted successfully!", show_alert=True)
            # Try to update caption if it's a photo, otherwise update text
            try:
                if query.message.photo:
                    current_caption = query.message.caption or ""
                    await query.edit_message_caption(
                        caption=current_caption + "\n\n✅ <b>Request submitted!</b>",
                        parse_mode='HTML'
                    )
                else:
                    current_text = query.message.text or ""
                    await query.edit_message_text(
                        text=current_text + "\n\n✅ <b>Request submitted!</b>",
                        parse_mode='HTML'
                    )
            except Exception as e:
                logger.error(f"Error updating message: {e}")
            # Clear search results
            context.user_data.pop(f'{item_type}_results', None)
        else:
            await query.answer("❌ Failed to submit request. Please try again.", show_alert=True)
    except Exception as e:
        logger.error(f"Error requesting item: {e}", exc_info=True)
        await query.answer("❌ An error occurred. Please try again.", show_alert=True)


async def start_from_callback(query, context: ContextTypes.DEFAULT_TYPE):
    """Show start menu from callback."""
    # Clear search state so next message shows main menu instead of searching
    context.user_data.pop('request_type', None)
    context.user_data.pop('movie_results', None)
    context.user_data.pop('tv_results', None)
    context.user_data.pop('movie_from_list', None)
    context.user_data.pop('tv_from_list', None)
    context.user_data.pop('movie_list_page', None)
    context.user_data.pop('tv_list_page', None)

    reply_markup = main_menu_markup(query.message.chat if query.message else None)

    try:
        await query.edit_message_text(
            "🎬 <b>Welcome to Sparky Requests Bot!</b>\n\n"
            "Choose what you'd like to request:",
            reply_markup=reply_markup,
            parse_mode='HTML'
        )
    except:
        # If message can't be edited (e.g., it's a photo), send new message
        await query.message.reply_text(
            "🎬 <b>Welcome to Sparky Requests Bot!</b>\n\n"
            "Choose what you'd like to request:",
            reply_markup=reply_markup,
            parse_mode='HTML'
        )


async def handle_search_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle search query from user."""
    # Ignore messages from authorized group chats
    if is_message_from_authorized_group(update):
        return
    
    # Check authorization
    is_authorized, error_msg = await is_user_authorized(update, context)
    if not is_authorized:
        await update.message.reply_text(error_msg, parse_mode='HTML')
        return
    
    if 'request_type' not in context.user_data:
        # User hasn't selected movie/tv yet - show the start menu
        reply_markup = main_menu_markup(update.effective_chat)
        await update.message.reply_text(
            "🎬 Welcome to Sparky Requests Bot!\n\n"
            "Choose what you'd like to request:",
            reply_markup=reply_markup,
            parse_mode='HTML'
        )
        return
    
    if not ombi_client:
        await update.message.reply_text(
            "❌ Ombi client is not configured. Please check your environment variables."
        )
        return
    
    item_type = context.user_data['request_type']
    query_text = update.message.text.strip()
    
    # Extract title from IMDb URL if it's an IMDb link
    imdb_year = None  # Year extracted from IMDB page
    if 'imdb.com' in query_text.lower():
        await update.message.reply_text("🔗 Extracting title from IMDb link...")
        try:
            query_text, imdb_year = media_service.resolve_imdb_link(query_text)
        except ImdbLookupError as e:
            await update.message.reply_text(f"❌ {e}")
            return
        except Exception as e:
            logger.error(f"Error resolving IMDb link: {e}", exc_info=True)
            await update.message.reply_text(
                "❌ Could not extract title from IMDb link. Please try searching with the title directly."
            )
            return

    # Clean up the query and extract an optional year filter (movies only)
    query_text = media_service.clean_query_text(query_text)
    query_text, search_year = media_service.extract_search_year(query_text, item_type, imdb_year)

    await update.message.reply_text(f"🔍 Searching for {item_type}: {query_text}...")
    
    try:
        # Shared search pipeline (year fallback, TV detail enrichment, popularity sort)
        results = media_service.perform_search(item_type, query_text, search_year)

        if not results:
            logger.warning(f"No results found for {item_type} query: '{query_text}'")
            await update.message.reply_text(
                f"❌ No {item_type} found matching '{query_text}'.\n\n"
                "Please try a different search term or return to the main menu.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🏠 Main Menu", callback_data="cancel_main")
                ]])
            )
            return

        # Store results
        context.user_data[f'{item_type}_results'] = results
        context.user_data[f'{item_type}_from_list'] = False

        # Always show detailed view first (better for mobile readability)
        # List view is available via button when there are many results
        await show_first_result(update, context, item_type, results)
        
    except Exception as e:
        logger.error(f"Error searching {item_type}: {e}", exc_info=True)
        await update.message.reply_text(
            f"❌ An error occurred while searching. Please try again later."
        )


async def show_first_result(update: Update, context: ContextTypes.DEFAULT_TYPE, item_type: str, results: list):
    """Show the first search result."""
    if not results:
        return
    
    item = results[0]
    # Try multiple possible field names for ID (Ombi uses theTvDbId for TV shows)
    item_id = (item.get('theTvDbId') or item.get('theMovieDbId') or  # TV shows use theTvDbId
               item.get('tvDbId') or item.get('tvdbId') or 
               item.get('id') or item.get('Id') or 
               item.get('movieDbId') or item.get('movie_id') or 
               item.get('tmdbId') or item.get('tmdb_id') or
               item.get('seriesId'))  # Fallback to seriesId for TV
    
    if not item_id:
        await update.message.reply_text("❌ Invalid item. Please try again.")
        return

    # For movies, fetch detailed info to get IMDB ID (search results don't include it)
    if item_type == 'movie':
        media_service.enrich_movie_imdb(item)

    # Format description
    description = format_item_description(item, item_type)
    
    # Check if item should hide request button (available, approved, or requested)
    should_hide, status = get_item_status(item)
    logger.debug(f"Item status check: should_hide={should_hide}, status={status}, partlyAvailable={item.get('partlyAvailable')}")
    
    # Get poster URL
    poster_url = get_poster_url(item)
    
    # Create navigation buttons
    keyboard = []

    # Navigation buttons (only Next if multiple results)
    if len(results) > 1:
        nav_buttons = [InlineKeyboardButton("Next ▶️", callback_data=f"next_{item_type}_0")]
        # Add List View button when many results
        if len(results) > LIST_VIEW_BUTTON_THRESHOLD:
            nav_buttons.append(InlineKeyboardButton("📋 List View", callback_data=f"tolist_{item_type}"))
        keyboard.append(nav_buttons)

    # Action buttons - only show Request if not available/approved/requested
    action_buttons = []
    if not should_hide:
        action_buttons.append(InlineKeyboardButton("✅ Request", callback_data=f"request_{item_type}_{item_id}"))
    action_buttons.append(InlineKeyboardButton("❌ Cancel", callback_data="cancel_main"))
    keyboard.append(action_buttons)
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    # Send message with poster if available
    try:
        if poster_url:
            await update.message.reply_photo(
                photo=poster_url,
                caption=description + f"\n\n📊 Result 1 of {len(results)}",
                reply_markup=reply_markup,
                parse_mode='HTML'
            )
        else:
            await update.message.reply_text(
                description + f"\n\n📊 Result 1 of {len(results)}",
                reply_markup=reply_markup,
                parse_mode='HTML'
            )
    except Exception as e:
        logger.error(f"Error showing first result: {e}")
        # Fallback to text only
        await update.message.reply_text(
            description + f"\n\n📊 Result 1 of {len(results)}",
            reply_markup=reply_markup,
            parse_mode='HTML'
        )


def get_item_title_for_list(item: dict, item_type: str) -> str:
    """Get a formatted title with year and rating for list view."""
    if item_type == 'movie':
        title = (item.get('title') or item.get('Title') or
                 item.get('movieTitle') or item.get('name') or 'Unknown')
    else:
        title = (item.get('title') or item.get('Title') or
                 item.get('name') or item.get('Name') or
                 item.get('seriesName') or 'Unknown')

    year = get_item_year(item, item_type)
    rating = get_item_rating(item, item_type)

    year_str = f" ({year})" if year else ""
    rating_str = f" ⭐{rating:.1f}" if rating > 0 else ""

    return f"{title}{year_str}{rating_str}"


def get_status_emoji(item: dict) -> str:
    """Get status emoji for list view."""
    should_hide, status = get_item_status(item)
    if status == 'available':
        return "✅"
    elif status == 'partially_available':
        return "✅"
    elif status == 'approved':
        return "🗓️"  # Scheduled/approved but not yet available
    elif status == 'requested':
        return "⏳"
    elif status == 'denied':
        return "❌"
    else:
        return "📥"  # Can be requested


async def show_results_list(update: Update, context: ContextTypes.DEFAULT_TYPE, item_type: str, results: list, page: int = 0):
    """Show a list view of search results with quick actions."""
    items_per_page = 5
    total_pages = (len(results) + items_per_page - 1) // items_per_page
    start_idx = page * items_per_page
    end_idx = min(start_idx + items_per_page, len(results))

    # Store list view state
    context.user_data[f'{item_type}_from_list'] = True
    context.user_data[f'{item_type}_list_page'] = page

    # Build keyboard with title + status/request buttons
    keyboard = []

    for idx in range(start_idx, end_idx):
        item = results[idx]
        title_text = get_item_title_for_list(item, item_type)
        status_emoji = get_status_emoji(item)

        # Get item ID for callbacks
        item_id = (item.get('theTvDbId') or item.get('theMovieDbId') or
                   item.get('tvDbId') or item.get('tvdbId') or
                   item.get('id') or item.get('Id') or
                   item.get('movieDbId') or item.get('seriesId'))

        # Title button - shows full details
        title_btn = InlineKeyboardButton(title_text, callback_data=f"sel_{item_type}_{idx}")

        # Status/Action button
        should_hide, status = get_item_status(item)
        if should_hide:
            # Already available/requested - just show status
            action_btn = InlineKeyboardButton(status_emoji, callback_data=f"status_{item_type}_{idx}")
        else:
            # Can request - quick request button
            action_btn = InlineKeyboardButton(status_emoji, callback_data=f"qreq_{item_type}_{idx}")

        keyboard.append([title_btn, action_btn])

    # Pagination buttons
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("◀️ Prev", callback_data=f"listpage_{item_type}_{page-1}"))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton("Next ▶️", callback_data=f"listpage_{item_type}_{page+1}"))
    if nav_buttons:
        keyboard.append(nav_buttons)

    # Cancel button
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_main")])

    reply_markup = InlineKeyboardMarkup(keyboard)

    # Build message text
    type_emoji = "🎬" if item_type == 'movie' else "📺"
    text = f"{type_emoji} <b>Found {len(results)} results</b> (page {page + 1}/{total_pages})\n\n"
    text += "Tap a title for details, or 📥 for quick request.\n"
    text += "✅ = Available | 🗓️ = Scheduled | ⏳ = Pending | ❌ = Denied"

    await update.message.reply_text(
        text,
        reply_markup=reply_markup,
        parse_mode='HTML'
    )


async def show_results_list_from_callback(query, context: ContextTypes.DEFAULT_TYPE, item_type: str, page: int = 0):
    """Show a list view of search results from a callback (e.g., back button or pagination)."""
    results_key = f'{item_type}_results'
    if results_key not in context.user_data:
        await query.edit_message_text("❌ No search results found. Please start a new search.")
        return

    results = context.user_data[results_key]
    items_per_page = 5
    total_pages = (len(results) + items_per_page - 1) // items_per_page
    start_idx = page * items_per_page
    end_idx = min(start_idx + items_per_page, len(results))

    # Store list view state
    context.user_data[f'{item_type}_from_list'] = True
    context.user_data[f'{item_type}_list_page'] = page

    # Build keyboard with title + status/request buttons
    keyboard = []

    for idx in range(start_idx, end_idx):
        item = results[idx]
        title_text = get_item_title_for_list(item, item_type)
        status_emoji = get_status_emoji(item)

        # Get item ID for callbacks
        item_id = (item.get('theTvDbId') or item.get('theMovieDbId') or
                   item.get('tvDbId') or item.get('tvdbId') or
                   item.get('id') or item.get('Id') or
                   item.get('movieDbId') or item.get('seriesId'))

        # Title button - shows full details
        title_btn = InlineKeyboardButton(title_text, callback_data=f"sel_{item_type}_{idx}")

        # Status/Action button
        should_hide, status = get_item_status(item)
        if should_hide:
            # Already available/requested - just show status
            action_btn = InlineKeyboardButton(status_emoji, callback_data=f"status_{item_type}_{idx}")
        else:
            # Can request - quick request button
            action_btn = InlineKeyboardButton(status_emoji, callback_data=f"qreq_{item_type}_{idx}")

        keyboard.append([title_btn, action_btn])

    # Pagination buttons
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("◀️ Prev", callback_data=f"listpage_{item_type}_{page-1}"))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton("Next ▶️", callback_data=f"listpage_{item_type}_{page+1}"))
    if nav_buttons:
        keyboard.append(nav_buttons)

    # Cancel button
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_main")])

    reply_markup = InlineKeyboardMarkup(keyboard)

    # Build message text
    type_emoji = "🎬" if item_type == 'movie' else "📺"
    text = f"{type_emoji} <b>Found {len(results)} results</b> (page {page + 1}/{total_pages})\n\n"
    text += "Tap a title for details, or 📥 for quick request.\n"
    text += "✅ = Available | 🗓️ = Scheduled | ⏳ = Pending | ❌ = Denied"

    try:
        # Try to edit the current message
        if query.message.photo:
            # If it's a photo message, delete and send new text
            await query.message.delete()
            await query.message.reply_text(
                text,
                reply_markup=reply_markup,
                parse_mode='HTML'
            )
        else:
            await query.edit_message_text(
                text,
                reply_markup=reply_markup,
                parse_mode='HTML'
            )
    except Exception as e:
        logger.error(f"Error showing results list: {e}")
        await query.message.reply_text(
            text,
            reply_markup=reply_markup,
            parse_mode='HTML'
        )


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
