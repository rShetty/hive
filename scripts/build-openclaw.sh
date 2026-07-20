#!/bin/bash
# Build and push the OpenClaw agent image to a registry.
# Run this locally or in CI whenever docker/Dockerfile.openclaw changes.
#
# Usage:
#   ./scripts/build-openclaw.sh [tag]
#
# Examples:
#   ./scripts/build-openclaw.sh latest
#   ./scripts/build-openclaw.sh v1.0.0
#   DOCKER_USER=myuser ./scripts/build-openclaw.sh latest

set -e

TAG="${1:-latest}"
DOCKER_USER="${DOCKER_USER:-$(whoami)}"
IMAGE_NAME="${DOCKER_USER}/openclaw:${TAG}"

echo "🔨 Building OpenClaw image: ${IMAGE_NAME} ..."
docker build -f docker/Dockerfile.openclaw -t "${IMAGE_NAME}" docker/

echo "📤 Pushing to registry ..."
docker push "${IMAGE_NAME}"

echo "✅ Done! Set OPENCLAW_IMAGE=${IMAGE_NAME} in your Hive environment."
