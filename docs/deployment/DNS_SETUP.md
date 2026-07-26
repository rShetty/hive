# DNS Setup Guide for Hive

This guide walks you through setting up DNS for your Hive deployment.

## Overview

You need to point your domain (e.g., `hive.rajeev.me`) to your VPS IP address so Traefik can automatically provision SSL certificates via Let's Encrypt.

## Step-by-Step Setup

### 1. Find Your VPS IP Address

```bash
# On your VPS, run:
curl ifconfig.me
echo "Your VPS IP is above"
```

Or check your VPS provider dashboard.

### 2. Add DNS A Record

**For Cloudflare:**
1. Log into Cloudflare dashboard
2. Select your domain (`rajeev.me`)
3. Go to **DNS** → **Records**
4. Click **Add Record**
5. Configure:
   - **Type**: `A`
   - **Name**: `hive` (this creates hive.rajeev.me)
   - **IPv4 Address**: `YOUR_VPS_IP`
   - **Proxy status**: 🟡 DNS only (not proxied) - or 🟠 Proxied if you want Cloudflare's CDN
   - **TTL**: `Auto`
6. Click **Save**

**For Namecheap:**
1. Log into Namecheap account
2. Go to **Domain List** → Click **Manage** next to your domain
3. Go to **Advanced DNS** tab
4. Click **Add New Record**
5. Configure:
   - **Type**: `A Record`
   - **Host**: `hive`
   - **Value**: `YOUR_VPS_IP`
   - **TTL**: `Automatic`
6. Click **Save**

**For GoDaddy:**
1. Log into GoDaddy account
2. Go to **My Products** → **DNS** next to your domain
3. Click **Add** under Records
4. Configure:
   - **Type**: `A`
   - **Name**: `hive`
   - **Value**: `YOUR_VPS_IP`
   - **TTL**: `600 seconds`
5. Click **Save**

**For AWS Route53:**
1. Go to Route53 console
2. Select your hosted zone
3. Click **Create Record**
4. Configure:
   - **Record name**: `hive`
   - **Record type**: `A`
   - **Value**: `YOUR_VPS_IP`
   - **TTL**: `300`
5. Click **Create**

**For Google Domains:**
1. Go to Google Domains
2. Select your domain → **DNS**
3. Under **Custom resource records**:
   - **Name**: `hive`
   - **Type**: `A`
   - **TTL**: `1H`
   - **Data**: `YOUR_VPS_IP`
4. Click **Save**

### 3. Verify DNS Propagation

```bash
# Check if DNS is resolving
dig hive.rajeev.me +short

# Or using nslookup
nslookup hive.rajeev.me

# Should output your VPS IP
```

**Note**: DNS propagation can take 1 minute to 24 hours (usually within 5 minutes).

### 4. Open Firewall Ports

Ensure your VPS firewall allows incoming traffic on:
- Port 80 (HTTP) - for Let's Encrypt challenge
- Port 443 (HTTPS) - for your application

```bash
# For UFW (Ubuntu):
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw reload

# For firewalld (CentOS/RHEL):
sudo firewall-cmd --permanent --add-port=80/tcp
sudo firewall-cmd --permanent --add-port=443/tcp
sudo firewall-cmd --reload

# For iptables:
sudo iptables -A INPUT -p tcp --dport 80 -j ACCEPT
sudo iptables -A INPUT -p tcp --dport 443 -j ACCEPT
```

### 5. Deploy Hive

```bash
# SSH to your VPS
git clone https://github.com/rshetty/agent-marketplace.git hive
cd hive

# Create environment file with your domain
ENCRYPTION_KEY=$(openssl rand -base64 32)
SECRET_KEY=$(openssl rand -hex 32)

cat > .env << EOF
ENCRYPTION_KEY=$ENCRYPTION_KEY
SECRET_KEY=$SECRET_KEY
MARKETPLACE_URL=https://hive.rajeev.me
EOF

# Build and start
docker-compose -f docker-compose.prod.yml up -d --build

# Watch logs to see SSL certificate issuance
docker-compose -f docker-compose.prod.yml logs -f traefik
```

### 6. Verify SSL Certificate

Once Traefik shows the certificate is obtained:

```bash
# Test your site
curl -I https://hive.rajeev.me

# Should show HTTP/2 200 with valid SSL
```

Visit `https://hive.rajeev.me` in your browser - you should see the Hive homepage with a valid SSL certificate (no warnings).

### 7. Traefik Dashboard (Optional)

If you enabled the Traefik dashboard, visit:
- URL: `https://traefik.hive.rajeev.me`
- Username: `admin`
- Password: `admin` (change this in docker-compose.prod.yml!)

## Troubleshooting

### Issue: DNS not resolving
```bash
# Check from different locations
dig @8.8.8.8 hive.rajeev.me

# Flush local DNS cache (macOS)
sudo dscacheutil -flushcache

# Wait longer - DNS propagation takes time
```

### Issue: SSL certificate not issued
```bash
# Check Traefik logs
docker-compose -f docker-compose.prod.yml logs traefik

# Ensure port 80 is open (required for ACME challenge)
curl -v http://hive.rajeev.me

# Restart Traefik
docker-compose -f docker-compose.prod.yml restart traefik
```

### Issue: "Bad Gateway" error
- Check if marketplace container is running: `docker ps`
- Check marketplace logs: `docker-compose -f docker-compose.prod.yml logs marketplace`
- Ensure all containers are on the same network

## Custom Domain

To use a different domain:
1. Update DNS A record to point to your VPS
2. Edit `docker-compose.prod.yml`:
   ```yaml
   - "traefik.http.routers.hive.rule=Host(`your-domain.com`)"
   ```
3. Update `.env`:
   ```
   MARKETPLACE_URL=https://your-domain.com
   ```
4. Restart: `docker-compose -f docker-compose.prod.yml up -d`

## Support

If you encounter issues:
1. Check DNS propagation: https://dnschecker.org
2. Verify SSL: https://www.sslshopper.com/ssl-checker.html
3. Check Traefik docs: https://doc.traefik.io/
