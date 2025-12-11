# Local Testing Guide

## Quick Start for Local Testing on Mac

### Prerequisites

1. **Python 3.11+** installed
2. Access to your Ombi instance (local or remote)

### Step 1: Install Dependencies

```bash
pip install -r requirements.txt
```

Or use a virtual environment (recommended):

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Step 2: Set Environment Variables

Create a `.env` file or export them in your terminal:

```bash
export TELEGRAM_BOT_TOKEN="your_telegram_bot_token"
export OMBI_URL="http://your-ombi-instance:3579"  # or https://ombi.example.com
export OMBI_API_KEY="your_ombi_api_key"
export OMBI_REQUEST_USER="requests"  # Optional: Ombi username for requests
```

Or create a `.env` file (make sure to add it to `.gitignore`):

```bash
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
OMBI_URL=http://your-ombi-instance:3579
OMBI_API_KEY=your_ombi_api_key
OMBI_REQUEST_USER=requests  # Optional
```

### Step 3: Run the Bot

**Option A: Using the helper script (Recommended)**

```bash
chmod +x run_local.sh
./run_local.sh
```

This script will:
- Check for required environment variables
- Load variables from `.env` file
- Start the bot

**Option B: Run directly**

```bash
python bot.py
```

**Note**: If you use Option B, make sure to set your environment variables first:
```bash
export $(cat .env | grep -v '^#' | xargs)
```

### Step 4: Test the Bot

1. Open Telegram and find your bot
2. Send `/start` to see the main menu
3. Choose "🎬 Request Movie" or "📺 Request TV Show"
4. Enter a movie/TV show title or paste an IMDb link
5. Browse results using Previous/Next buttons
6. Click "✅ Request" to submit your request

## Troubleshooting

### Bot not responding
- Check that `TELEGRAM_BOT_TOKEN` is correct
- Make sure the bot is running (check terminal for errors)
- Verify you've started a chat with the bot in Telegram

### Ombi API errors
- Verify `OMBI_URL` is correct and accessible from your Mac
- Check `OMBI_API_KEY` is valid
- Test Ombi API directly: `curl -H "ApiKey: YOUR_KEY" http://ombi-url/api/v1/Search/movie/test`

### No results found
- Try a different search query
- Use an IMDb link instead: `https://www.imdb.com/title/tt0133093/`
- Check Ombi logs for search errors

## Notes

- **Local Ombi**: If Ombi is running in Docker, use `http://localhost:3579` or `http://host.docker.internal:3579` depending on your setup
- **No ngrok needed**: The bot uses polling (outbound connections only), so no reverse proxy or exposed ports needed
- **No WEBAPP_URL needed**: The bot uses in-chat buttons, not Telegram Web Apps
