import os
import json
import logging
import re
import requests
from bs4 import BeautifulSoup
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from telegram.error import TelegramError
from ombi_client import OmbiClient

# Load environment variables from .env file if available
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv not installed, use system env vars

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

# Initialize Ombi client
try:
    ombi_client = OmbiClient()
except ValueError as e:
    logger.error(f"Failed to initialize Ombi client: {e}")
    ombi_client = None

# Get environment variables
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')

if not TELEGRAM_BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN environment variable is required")

# Group authorization feature flag
ENABLE_GROUP_AUTH = os.getenv('ENABLE_GROUP_AUTH', '').lower() in ('true', '1', 'yes')
AUTHORIZED_GROUP_CHAT_IDS_STR = os.getenv('AUTHORIZED_GROUP_CHAT_ID', '').strip() or os.getenv('AUTHORIZED_GROUP_CHAT_IDS', '').strip()
# Parse comma-separated list of group chat IDs, supporting both singular and plural env var names
AUTHORIZED_GROUP_CHAT_IDS = [gid.strip() for gid in AUTHORIZED_GROUP_CHAT_IDS_STR.split(',') if gid.strip()] if AUTHORIZED_GROUP_CHAT_IDS_STR else []


async def is_user_authorized(update: Update, context: ContextTypes.DEFAULT_TYPE) -> tuple[bool, str]:
    """Check if user is authorized to use the bot.
    
    Checks if user is a member of any of the authorized group chats.
    
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


def get_item_status(item: dict):
    """Check item status and return (should_hide_request, status_text).
    
    Returns:
        tuple: (bool, str) - (True if request should be hidden, status message)
        Status can be: 'available', 'partially_available', 'approved', 'requested', 'denied', or None
    """
    # Check if denied first (highest priority - cannot request if denied)
    denied = item.get('denied', False) or item.get('Denied', False) or item.get('isDenied', False)
    if denied:
        logger.debug(f"Item is denied (denied={denied}, deniedReason={item.get('deniedReason')})")
        return (True, 'denied')
    
    # Check if truly available (in library/content provider)
    if item.get('available', False) or item.get('alreadyincp', False) or item.get('fullyAvailable', False):
        return (True, 'available')
    
    # Check if partially available (some episodes/seasons available)
    # First check the partlyAvailable field
    partly_avail = None
    if 'partlyAvailable' in item:
        partly_avail = item['partlyAvailable']
        logger.debug(f"Found partlyAvailable key with value: {partly_avail} (type: {type(partly_avail)})")
    elif 'partiallyAvailable' in item:
        partly_avail = item['partiallyAvailable']
        logger.debug(f"Found partiallyAvailable key with value: {partly_avail} (type: {type(partly_avail)})")
    elif 'partiallyavailable' in item:
        partly_avail = item['partiallyavailable']
        logger.debug(f"Found partiallyavailable key with value: {partly_avail} (type: {type(partly_avail)})")
    
    # Also check seasonRequests for TV shows - if some seasons are available, it's partially available
    if 'seasonRequests' in item:
        season_requests = item.get('seasonRequests', [])
        if isinstance(season_requests, list) and len(season_requests) > 0:
            # Check if any seasons are available
            available_seasons = [s for s in season_requests if s.get('available', False) or s.get('fullyAvailable', False)]
            total_seasons = len(season_requests)
            if available_seasons and len(available_seasons) < total_seasons:
                # Some seasons available but not all = partially available
                logger.debug(f"Found {len(available_seasons)}/{total_seasons} seasons available - marking as partially available")
                return (True, 'partially_available')
    
    # Check if value is truthy (not None, not False, not empty string)
    if partly_avail is not None:
        # If it's explicitly False, don't treat as partially available
        if partly_avail is False:
            logger.debug("partlyAvailable is False - not partially available")
        # If it's an empty string or falsy string, don't treat as partially available
        elif isinstance(partly_avail, str) and partly_avail.strip().lower() in ('false', '0', 'no', ''):
            logger.debug(f"partlyAvailable is falsy string '{partly_avail}' - not partially available")
        # Any other truthy value means partially available (True, 1, "true", non-empty string, etc.)
        else:
            logger.debug(f"Item is partially available (partlyAvailable={partly_avail}, truthy={bool(partly_avail)})")
            return (True, 'partially_available')
    
    # Check if approved (scheduled to upload when available digitally)
    # Check multiple possible field names and values
    approved = item.get('approved', False) or item.get('Approved', False) or item.get('isApproved', False)
    request_id = item.get('requestId')
    
    # If there's a requestId, it might be approved
    # Also check if approved is explicitly True
    if request_id:
        logger.debug(f"Item has requestId={request_id}, approved={approved}")
        # If approved is True, definitely approved
        if approved:
            logger.debug("Item is approved (approved=True)")
            return (True, 'approved')
        # If there's a requestId but approved is False, it might still be pending approval
        # But we'll treat it as requested, not approved
    
    if approved:
        logger.debug(f"Item is approved (approved={approved})")
        return (True, 'approved')
    
    # Check if requested (pending approval)
    # requestId: 0 or null means no request exists, any positive number means a request exists
    has_request = request_id and request_id != 0 and request_id != '0'
    if item.get('requested', False) or has_request:
        logger.debug(f"Item is requested (requested={item.get('requested')}, requestId={request_id}, has_request={has_request})")
        return (True, 'requested')
    
    # Not available, approved, or requested - can be requested
    return (False, None)


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
        
        # Add status indicator based on state
        if status == 'denied':
            text += " ❌ <b>Not Available</b>"
        elif status == 'available':
            text += " ✅ <b>Already Available</b>"
        elif status == 'partially_available':
            text += " ✅ <b>Partially Available</b>"
        elif status == 'approved':
            text += " ✅ <b>Approved - Scheduled to upload when available digitally</b>"
        elif status == 'requested':
            text += " ⏳ <b>Requested - Pending approval</b>"
        
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
        
        # Add status indicator based on state
        if status == 'denied':
            text += " ❌ <b>Not Available</b>"
        elif status == 'available':
            text += " ✅ <b>Already Available</b>"
        elif status == 'partially_available':
            text += " ✅ <b>Partially Available</b>"
        elif status == 'approved':
            text += " ✅ <b>Approved - Scheduled to upload when available digitally</b>"
        elif status == 'requested':
            text += " ⏳ <b>Requested - Pending approval</b>"
        
        if rating and rating > 0:
            text += f"\n\n⭐ Rating: {rating:.1f}/10\n\n"
        else:
            text += "\n\n"
        text += f"{overview[:300]}"
        if len(overview) > 300:
            text += "..."
        return text


def get_poster_url(item: dict, base_url: str = "https://image.tmdb.org/t/p/w500") -> str:
    """Get poster URL for item."""
    # Try multiple possible field names for poster path
    # Ombi TV shows use 'banner' for poster images
    poster_path = (item.get('banner') or item.get('Banner') or  # TV shows often use banner
                   item.get('posterPath') or item.get('poster_path') or 
                   item.get('backdropPath') or item.get('backdrop_path') or
                   item.get('poster') or item.get('Poster') or 
                   item.get('posterUrl') or item.get('poster_url') or
                   item.get('image') or item.get('Image'))
    
    if poster_path:
        # If it's already a full URL, return it
        if poster_path.startswith('http://') or poster_path.startswith('https://'):
            return poster_path
        # If it starts with /, prepend base URL
        if poster_path.startswith('/'):
            return f"{base_url}{poster_path}"
        # Otherwise, assume it's a relative path
        return f"{base_url}/{poster_path.lstrip('/')}"
    return None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command - show main menu."""
    # Check authorization
    is_authorized, error_msg = await is_user_authorized(update, context)
    if not is_authorized:
        await update.message.reply_text(error_msg, parse_mode='HTML')
        return
    
    keyboard = [
        [InlineKeyboardButton("🎬 Request Movie", callback_data="req_movie")],
        [InlineKeyboardButton("📺 Request TV Show", callback_data="req_tv")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "🎬 <b>Welcome to Sparky Requests Bot!</b>\n\n"
        "Choose what you'd like to request:",
        reply_markup=reply_markup,
        parse_mode='HTML'
    )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle callback queries."""
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
    
    # Format description
    description = format_item_description(item, item_type)
    
    # Check if item should hide request button (available, approved, or requested)
    should_hide, status = get_item_status(item)
    logger.debug(f"Item status check: should_hide={should_hide}, status={status}, partlyAvailable={item.get('partlyAvailable')}")
    
    # Get poster URL
    poster_url = get_poster_url(item)
    
    # Create navigation buttons
    keyboard = []
    
    # Navigation buttons
    nav_buttons = []
    if index > 0:
        nav_buttons.append(InlineKeyboardButton("◀️ Previous", callback_data=f"prev_{item_type}_{index}"))
    if index < len(results) - 1:
        nav_buttons.append(InlineKeyboardButton("Next ▶️", callback_data=f"next_{item_type}_{index}"))
    if nav_buttons:
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
        # Request the item
        success = False
        if item_type == 'movie':
            success = ombi_client.request_movie(item_id)
        else:
            success = ombi_client.request_tv(item_id)
        
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
    keyboard = [
        [InlineKeyboardButton("🎬 Request Movie", callback_data="req_movie")],
        [InlineKeyboardButton("📺 Request TV Show", callback_data="req_tv")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
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
    # Check authorization
    is_authorized, error_msg = await is_user_authorized(update, context)
    if not is_authorized:
        await update.message.reply_text(error_msg, parse_mode='HTML')
        return
    
    if 'request_type' not in context.user_data:
        # User hasn't selected movie/tv yet
        return
    
    if not ombi_client:
        await update.message.reply_text(
            "❌ Ombi client is not configured. Please check your environment variables."
        )
        return
    
    item_type = context.user_data['request_type']
    query_text = update.message.text.strip()
    
    # Extract title from IMDb URL if it's an IMDb link
    if 'imdb.com' in query_text.lower():
        await update.message.reply_text("🔗 Extracting title from IMDb link...")
        try:
            # Extract IMDb ID from URL
            imdb_match = re.search(r'tt\d+', query_text)
            if not imdb_match:
                await update.message.reply_text(
                    "❌ Could not extract IMDb ID from URL. Please try again."
                )
                return
            
            imdb_id = imdb_match.group(0)
            imdb_url = f"https://www.imdb.com/title/{imdb_id}/"
            
            # Fetch IMDb page
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
            }
            response = requests.get(imdb_url, headers=headers, timeout=10)
            response.raise_for_status()
            
            # Parse the page
            soup = BeautifulSoup(response.content, 'html.parser')
            
            # Try to find the title in various possible locations
            title = None
            raw_title = None
            
            # Method 1: Look for h1 with data-testid="hero-title-block__title"
            # This is the most reliable source for the actual title
            title_elem = soup.find('h1', {'data-testid': 'hero-title-block__title'})
            if title_elem:
                # Get only the direct text, not from child elements
                # IMDb often has the title as direct text, with year in a span
                raw_title = title_elem.get_text(separator=' ', strip=True)
                # If there are child elements, try to get just the first text node
                if not raw_title or len(raw_title) > 100:  # If too long, might have extra data
                    # Try to find span with year and exclude it
                    year_span = title_elem.find('span', class_=re.compile(r'heroTitle|Year'))
                    if year_span:
                        year_span.decompose()  # Remove year span
                    raw_title = title_elem.get_text(separator=' ', strip=True)
            
            # Method 2: Look for title tag and extract from it
            if not raw_title or len(raw_title) > 100:
                title_tag = soup.find('title')
                if title_tag:
                    raw_title = title_tag.get_text(strip=True)
            
            # Method 3: Look for og:title meta tag
            if not raw_title or len(raw_title) > 100:
                og_title = soup.find('meta', property='og:title')
                if og_title and og_title.get('content'):
                    raw_title = og_title['content'].strip()
            
            # Clean up the title - remove year, TV Series info, and other metadata
            if raw_title:
                # Remove " - IMDb" suffix if present
                raw_title = re.sub(r'\s*-\s*IMDb\s*$', '', raw_title, flags=re.IGNORECASE)
                
                # Remove year patterns like "(2025)", "(2025-)", "(TV Series 2025-)", etc.
                # Handle both at end and in middle
                raw_title = re.sub(r'\s*\([^)]*\d{4}[^)]*\)\s*', ' ', raw_title)
                raw_title = re.sub(r'\s*\(TV Series[^)]*\)\s*', ' ', raw_title, flags=re.IGNORECASE)
                raw_title = re.sub(r'\s*\([^)]*\)\s*$', '', raw_title)  # Remove trailing parentheses
                
                # Remove any trailing metadata patterns (rating, genres, etc.)
                # Remove patterns like "⭐ 8.4 | Drama, Sci-Fi" or "8.4 | Drama, Sci-Fi"
                raw_title = re.sub(r'\s*[⭐★]\s*\d+\.\d+.*$', '', raw_title)
                raw_title = re.sub(r'\s*\d+\.\d+\s*\|.*$', '', raw_title)  # Rating | genres
                raw_title = re.sub(r'\s*\|\s*.*$', '', raw_title)  # Remove anything after |
                
                # Remove common suffixes
                raw_title = re.sub(r'\s*\(TV Series\)\s*$', '', raw_title, flags=re.IGNORECASE)
                raw_title = re.sub(r'\s*TV Series\s*$', '', raw_title, flags=re.IGNORECASE)
                
                # Clean up any remaining extra whitespace and normalize
                title = ' '.join(raw_title.split()).strip()
                
                # Limit title length to reasonable size (shouldn't be more than ~50 chars typically)
                if len(title) > 100:
                    # If still too long, try to extract just the first part before any special chars
                    title_match = re.match(r'^([^⭐★|(]+)', title)
                    if title_match:
                        title = title_match.group(1).strip()
            
            if title:
                query_text = title
                logger.debug(f"Extracted title '{title}' from IMDb URL {imdb_url} (raw: '{raw_title}')")
            else:
                # Fallback: use IMDb ID (might not work, but better than nothing)
                logger.warning(f"Could not extract title from IMDb page, using ID: {imdb_id}")
                query_text = imdb_id
                
        except requests.RequestException as e:
            logger.error(f"Error fetching IMDb page: {e}")
            await update.message.reply_text(
                "❌ Could not fetch IMDb page. Please try searching with the title directly."
            )
            return
        except Exception as e:
            logger.error(f"Error parsing IMDb page: {e}", exc_info=True)
            await update.message.reply_text(
                "❌ Could not extract title from IMDb link. Please try searching with the title directly."
            )
            return
    
    # Clean up query text - remove any extra whitespace and ensure it's just the title
    query_text = query_text.strip()
    # Remove any trailing metadata that might have been included
    query_text = re.sub(r'\s*[⭐★]\s*\d+\.\d+.*$', '', query_text)  # Remove rating and after
    query_text = re.sub(r'\s*\|\s*.*$', '', query_text)  # Remove anything after |
    query_text = query_text.strip()
    
    # Remove year from query if present (Ombi doesn't handle years well in search)
    # But don't remove if the entire query is just a year (e.g., movie called "1942")
    year_match = re.search(r'\b(19|20)\d{2}\b', query_text)
    if year_match:
        year = year_match.group(0)
        # Check if the entire query is just the year (possibly with whitespace)
        query_without_whitespace = query_text.strip()
        if query_without_whitespace == year or query_without_whitespace == f"({year})":
            # Entire query is just a year - don't remove it (could be a movie title like "1942")
            logger.debug(f"Query is just a year '{year}', keeping it as-is")
        else:
            # Query has text + year - remove the year (handles formats like "Title 1999", "Title (1999)", "Title, 1999")
            query_text = re.sub(r'\s*[,\s]*\(?\s*\b(19|20)\d{2}\b\s*\)?\s*', ' ', query_text).strip()
            query_text = ' '.join(query_text.split())  # Normalize whitespace
            logger.debug(f"Removed year '{year}' from query, searching with: '{query_text}'")
    
    await update.message.reply_text(f"🔍 Searching for {item_type}: {query_text}...")
    
    try:
        # Search Ombi (without year)
        if item_type == 'movie':
            results = ombi_client.search_movie(query_text)
        else:
            results = ombi_client.search_tv(query_text)
        
        if not results:
            await update.message.reply_text(
                f"❌ No {item_type} found matching '{query_text}'.\n\n"
                "Please try a different search term or return to the main menu.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🏠 Main Menu", callback_data="cancel_main")
                ]])
            )
            return
        
        # For TV shows, try to get detailed info which may have more accurate availability status
        if item_type == 'tv' and results:
            # Try to get detailed info for the first result to get accurate availability
            first_item = results[0]
            # Try multiple ID fields - Ombi might use TMDB ID for the info endpoint
            tv_id = (first_item.get('theTvDbId') or first_item.get('tvDbId') or 
                    first_item.get('tvdbId') or first_item.get('theMovieDbId') or  # Try TMDB ID as fallback
                    first_item.get('seriesId'))
            
            if tv_id:
                logger.debug(f"Fetching detailed TV info for ID: {tv_id} (theTvDbId={first_item.get('theTvDbId')}, theMovieDbId={first_item.get('theMovieDbId')}, seriesId={first_item.get('seriesId')})")
                detailed_info = ombi_client.get_tv_info(tv_id)
                if detailed_info:
                    # Check seasonRequests in detailed info - might have availability per season
                    if 'seasonRequests' in detailed_info:
                        season_requests = detailed_info.get('seasonRequests', [])
                        # Check if any seasons are available (partial availability)
                        if isinstance(season_requests, list) and len(season_requests) > 0:
                            available_seasons = [s for s in season_requests if s.get('available', False) or s.get('fullyAvailable', False)]
                            total_seasons = len(season_requests)
                            if available_seasons and len(available_seasons) < total_seasons:
                                logger.debug(f"Found {len(available_seasons)}/{total_seasons} seasons available - marking as partially available")
                                # Force partlyAvailable to True if we find some (but not all) seasons available
                                detailed_info['partlyAvailable'] = True
                    # Also check if detailed info has different partlyAvailable value
                    if detailed_info.get('partlyAvailable') is True:
                        logger.debug("Detailed info shows partlyAvailable=True")
                    # Merge detailed info into first result to get accurate availability status
                    first_item.update(detailed_info)
                    logger.debug(f"After merge - partlyAvailable: {first_item.get('partlyAvailable')}, fullyAvailable: {first_item.get('fullyAvailable')}, available: {first_item.get('available')}, approved: {first_item.get('approved')}, requestId: {first_item.get('requestId')}")
                else:
                    logger.warning(f"Failed to fetch detailed info for TV ID {tv_id} - trying alternative endpoint or ID")
                    # Try with TMDB ID if we used TVDb ID
                    if tv_id != first_item.get('theMovieDbId') and first_item.get('theMovieDbId'):
                        logger.debug(f"Retrying with TMDB ID: {first_item.get('theMovieDbId')}")
                        detailed_info = ombi_client.get_tv_info(first_item.get('theMovieDbId'))
                        if detailed_info:
                            logger.debug(f"Successfully fetched detailed info with TMDB ID")
                            first_item.update(detailed_info)
        
        # Store results
        context.user_data[f'{item_type}_results'] = results
        
        # Show first result
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
        keyboard.append([InlineKeyboardButton("Next ▶️", callback_data=f"next_{item_type}_0")])
    
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


def main():
    """Start the bot."""
    
    # Create application
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
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
