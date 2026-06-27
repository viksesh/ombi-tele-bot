#!/bin/bash

# Quick start script for local testing on Mac

echo "🚀 Starting Ombi Telegram Bot locally..."
echo ""

# Check if .env file exists
if [ ! -f .env ]; then
    echo "⚠️  No .env file found. Creating .env.example..."
    echo ""
    echo "Please create a .env file with:"
    echo "  TELEGRAM_BOT_TOKEN=your_token"
    echo "  OMBI_URL=http://localhost:3579"
    echo "  OMBI_API_KEY=your_key"
    echo ""
    echo "Or copy from .env.example if it exists"
    exit 1
fi


# Load environment variables (handles inline comments and values with spaces)
set -a
source .env
set +a

# Check required variables
if [ -z "$TELEGRAM_BOT_TOKEN" ]; then
    echo "❌ TELEGRAM_BOT_TOKEN not set in .env"
    exit 1
fi

if [ -z "$OMBI_URL" ]; then
    echo "❌ OMBI_URL not set in .env"
    exit 1
fi

if [ -z "$OMBI_API_KEY" ]; then
    echo "❌ OMBI_API_KEY not set in .env"
    exit 1
fi


echo "✅ Environment variables loaded"
echo ""

# Stop any leftover bot instances from a previous run. Stale processes keep
# polling Telegram (causing getUpdates conflicts) and, more importantly, hold
# the mini app port (WEBAPP_PORT) so the new instance hangs on startup while
# binding it.
STALE_BOTS=$(pgrep -f "python.*bot.py")
if [ -n "$STALE_BOTS" ]; then
    echo "🧹 Stopping leftover bot processes: $STALE_BOTS"
    kill -9 $STALE_BOTS 2>/dev/null
fi

# Free the mini app port if something else is holding it.
if [ -n "$WEBAPP_PORT" ]; then
    PORT_PID=$(lsof -nP -tiTCP:"$WEBAPP_PORT" -sTCP:LISTEN 2>/dev/null)
    if [ -n "$PORT_PID" ]; then
        echo "🧹 Freeing port $WEBAPP_PORT (held by PID $PORT_PID)"
        kill -9 $PORT_PID 2>/dev/null
    fi
fi
echo ""

# Check if virtual environment exists
if [ -d "venv" ]; then
    echo "📦 Activating virtual environment..."
    source venv/bin/activate
fi

# Start bot
echo "🤖 Starting Telegram bot..."
echo ""
python bot.py

