#!/bin/bash

# Deployment script for Hive application
# Pulls code from GitHub and deploys to remote server via SSH
#
# Usage:
#   ./deploy.sh [options]
#
# Options (all can also be set as environment variables):
#   --host HOST              Remote server IP/hostname (default: 187.127.140.125)
#   --port PORT              App port on remote server (default: 8080)
#   --branch BRANCH          Git branch to deploy (default: main)
#   --openclaw-host HOST     VPS host where OpenClaw containers are deployed
#                            (defaults to same as --host)
#   --openclaw-ssh-key PATH  Path to SSH key on the remote server for OpenClaw deploys
#                            (default: /root/.ssh/id_ed25519)
#   --openclaw-ssh-user USER SSH user for OpenClaw VPS (default: root)
#   --openclaw-ssh-port PORT SSH port for OpenClaw VPS (default: 22)
#
# Environment variables (override defaults):
#   REMOTE_HOST, REMOTE_PORT, GIT_BRANCH
#   OPENCLAW_VPS_HOST, OPENCLAW_VPS_SSH_KEY_PATH,
#   OPENCLAW_VPS_SSH_USER, OPENCLAW_VPS_SSH_PORT
#   ENCRYPTION_KEY, SECRET_KEY

set -e  # Exit on error

# ---- Defaults ----
REMOTE_SERVER="${REMOTE_HOST:-187.127.140.125}"
REMOTE_PORT="${REMOTE_PORT:-8080}"
APP_NAME="hive"
IMAGE_NAME="hive-marketplace"
GITHUB_REPO="https://github.com/rshetty/hive.git"
GIT_BRANCH="${GIT_BRANCH:-main}"

# OpenClaw defaults (can be overridden)
OC_HOST="${OPENCLAW_VPS_HOST:-}"
OC_SSH_KEY="${OPENCLAW_VPS_SSH_KEY_PATH:-/root/.ssh/id_ed25519}"
OC_SSH_USER="${OPENCLAW_VPS_SSH_USER:-root}"
OC_SSH_PORT="${OPENCLAW_VPS_SSH_PORT:-22}"

# ---- Parse CLI flags ----
while [[ $# -gt 0 ]]; do
    case "$1" in
        --host)            REMOTE_SERVER="$2"; shift 2 ;;
        --port)            REMOTE_PORT="$2";   shift 2 ;;
        --branch)          GIT_BRANCH="$2";    shift 2 ;;
        --openclaw-host)   OC_HOST="$2";       shift 2 ;;
        --openclaw-ssh-key) OC_SSH_KEY="$2";   shift 2 ;;
        --openclaw-ssh-user) OC_SSH_USER="$2"; shift 2 ;;
        --openclaw-ssh-port) OC_SSH_PORT="$2"; shift 2 ;;
        --help|-h)
            sed -n '3,25p' "$0"
            exit 0
            ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

# If OpenClaw host not explicitly set, default to the app server itself
OC_HOST="${OC_HOST:-$REMOTE_SERVER}"

REMOTE_HOST="root@${REMOTE_SERVER}"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Helper functions
log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

log_info "Deploying to ${REMOTE_SERVER}:${REMOTE_PORT} (branch: ${GIT_BRANCH})"
log_info "OpenClaw VPS host: ${OC_HOST} | SSH key: ${OC_SSH_KEY}"

# Load persisted keys from server's .env if not already set locally
if [ -z "$ENCRYPTION_KEY" ] || [ -z "$SECRET_KEY" ]; then
    log_info "Checking server for persisted keys at /opt/${APP_NAME}/.env ..."
    SERVER_ENV=$(ssh "$REMOTE_HOST" "cat /opt/${APP_NAME}/.env 2>/dev/null || true")
    if echo "$SERVER_ENV" | grep -q "ENCRYPTION_KEY="; then
        eval "$(echo "$SERVER_ENV" | grep -E '^(ENCRYPTION_KEY|SECRET_KEY)=')"
        log_info "Loaded keys from server .env"
    fi
fi

# Check if required environment variables are set
KEYS_GENERATED=false
if [ -z "$ENCRYPTION_KEY" ]; then
    log_warn "ENCRYPTION_KEY not set. Generating a random one..."
    export ENCRYPTION_KEY=$(openssl rand -hex 32)
    KEYS_GENERATED=true
    log_warn "⚠️  IMPORTANT: Save the generated keys! They will be displayed at the end."
fi

