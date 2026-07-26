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

To backup:
```bash
ssh root@187.127.140.125 'tar -czf /tmp/hive-backup.tar.gz /opt/hive/data'
scp root@187.127.140.125:/tmp/hive-backup.tar.gz ./hive-backup-$(date +%Y%m%d).tar.gz
```

## Security Considerations

⚠️ **IMPORTANT:** This deployment script is designed for development/testing. For production:

1. **Don't run as root**: Create a dedicated user account
2. **Use a reverse proxy**: Put Nginx/Caddy in front with SSL/TLS
3. **Restrict port access**: Use firewall rules (ufw/iptables)
4. **Secure the Docker socket**: Use a Docker socket proxy instead of mounting it directly
5. **Use secrets management**: Consider using Docker secrets or a vault service
6. **Enable monitoring**: Set up logging and monitoring (Prometheus, Grafana, etc.)
7. **Regular backups**: Automate database backups
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
