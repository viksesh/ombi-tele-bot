"""AWS Lambda handler for Ombi webhook notifications."""
import os
import json
import logging
import asyncio
import time
from typing import Tuple, Optional
from telegram import Bot
from telegram.error import TelegramError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Get environment variables from Lambda
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
GROUP_CHAT_ID = os.getenv('TELEGRAM_GROUP_CHAT_ID')
GROUP_THREAD_ID = os.getenv('TELEGRAM_GROUP_THREAD_ID')
DEDUP_TABLE_NAME = os.getenv('DEDUP_TABLE_NAME')  # Optional DynamoDB table for deduplication

# Try to import boto3 for DynamoDB (optional)
try:
    import boto3
    dynamodb = boto3.resource('dynamodb') if DEDUP_TABLE_NAME else None
except ImportError:
    dynamodb = None
    if DEDUP_TABLE_NAME:
        logger.warning("boto3 not available, deduplication disabled")


def run_async(coro):
    """Run an async coroutine synchronously for Lambda."""
    # Lambda's event loop handling - use asyncio.run() for clean execution
    # This creates a new event loop, runs the coroutine, and properly cleans up
    try:
        # Try to get existing loop first (for compatibility)
        loop = asyncio.get_running_loop()
        # If we're already in an async context, we can't use asyncio.run()
        # This shouldn't happen in Lambda, but handle it just in case
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(asyncio.run, coro)
            return future.result(timeout=25)  # 25 second timeout (Lambda has 30s total)
    except RuntimeError:
        # No running loop, safe to use asyncio.run()
        return asyncio.run(coro)


def get_poster_url(data: dict) -> Optional[str]:
    """Get poster URL from webhook data.
    
    Args:
        data: Webhook data dictionary from Ombi
        
    Returns:
        Poster URL string or None if not found
    """
    # Ombi webhooks use 'posterImage' which is already a full URL
    return data.get('posterImage') or data.get('poster_image')


def normalize_notification_type(notification_type: str) -> str:
    """Normalize notification types for deduplication.
    
    RequestDeclined and RequestDenied are treated as the same (both mean denied).
    Note: RequestAvailable is not handled, so it's not normalized here.
    
    Args:
        notification_type: The notification type from Ombi
        
    Returns:
        Normalized notification type for deduplication
    """
    # Normalize denial types
    if notification_type in ['RequestDeclined', 'RequestDenied']:
        return 'denied'
    # Return as-is for other types (RequestApproved stays as RequestApproved)
    return notification_type


def is_duplicate_notification(request_id: str, notification_type: str) -> bool:
    """Check if we've already processed this notification.
    
    Uses DynamoDB to track processed notifications with a 24-hour TTL.
    Normalizes notification types so RequestApproved/RequestAvailable are treated as the same.
    
    Args:
        request_id: The Ombi request ID
        notification_type: The notification type (e.g., 'RequestApproved')
        
    Returns:
        True if this notification was already processed, False otherwise
    """
    if not DEDUP_TABLE_NAME or not dynamodb:
        # Deduplication disabled - return False (not a duplicate)
        return False
    
    try:
        table = dynamodb.Table(DEDUP_TABLE_NAME)
        # Normalize notification type for deduplication
        normalized_type = normalize_notification_type(notification_type)
        # Create unique key from requestId + normalized notificationType
        notification_key = f"{request_id}:{normalized_type}"
        
        # Check if notification exists
        response = table.get_item(
            Key={'notification_key': notification_key}
        )
        
        if 'Item' in response:
            logger.info(f"Duplicate notification detected: {notification_key} (original type: {notification_type})")
            return True
        
        # Mark as processed with 24-hour TTL (86400 seconds)
        ttl = int(time.time()) + 86400
        table.put_item(
            Item={
                'notification_key': notification_key,
                'ttl': ttl,
                'processed_at': int(time.time()),
                'original_type': notification_type  # Store original for debugging
            }
        )
        logger.debug(f"Marked notification as processed: {notification_key} (original type: {notification_type})")
        return False
        
    except Exception as e:
        # If DynamoDB fails, log warning but don't block processing
        logger.warning(f"Deduplication check failed: {e}, processing notification anyway")
        return False


async def _send_message(bot: Bot, message: str, photo_url: Optional[str] = None, message_thread_id: Optional[int] = None):
    """Helper function to send a message or photo."""
    if photo_url:
        await bot.send_photo(
            chat_id=GROUP_CHAT_ID,
            photo=photo_url,
            caption=message,
            parse_mode='HTML',
            message_thread_id=message_thread_id
        )
    else:
        await bot.send_message(
            chat_id=GROUP_CHAT_ID,
            text=message,
            parse_mode='HTML',
            message_thread_id=message_thread_id
        )