if [ -z "$SECRET_KEY" ]; then
    log_warn "SECRET_KEY not set. Generating a random one..."
    export SECRET_KEY=$(openssl rand -hex 32)
    KEYS_GENERATED=true
fi

# Step 1: Check SSH connection
log_info "Testing SSH connection to $REMOTE_HOST..."
ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new "$REMOTE_HOST" "echo 'Connection successful'" > /dev/null 2>&1

if [ $? -ne 0 ]; then
    log_error "Cannot connect to $REMOTE_HOST. Please check SSH access."
    exit 1
fi

log_info "SSH connection successful"

# Step 2: Install/verify remote server dependencies
log_info "Checking and installing remote server dependencies..."
ssh -o StrictHostKeyChecking=accept-new "$REMOTE_HOST" bash <<'VERIFY'
set -e

# Check for git
if ! command -v git &> /dev/null; then
    echo "Installing git..."
    apt-get update && apt-get install -y git
fi

# Check for docker
if ! command -v docker &> /dev/null; then
    echo "Installing Docker..."
    curl -fsSL https://get.docker.com | sh
fi

# Check for docker-compose
if ! command -v docker-compose &> /dev/null; then
    echo "Installing docker-compose..."
    curl -L "https://github.com/docker/compose/releases/latest/download/docker-compose-$(uname -s)-$(uname -m)" -o /usr/local/bin/docker-compose
    chmod +x /usr/local/bin/docker-compose
fi

echo "All dependencies ready"
VERIFY

if [ $? -ne 0 ]; then
    log_error "Remote server dependency setup failed"
    exit 1
fi

log_info "Remote server dependencies ready"

# Step 3: Create deployment directory
log_info "Creating deployment directory on remote server..."
ssh "$REMOTE_HOST" "mkdir -p /opt/${APP_NAME}"

# Step 4: Pull code from GitHub and deploy on remote server
log_info "Pulling code from GitHub and deploying on remote server..."
ssh "$REMOTE_HOST" bash <<ENDSSH
set -e

cd /opt/${APP_NAME}

# Clone or update repository
if [ -d ".git" ]; then
    echo "Repository exists, pulling latest changes..."
    git fetch origin
    git checkout ${GIT_BRANCH}
    git pull origin ${GIT_BRANCH}
else
    echo "Cloning repository..."
    git clone -b ${GIT_BRANCH} ${GITHUB_REPO} .
fi

# Create production docker-compose configuration
echo "Creating production docker-compose configuration..."
cat > docker-compose.prod.yml <<DOCKEREOF

