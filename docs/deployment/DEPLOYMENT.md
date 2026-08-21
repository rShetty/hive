# Hive Deployment Guide

This guide covers deploying the Hive application to a remote server using Docker.

## Prerequisites

### Local Machine
- SSH access to the remote server
- `openssl` for generating keys (usually pre-installed on macOS/Linux)

### Remote Server
- Ubuntu/Debian Linux (recommended)
- Docker and Docker Compose installed
- Git installed
- SSH access with public key authentication
- Port 8080 open (or your configured port)

## Quick Start

### 1. Set Up Environment Variables

The deployment script requires two secret keys. You can either:

**Option A: Let the script generate them** (first deployment)
```bash
cd /Users/rshetty/hive
./deploy.sh
```
The script will generate random keys and display them at the end. **Save these keys securely!**

**Option B: Use existing keys** (subsequent deployments)
```bash
# Set environment variables before running
export ENCRYPTION_KEY='your-encryption-key-from-first-deployment'
export SECRET_KEY='your-secret-key-from-first-deployment'

./deploy.sh
```

**Option C: Use a .env file**
```bash
# Copy the example file
cp .env.example .env

# Edit .env and add your keys
nano .env

# Source it before deploying
source .env
./deploy.sh
```

### 2. Run Deployment

```bash
cd /Users/rshetty/hive
./deploy.sh
```

The script will:
1. Check SSH connectivity
2. Verify remote server has required dependencies (git, docker, docker-compose)
3. Clone/update the repository from GitHub
4. Build the Docker image on the remote server
5. Start the application on port 8080
6. Verify the deployment

## Configuration

You can customize the deployment by setting environment variables:

```bash
# Change the Git branch to deploy
export GIT_BRANCH=develop

# Deploy
./deploy.sh
```

### Available Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `ENCRYPTION_KEY` | Auto-generated | Encryption key for sensitive data |
| `SECRET_KEY` | Auto-generated | JWT token secret |
| `GIT_BRANCH` | `main` | Git branch to deploy |
| `REMOTE_HOST` | `root@187.127.140.125` | SSH connection string |
| `REMOTE_PORT` | `8080` | Port to expose the application |

## Post-Deployment

After successful deployment, the application will be available at:
```
http://187.127.140.125:8080
```

### Useful Commands

**View logs:**
```bash
ssh root@187.127.140.125 'cd /opt/hive && docker-compose -f docker-compose.prod.yml logs -f'
```

**Restart the application:**
```bash
ssh root@187.127.140.125 'cd /opt/hive && docker-compose -f docker-compose.prod.yml restart'
```

**Stop the application:**
```bash
ssh root@187.127.140.125 'cd /opt/hive && docker-compose -f docker-compose.prod.yml down'
```

**SSH into the server:**
```bash
ssh root@187.127.140.125
```

**Check container status:**
```bash
ssh root@187.127.140.125 'cd /opt/hive && docker-compose -f docker-compose.prod.yml ps'
```

## Troubleshooting

### Deployment fails with "permission denied"
- Ensure your SSH key is added to the remote server's `~/.ssh/authorized_keys`
- Test SSH access: `ssh root@187.127.140.125`

### Health check fails
- The application may still be starting up. Wait 30 seconds and check:
  ```bash
  ssh root@187.127.140.125 'curl http://localhost:8080/api/health'
  ```
- Check logs for errors:
  ```bash
  ssh root@187.127.140.125 'cd /opt/hive && docker-compose -f docker-compose.prod.yml logs'
  ```

### "git not found" or "docker not found"
Install the missing dependencies on the remote server:
```bash
ssh root@187.127.140.125

# Install git
apt update && apt install -y git

# Install Docker
curl -fsSL https://get.docker.com | sh

# Install Docker Compose
apt install -y docker-compose
```

### Database/data persistence
The application data is stored in `/opt/hive/data` on the remote server. This directory persists across deployments.

