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


# Load environment variables
export $(cat .env | grep -v '^#' | xargs)

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

# Check if virtual environment exists
if [ -d "venv" ]; then
    echo "📦 Activating virtual environment..."
    source venv/bin/activate
fi

# Start bot
echo "🤖 Starting Telegram bot..."
echo ""
python bot.py

