# AWS Lambda Webhook Handler

This Lambda function handles Ombi webhook notifications and sends approval/denial messages to a Telegram group thread.

## Deployment

### Option 1: Using AWS SAM

1. Install AWS SAM CLI: https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html

2. Deploy:
```bash
sam build
sam deploy --guided
```

### Option 2: Manual ZIP Deployment

**⚠️ Important:** If you're on macOS/Windows, use the Docker method below to ensure Linux compatibility.

1. **Create deployment package (macOS/Windows - use Docker):**
```bash
cd lambda

# Method 1: Using Docker (recommended for macOS/Windows)
docker run --rm -v $(pwd):/var/task public.ecr.aws/lambda/python:3.11 \
  bash -c "pip install -r requirements.txt -t . && zip -r lambda-webhook.zip . -x '*.pyc' '__pycache__/*' '*.dist-info/*'"

# Method 2: Using deploy script (may have compatibility issues on macOS)
./deploy.sh

# Method 3: Manual (Linux only)
pip install -r requirements.txt -t .
zip -r lambda-webhook.zip . -x "*.pyc" "__pycache__/*" "*.dist-info/*"
```

**Important:** 
- Make sure `webhook_handler.py` is at the root of the ZIP file, not in a subdirectory
- If you get `ImportModuleError`, the dependencies weren't built for Linux - use Docker method

2. **Create Lambda function:**
   - Go to AWS Lambda Console
   - Create function → Author from scratch
   - Runtime: Python 3.11
   - **Architecture: arm64** (Graviton2 - cheaper and better performance)
   - **Handler:** `webhook_handler.lambda_handler` ⚠️ **IMPORTANT: Set this correctly!**
   - Upload `lambda-webhook.zip`

3. **Configure environment variables:**
   - `TELEGRAM_BOT_TOKEN` - Your Telegram bot token
   - `TELEGRAM_GROUP_CHAT_ID` - Group chat ID (negative number)
   - `TELEGRAM_GROUP_THREAD_ID` - Optional thread ID

4. **Set up API Gateway:**
   - Create API Gateway REST API
   - Create POST method pointing to Lambda function
   - Deploy API
   - Copy the API Gateway endpoint URL

5. **Configure Ombi webhook:**
   - URL: `https://your-api-gateway-url.amazonaws.com/prod/notifications/ombi`
   - Enable "Request Approved" and "Request Denied" notifications

## Testing

Test locally (simulates Lambda event):
```python
import json
from webhook_handler import lambda_handler

event = {
    'body': json.dumps({
        'notificationType': 'RequestApproved',
        'title': 'Test Movie',
        'type': 'Movie',
        'year': '2024',
        'requestStatus': 'Approved'
    })
}

result = lambda_handler(event, {})
print(result)
```

Test via API Gateway:
```bash
curl -X POST https://your-api-gateway-url.amazonaws.com/prod/notifications/ombi \
  -H "Content-Type: application/json" \
  -d '{
    "notificationType": "RequestApproved",
    "title": "Test Movie",
    "type": "Movie",
    "year": "2024"
  }'
```

## Environment Variables

Required:
- `TELEGRAM_BOT_TOKEN` - Telegram bot token
- `TELEGRAM_GROUP_CHAT_ID` - Group chat ID

Optional:
- `TELEGRAM_GROUP_THREAD_ID` - Thread ID for topic notifications

## Cost Estimate

Lambda free tier: 1M requests/month free
After free tier: ~$0.20 per 1M requests

For webhook notifications (maybe 10-100 per day), this is essentially free.