def send_group_notification(message: str, thread_id: int = None, photo_url: Optional[str] = None):
    """Send notification to group chat (optionally in a thread).
    
    Args:
        message: Text message to send
        thread_id: Optional thread ID for forum topics
        photo_url: Optional photo URL to send with the message
    """
    if not GROUP_CHAT_ID or not TELEGRAM_BOT_TOKEN:
        logger.warning("GROUP_CHAT_ID or TELEGRAM_BOT_TOKEN not configured")
        return False
    
    try:
        # Use provided thread_id, or fall back to GROUP_THREAD_ID env var
        message_thread_id = None
        if thread_id:
            message_thread_id = int(thread_id)
        elif GROUP_THREAD_ID:
            try:
                message_thread_id = int(GROUP_THREAD_ID)
            except (ValueError, TypeError):
                logger.warning(f"Invalid GROUP_THREAD_ID: {GROUP_THREAD_ID}")
        
        logger.info(f"Sending notification to chat_id={GROUP_CHAT_ID}, thread_id={message_thread_id}, has_photo={bool(photo_url)}")
        
        async def send_message_async():
            async with Bot(token=TELEGRAM_BOT_TOKEN) as bot:
                await _send_message(bot, message, photo_url, message_thread_id)
        
        try:
            run_async(send_message_async())
            logger.info(f"Sent notification to {GROUP_CHAT_ID}" + (f" (thread {message_thread_id})" if message_thread_id else ""))
            return True
        except TelegramError as e:
            error_msg = str(e)
            
            # If topic is closed, try reopening it
            topic_closed_indicators = [
                "topic_closed", "topic closed", "topic is closed",
                "forum topic is closed", "bad request: topic is closed"
            ]
            
            is_topic_closed = message_thread_id and any(
                indicator in error_msg.lower() for indicator in topic_closed_indicators
            )
            
            if is_topic_closed:
                logger.warning(f"Topic {message_thread_id} is closed, attempting to reopen")
                try:
                    async def reopen_and_send():
                        async with Bot(token=TELEGRAM_BOT_TOKEN) as bot:
                            await bot.reopen_forum_topic(
                                chat_id=GROUP_CHAT_ID,
                                message_thread_id=message_thread_id
                            )
                            logger.info(f"Reopened topic {message_thread_id}")
                            await _send_message(bot, message, photo_url, message_thread_id)
                    
                    run_async(reopen_and_send())
                    logger.info(f"Sent notification after reopening topic")
                    return True
                except TelegramError as reopen_error:
                    logger.warning(f"Failed to reopen topic: {reopen_error}, sending without thread")
                    async def send_without_thread():
                        async with Bot(token=TELEGRAM_BOT_TOKEN) as bot:
                            await _send_message(bot, message, photo_url, None)
                    
                    run_async(send_without_thread())
                    logger.info(f"Sent notification without thread")
                    return True
            else:
                raise
    
    except TelegramError as e:
        logger.error(f"Failed to send notification: {e}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        return False


def format_approval_notification(data: dict) -> Tuple[str, Optional[str]]:
    """Format an approval notification message.
    
    Returns:
        tuple: (message, poster_url) - formatted message and poster URL (if available)
    """
    title = data.get('title', 'Unknown')
    request_type = data.get('type', 'Unknown')
    year = data.get('year', '')
    request_status = data.get('requestStatus', 'Approved')
    
    message = f"✅ <b>{request_type} Request Approved</b>\n\n"
    message += f"<b>Title:</b> {title}"
    if year:
        message += f" ({year})"
    message += f"\n<b>Status:</b> {request_status}\n"
    message += f"\nIt will be uploaded within 24 hours if already available digitally, or automatically when it becomes available."
    
    poster_url = get_poster_url(data)
    
    return message, poster_url


def format_denial_notification(data: dict) -> Tuple[str, Optional[str]]:
    """Format a denial notification message.
    
    Returns:
        tuple: (message, poster_url) - formatted message and poster URL (if available)
    """
    title = data.get('title', 'Unknown')
    request_type = data.get('type', 'Unknown')
    year = data.get('year', '')
    deny_reason = data.get('denyReason', 'No reason provided')
    
    message = f"❌ <b>{request_type} Request Denied</b>\n\n"
    message += f"<b>Title:</b> {title}"
    if year:
        message += f" ({year})"
    message += f"\n\nIt is not available."
    if deny_reason and deny_reason.lower() != 'no reason provided':
        message += f"\n<b>Reason:</b> {deny_reason}"
    
    poster_url = get_poster_url(data)
    
    return message, poster_url


def lambda_handler(event, context):
    """AWS Lambda handler for Ombi webhook notifications."""
    logger.info(f"Received event: {json.dumps(event)}")
    
    # Extract body from API Gateway event
    if 'body' in event:
        try:
            body = json.loads(event['body']) if isinstance(event['body'], str) else event['body']
        except json.JSONDecodeError:
            logger.error(f"Invalid JSON in body: {event.get('body')}")
            return {
                'statusCode': 400,
                'body': json.dumps({'error': 'Invalid JSON'})
            }
    else:
        # Direct invocation (for testing)
        body = event
        if not isinstance(body, dict):
            return {
                'statusCode': 400,
                'body': json.dumps({'error': 'Invalid event format'})
            }
    
    # Validate required fields
    notification_type = body.get('notificationType', '')
    request_id = body.get('requestId', '')
    logger.info(f"Processing notification: {notification_type}, requestId: {request_id}")
    
    # Map notification types to formatter functions (only RequestApproved and denials)
    # RequestAvailable is ignored - we only notify on approval, not when content becomes available
    notification_handlers = {
        'RequestApproved': format_approval_notification,
        'RequestDeclined': format_denial_notification,
        'RequestDenied': format_denial_notification,
    }
    
    handler = notification_handlers.get(notification_type)
    if handler:
        # Only deduplicate notifications we actually process
        if request_id and is_duplicate_notification(str(request_id), notification_type):
            logger.info(f"Skipping duplicate notification: {notification_type} for requestId {request_id}")
            return {
                'statusCode': 200,
                'body': json.dumps({
                    'status': 'duplicate',
                    'type': notification_type,
                    'requestId': request_id
                })
            }
        
        message, poster_url = handler(body)
        success = send_group_notification(message, photo_url=poster_url)
        return {
            'statusCode': 200 if success else 500,
            'body': json.dumps({
                'status': 'success' if success else 'error',
                'type': notification_type,
                **({'message': 'Failed to send notification'} if not success else {})
            })
        }
    
    # Ignore other notification types (like NewRequest) - don't deduplicate these
    logger.info(f"Ignoring notification type: {notification_type}")
    return {
        'statusCode': 200,
        'body': json.dumps({'status': 'ignored', 'type': notification_type})
    }

