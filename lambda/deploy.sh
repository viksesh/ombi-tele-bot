#!/bin/bash
# Helper script to create Lambda deployment package
# Note: For best results, use Docker to build (see below) or build on Linux

set -e

echo "📦 Creating Lambda deployment package..."

# Create a temporary directory for the package
TEMP_DIR=$(mktemp -d)
echo "Using temp directory: $TEMP_DIR"

# Copy the handler file
cp webhook_handler.py "$TEMP_DIR/"

# Install dependencies for Linux (Lambda's environment)
echo "Installing dependencies for Linux (Lambda environment)..."
echo "⚠️  If you're on macOS, consider using Docker method (see README) for best compatibility"

# Try to install with platform-specific flags (ARM64 for Lambda Graviton2)
# If on Apple Silicon Mac, this will work natively
ARCH=$(uname -m)
if [ "$ARCH" = "arm64" ]; then
    echo "Detected ARM64 system - building ARM64 package for Lambda Graviton2"
    pip install -r requirements.txt -t "$TEMP_DIR" --quiet
else
    echo "⚠️  Building ARM64 package on $ARCH system - may have compatibility issues"
    echo "   Consider using deploy-docker.sh instead for best results"
    pip install -r requirements.txt -t "$TEMP_DIR" \
        --platform linux_arm64 \
        --only-binary :all: \
        --python-version 3.11 \
        --implementation cp \
        --abi cp311 \
        2>&1 | grep -v "WARNING: Target platform" || {
        echo "⚠️  Platform-specific install failed, trying regular install..."
        pip install -r requirements.txt -t "$TEMP_DIR" --quiet
    }
fi

# Verify webhook_handler.py is in the package
if [ ! -f "$TEMP_DIR/webhook_handler.py" ]; then
    echo "❌ ERROR: webhook_handler.py not found in package!"
    exit 1
fi

# Create ZIP file
ZIP_FILE="lambda-webhook.zip"
cd "$TEMP_DIR"
zip -r "$OLDPWD/$ZIP_FILE" . -q
cd "$OLDPWD"

# Verify ZIP structure
echo ""
echo "📋 Verifying package structure..."
unzip -l "$ZIP_FILE" | head -20

# Clean up
rm -rf "$TEMP_DIR"

echo ""
echo "✅ Created deployment package: $ZIP_FILE"
echo ""
echo "⚠️  IMPORTANT: If you still get ImportModuleError, use Docker method:"
echo "   docker run --rm -v \$(pwd):/var/task public.ecr.aws/lambda/python:3.11 pip install -r requirements.txt -t ."
echo ""
echo "Next steps:"
echo "1. Upload $ZIP_FILE to Lambda function"
echo "2. Set handler to: webhook_handler.lambda_handler"
echo "3. Set runtime to: Python 3.11"
echo "4. Configure environment variables:"
echo "   - TELEGRAM_BOT_TOKEN"
echo "   - TELEGRAM_GROUP_CHAT_ID"
echo "   - TELEGRAM_GROUP_THREAD_ID (optional)"

