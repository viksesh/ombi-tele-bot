#!/bin/bash
# Build and publish Docker image for Ubuntu 24 (linux/amd64)
# This script builds for linux/amd64 platform to run on Ubuntu servers

set -e

IMAGE_NAME="viksesh/ombi-tele-bot"
VERSION="${1:-1.1}"  # Use first argument as version, default to 1.1
PLATFORM="linux/amd64"  # Build for x86_64 Ubuntu servers

echo "🐳 Building Docker image for Ubuntu 24 (${PLATFORM})..."
echo "Image: ${IMAGE_NAME}:${VERSION}"
echo "Platform: ${PLATFORM}"
echo ""

# Build the image for linux/amd64 platform
echo "Building ${IMAGE_NAME}:${VERSION} for ${PLATFORM}..."
docker build --platform ${PLATFORM} -t ${IMAGE_NAME}:${VERSION} .

echo "Tagging as latest..."
docker tag ${IMAGE_NAME}:${VERSION} ${IMAGE_NAME}:latest

echo ""
echo "✅ Build complete!"
echo ""
echo "ℹ️  Note: Built for ${PLATFORM} (Ubuntu servers)"
echo "   Local testing on ARM Mac won't work - test on your Ubuntu server instead"
echo ""
echo "📤 To publish to Docker Hub:"
echo "   1. docker login"
echo "   2. docker push ${IMAGE_NAME}:${VERSION}"
echo "   3. docker push ${IMAGE_NAME}:latest"
echo ""
read -p "Do you want to publish to Docker Hub now? (y/n) " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    echo ""
    echo "Publishing ${IMAGE_NAME}:${VERSION}..."
    docker push ${IMAGE_NAME}:${VERSION}
    echo ""
    echo "Publishing ${IMAGE_NAME}:latest..."
    docker push ${IMAGE_NAME}:latest
    echo ""
    echo "✅ Published successfully!"
    echo ""
    echo "📝 Next steps:"
    echo "   On your Ubuntu 24 server, update docker-compose.yml to use:"
    echo "   image: ${IMAGE_NAME}:${VERSION}"
    echo ""
    echo "   Then test on the server:"
    echo "   docker run --rm ${IMAGE_NAME}:${VERSION} python --version"
fi