services:
  marketplace:
    build: .
    image: ${IMAGE_NAME}:latest
    ports:
      - "127.0.0.1:${REMOTE_PORT}:${REMOTE_PORT}"
    environment:
      - PORT=${REMOTE_PORT}
      - DATABASE_URL=sqlite+aiosqlite:////app/data/agent_marketplace.db
      - ENCRYPTION_KEY=${ENCRYPTION_KEY}
      - SECRET_KEY=${SECRET_KEY}
      - HIVE_URL=${HIVE_URL_OVERRIDE:-http://${REMOTE_SERVER}:${REMOTE_PORT}}
      - HIVE_DOMAIN=${HIVE_DOMAIN:-}
      - HIVE_SSL_CERT=${HIVE_SSL_CERT:-}
      - HIVE_SSL_KEY=${HIVE_SSL_KEY:-}
      - ALLOWED_ORIGINS=${ALLOWED_ORIGINS_OVERRIDE:-http://${REMOTE_SERVER}:${REMOTE_PORT},http://localhost:${REMOTE_PORT}}
      - OPENCLAW_IMAGE=${OPENCLAW_IMAGE:-openclaw/openclaw:latest}
      - OPENCLAW_VPS_HOST=${OC_HOST}
      - OPENCLAW_VPS_SSH_KEY_PATH=/root/.ssh/openclaw_deploy_key
      - OPENCLAW_VPS_SSH_USER=${OC_SSH_USER}
      - OPENCLAW_VPS_SSH_PORT=${OC_SSH_PORT}
      - COOKIE_SECURE=1
    volumes:
      - /opt/${APP_NAME}/data:/app/data
      - /var/run/docker.sock:/var/run/docker.sock:ro
      - /root/.ssh:/root/.ssh:ro
    restart: unless-stopped
    networks:
      - agent-marketplace

networks:
  agent-marketplace:
    driver: bridge
DOCKEREOF

# Ensure the SSH key used for OpenClaw deploys exists and is named correctly.
# The container mounts /root/.ssh read-only, so we just alias the key.
if [ -f "${OC_SSH_KEY}" ]; then
    cp -n "${OC_SSH_KEY}" /root/.ssh/openclaw_deploy_key 2>/dev/null || true
    chmod 600 /root/.ssh/openclaw_deploy_key
    echo "OpenClaw SSH key ready at /root/.ssh/openclaw_deploy_key"
else
    echo "WARNING: OpenClaw SSH key not found at ${OC_SSH_KEY} — OpenClaw deploy will fail"
fi

echo "Checking what's using port ${REMOTE_PORT}..."
ss -tlnp 2>/dev/null | grep ":${REMOTE_PORT}" || true

echo "Stopping all containers using port ${REMOTE_PORT}..."
for cid in \$(docker ps -q); do
    if docker port \$cid 2>/dev/null | grep -q "${REMOTE_PORT}"; then
        echo "Stopping container \$cid..."
        docker stop \$cid
        docker rm \$cid
    fi
done

echo "Stopping existing compose containers..."
docker-compose -f docker-compose.prod.yml down --remove-orphans 2>/dev/null || true

# Kill any remaining process on the port
echo "Killing any process on port ${REMOTE_PORT}..."
PID=\$(ss -tlnp | grep ":${REMOTE_PORT}" | grep -oP 'pid=\\K[0-9]+' | head -1)
if [ -n "\$PID" ]; then
    echo "Killing process \$PID on port ${REMOTE_PORT}..."
    kill -9 \$PID 2>/dev/null || true
    sleep 1
fi

echo "Building Docker image..."
docker-compose -f docker-compose.prod.yml build

if [ \$? -ne 0 ]; then
    echo "Failed to build Docker image"
    exit 1
fi

echo "Starting new containers..."
docker-compose -f docker-compose.prod.yml up -d

if [ \$? -ne 0 ]; then
    echo "Failed to start containers"
    exit 1
fi

echo "Deployment complete!"
docker-compose -f docker-compose.prod.yml ps

# Reload nginx if installed
if command -v nginx &> /dev/null; then
    echo "Reloading nginx..."
    nginx -s reload 2>/dev/null || systemctl reload nginx 2>/dev/null || true
fi
ENDSSH

if [ $? -ne 0 ]; then
    log_error "Deployment failed on remote server"
    exit 1
fi

# Step 5: Verify deployment
log_info "Verifying deployment..."
sleep 5
ssh "$REMOTE_HOST" "curl -s http://localhost:${REMOTE_PORT}/api/health" > /dev/null 2>&1

if [ $? -eq 0 ]; then
    log_info "✅ Deployment successful!"
    log_info "Application is running at: http://${REMOTE_SERVER}:${REMOTE_PORT}"
    log_info "OpenClaw will deploy to: ${OC_HOST} (SSH user: ${OC_SSH_USER})"
else
    log_warn "Deployment completed but health check failed. The application may still be starting up."
    log_info "Check logs with: ssh $REMOTE_HOST 'cd /opt/${APP_NAME} && docker-compose -f docker-compose.prod.yml logs -f'"
fi

# Persist keys to server .env if they were generated this run
if [ "$KEYS_GENERATED" = true ]; then
    log_info "Saving generated keys to /opt/${APP_NAME}/.env on server..."
    ssh "$REMOTE_HOST" "cat > /opt/${APP_NAME}/.env << 'ENVEOF'
ENCRYPTION_KEY=${ENCRYPTION_KEY}
SECRET_KEY=${SECRET_KEY}
ENVEOF"
    log_info ""
    log_warn "⚠️  IMPORTANT: New keys were generated and saved to /opt/${APP_NAME}/.env on the server."
    log_warn "All previously encrypted data is unreadable with the new key."
    echo ""
    echo "export ENCRYPTION_KEY='${ENCRYPTION_KEY}'"
    echo "export SECRET_KEY='${SECRET_KEY}'"
    echo ""
fi

log_info ""
log_info "Useful commands:"
log_info "  View logs: ssh $REMOTE_HOST 'cd /opt/${APP_NAME} && docker-compose -f docker-compose.prod.yml logs -f'"
log_info "  Restart: ssh $REMOTE_HOST 'cd /opt/${APP_NAME} && docker-compose -f docker-compose.prod.yml restart'"
log_info "  Stop: ssh $REMOTE_HOST 'cd /opt/${APP_NAME} && docker-compose -f docker-compose.prod.yml down'"
