# Deploy to hive.rajeev.me

## Prerequisites
- Docker & Docker Compose installed
- Port 80 and 443 open
- DNS A record pointing to your VPS IP

## Deployment Steps

### 1. On Your VPS, clone the repo
```bash
git clone https://github.com/rShetty/agent-marketplace.git
cd agent-marketplace
```

### 2. Create environment file
```bash
# Generate secure keys
ENCRYPTION_KEY=$(openssl rand -base64 32)
SECRET_KEY=$(openssl rand -hex 32)

cat > .env << EOF
ENCRYPTION_KEY=$ENCRYPTION_KEY
SECRET_KEY=$SECRET_KEY
EOF
```

### 3. Build the agent image
```bash
docker build -t agent-marketplace-agent:latest -f docker/Dockerfile.agent docker/
```

### 4. Start the services
```bash
docker-compose -f docker-compose.prod.yml up -d
```

### 5. Verify it's running
```bash
# Check logs
docker-compose -f docker-compose.prod.yml logs -f marketplace

# Test health endpoint
curl https://hive.rajeev.me/api/health
```

### 6. DNS Setup
Make sure you have an A record:
- Name: `hive`
- Type: `A`
- Value: `YOUR_VPS_IP`

## Management

### View logs
```bash
docker-compose -f docker-compose.prod.yml logs -f
```

### Restart
```bash
docker-compose -f docker-compose.prod.yml restart
```

### Update
```bash
git pull
docker-compose -f docker-compose.prod.yml down
docker-compose -f docker-compose.prod.yml up -d --build
```

## OpenClaw Deployment Modes

Hive supports two ways to host OpenClaw agents. Pick the one that fits your setup.

### Option A: Single-Host Local Deploy (recommended, truly one-click)
OpenClaw containers run on the **same Docker daemon** as Hive. No separate VPS, no SSH keys, no remote file copying.

**How to enable:**
1. Build or pull the OpenClaw image:
   ```bash
   # Use the pre-built image from Docker Hub (if published)
   # OR build locally:
   docker build -f docker/Dockerfile.openclaw -t openclaw/openclaw:latest docker/
   ```
2. Make sure `OPENCLAW_IMAGE` is set in your environment (already defaults to `openclaw/openclaw:latest` in `docker-compose.prod.yml`).
3. **Do NOT** set `OPENCLAW_VPS_HOST` or `OPENCLAW_VPS_SSH_KEY_PATH` in your Hive container environment.
4. Hive will automatically fall back to creating containers locally via the mounted Docker socket (`/var/run/docker.sock`).

**Per-agent routing (optional):**
- If you set `HIVE_DOMAIN` (e.g., `hive.rajeev.me`) and run Traefik, OpenClaw containers get automatic Traefik labels so each agent gets its own subdomain: `slug.hive.rajeev.me`.
- Without Traefik, agents are accessible directly at `http://YOUR_VPS_IP:PORT`.

### Option B: Remote VPS Deploy (legacy)
Each OpenClaw agent is deployed to a separate VPS via SSH + Docker Compose.

**Requirements:**
- A separate VPS with Docker installed
- SSH key mounted into the Hive container
- Environment variables set:
  ```bash
  OPENCLAW_VPS_HOST=your-vps-ip
  OPENCLAW_VPS_SSH_KEY_PATH=/root/.ssh/openclaw_deploy_key
  OPENCLAW_VPS_SSH_USER=root
  OPENCLAW_VPS_SSH_PORT=22
  ```

**Pre-built image strongly recommended:**
- If you don't set `OPENCLAW_IMAGE`, Hive tries to copy `Dockerfile.openclaw` to the remote VPS and build there. This is slow and brittle.
- **Build once, push, and reuse:**
  ```bash
  ./scripts/build-openclaw.sh latest
  ```
- Then set `OPENCLAW_IMAGE=youruser/openclaw:latest` in Hive's environment.

## SSL Certificate
Traefik automatically handles SSL via Let's Encrypt. The first request may take a few seconds as the certificate is issued.

## Access Traefik Dashboard
Visit: https://traefik.hive.rajeev.me
- Username: admin
- Password: admin (change in docker-compose.prod.yml)