For database backups and restores, see [Backup & Restore](#backup--restore) below.

## Backup & Restore

Hive ships two scripts for consistent SQLite backups:

| Script | Purpose |
|--------|---------|
| `scripts/backup_db.sh` | Consistent, timestamped snapshot via `sqlite3 .backup` (falls back to the Python `sqlite3` backup API), integrity-verified, with retention pruning |
| `scripts/restore_db.sh` | Restores a backup after verifying its integrity (`PRAGMA integrity_check`) and atomically swaps it into place |

Both scripts resolve the target database the same way: `DB_PATH` env var → `DATABASE_URL` (SQLite URLs only) → `/opt/hive/data/agent_marketplace.db`.

### Configuration (environment variables)

| Variable | Default | Description |
|----------|---------|-------------|
| `BACKUP_DIR` | `/var/backups/hive` | Where timestamped backups are written |
| `RETENTION_DAYS` | `14` | Backups older than this are pruned after each run |
| `DB_PATH` | derived from `DATABASE_URL` | Explicit path to the SQLite file |
| `POST_BACKUP_HOOK` | unset | Optional command run after a verified backup; the backup path is passed as `$1` |

### Manual backup

```bash
ssh root@187.127.140.125
cd /opt/hive && git pull   # ensure the scripts are present
BACKUP_DIR=/var/backups/hive ./scripts/backup_db.sh
```

The script is safe to run while the app is running — `.backup` takes a consistent online snapshot. Every snapshot is verified with `PRAGMA integrity_check` before it counts as a backup; files older than `RETENTION_DAYS` are deleted automatically.

### Off-box copies (S3 / restic)

Local backups do not survive disk loss. Ship them off-box with `POST_BACKUP_HOOK`:

```bash
# S3
POST_BACKUP_HOOK='aws s3 cp "$1" s3://my-bucket/hive-db/' ./scripts/backup_db.sh

# restic
export RESTIC_REPOSITORY=/mnt/backups/hive RESTIC_PASSWORD_FILE=/root/.restic-pass
POST_BACKUP_HOOK='restic backup "$1"' ./scripts/backup_db.sh
```

### Schedule with cron or a systemd timer

**cron** (twice daily):

```cron
0 */12 * * * cd /opt/hive && BACKUP_DIR=/var/backups/hive POST_BACKUP_HOOK='aws s3 cp "$1" s3://my-bucket/hive-db/' ./scripts/backup_db.sh >> /var/log/hive-backup.log 2>&1
```

**systemd timer** — `/etc/systemd/system/hive-backup.service`:

```ini
[Unit]
Description=Hive SQLite database backup

[Service]
Type=oneshot
WorkingDirectory=/opt/hive
Environment=BACKUP_DIR=/var/backups/hive
ExecStart=/opt/hive/scripts/backup_db.sh
```

`/etc/systemd/system/hive-backup.timer`:

```ini
[Unit]
Description=Run hive-backup twice daily

[Timer]
OnCalendar=*-*-* 00/12:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

Enable with:

```bash
systemctl daemon-reload && systemctl enable --now hive-backup.timer
systemctl list-timers hive-backup.timer
```

### Verify backups

After each run (and at least monthly as a drill):

```bash
# 1. The script already ran integrity_check, but double-check the latest file:
LATEST=$(ls -t /var/backups/hive/agent_marketplace-*.db | head -1)
sqlite3 "$LATEST" 'PRAGMA integrity_check;'          # must print: ok
sqlite3 "$LATEST" "SELECT count(*) FROM users;"      # plausible row count

# 2. Test-restore into a scratch path (never touches the live database):
DB_PATH=/tmp/restore-drill.db ./scripts/restore_db.sh -y "$LATEST"
sqlite3 /tmp/restore-drill.db 'PRAGMA integrity_check;'   # must print: ok
rm /tmp/restore-drill.db
```

A backup that has never been restored is not a backup — run the drill above whenever you change schema or before upgrades.

### Restore runbook

1. **Stop the app** (restoring under a running writer can corrupt the database):
   ```bash
   ssh root@187.127.140.125
   cd /opt/hive && docker-compose -f docker-compose.prod.yml stop marketplace
   ```
2. **Pick the backup** to restore and run the restore script:
   ```bash
   LATEST=$(ls -t /var/backups/hive/agent_marketplace-*.db | head -1)
   ./scripts/restore_db.sh "$LATEST"    # prompts; use -y to skip the prompt
   ```
   The script verifies the backup's integrity first, keeps a safety copy of the current database as `<db>.pre-restore.bak`, removes stale `-wal`/`-shm` sidecar files, and atomically swaps in the restored file.
3. **Verify**: the script prints `integrity_check = ok` and the table count on success.
4. **Start the app again** and smoke-test:
   ```bash
   docker-compose -f docker-compose.prod.yml up -d marketplace
   curl -fsS http://localhost:8080/api/health
   ```
5. Once satisfied, delete the safety copy: `rm /opt/hive/data/agent_marketplace.db.pre-restore.bak`

## Security Considerations

⚠️ **IMPORTANT:** This deployment script is designed for development/testing. For production:

1. **Don't run as root**: Create a dedicated user account
2. **Use a reverse proxy**: Put Nginx/Caddy in front with SSL/TLS
3. **Restrict port access**: Use firewall rules (ufw/iptables)
4. **Secure the Docker socket**: Use a Docker socket proxy instead of mounting it directly
5. **Use secrets management**: Consider using Docker secrets or a vault service
6. **Enable monitoring**: Set up logging and monitoring (Prometheus, Grafana, etc.)
7. **Regular backups**: Automate database backups (see [Backup & Restore](#backup--restore))
8. **Keep keys secure**: Never commit `.env` or keys to version control

## Advanced: Production Setup

For a production deployment, consider:

1. **Use a non-root user:**
   ```bash
   # On remote server
   useradd -m -s /bin/bash hive
   usermod -aG docker hive
   ```

2. **Set up Nginx reverse proxy with SSL:**
   ```nginx
   server {
       listen 80;
       server_name yourdomain.com;
       
       location / {
           proxy_pass http://127.0.0.1:8080;
           proxy_set_header Host $host;
           proxy_set_header X-Real-IP $remote_addr;
       }
   }
   ```

3. **Use environment-specific configurations:**
   - Separate `.env.production` and `.env.staging`
   - Different encryption keys per environment
   - Different database files per environment

## Support

If you encounter issues:
1. Check the logs (see commands above)
2. Verify all prerequisites are met
3. Review the security considerations
4. Check GitHub repository for updates
