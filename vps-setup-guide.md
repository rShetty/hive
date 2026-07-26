# Omnigent + OpenCode VPS Setup Guide

## Prerequisites
- VPS at 187.127.140.125 with root access
- SSH client on your local machine
- Basic Linux command line knowledge

## Step 1: Connect to Your VPS

```bash
ssh root@187.127.140.125
```

## Step 2: Update System

```bash
apt update && apt upgrade -y
```

## Step 3: Install Required Dependencies

```bash
# Install Python 3.12+
apt install -y python3 python3-pip python3-venv

# Install Node.js 22 LTS
curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
apt install -y nodejs

# Install tmux
apt install -y tmux

# Install curl (should already be installed)
apt install -y curl
```

## Step 4: Install OpenCode (<1.8 version)

```bash
# Method 1: Using npm (install specific version)
npm install -g opencode-ai@1.7.0

# Verify installation
opencode --version

# Method 2: Using the official installer
curl -fsSL https://opencode.ai/install | bash
```

## Step 5: Install Omnigent

```bash
# Install using the official installer
curl -fsSL https://omnigent.ai/install.sh | sh

# Or install via pip
pip install omnigent

# Or using uv
uv pip install omnigent

# Verify installation
omnigent --version
```

## Step 6: Configure MiMo v21.5 Free Model

Create configuration file:

```bash
mkdir -p ~/.config/omnigent
cat > ~/.config/omnigent/config.yaml << 'EOF'
# Omnigent Configuration for OpenCode with MiMo
# MiMo v2.5 (mimo-v2.5) - Free tier via OpenRouter or Xiaomi API

models:
  mimo-v2.5:
    provider: openrouter
    model_id: xiaomi/mimo-v2.5
    api_key: YOUR_OPENROUTER_API_KEY
    
# Or use Xiaomi's official API
# models:
#   mimo-v2.5:
#     provider: anthropic
#     base_url: https://api.xiaomimimo.com/anthropic/v1/messages
#     api_key: YOUR_MIMO_API_KEY

harness: opencode

default_model: mimo-v2.5
EOF
```

## Step 7: Set Up MiMo API Credentials

### Option A: Using OpenRouter (Free Tier Available)

1. Sign up at https://openrouter.ai
2. Get your API key
3. Set environment variable:

```bash
export OPENROUTER_API_KEY="sk-or-your-key-here"

# Or add to your shell profile
echo 'export OPENROUTER_API_KEY="sk-or-your-key-here"' >> ~/.bashrc
source ~/.bashrc
```

### Option B: Using Xiaomi's Official API

1. Sign up at https://platform.xiaomimimo.com
2. Get your API key
3. Set environment variable:

```bash
export MIMO_API_KEY="your-mimo-api-key"

# Add to shell profile
echo 'export MIMO_API_KEY="your-mimo-api-key"' >> ~/.bashrc
source ~/.bashrc
```

## Step 8: Configure OpenCode for Remote Access

```bash
mkdir -p ~/.config/opencode

cat > ~/.config/opencode/opencode.json << 'EOF'
{
  "$schema": "https://opencode.ai/config.json",
  "server": {
    "port": 4096,
    "hostname": "0.0.0.0",
    "mdns": false
  },
  "model": {
    "provider": "openrouter",
    "model": "xiaomi/mimo-v2.5"
  }
}
EOF
```

## Step 9: Start Omnigent with OpenCode

```bash
# Start the server
omnigent server start

# Or launch directly with OpenCode harness
omnigent opencode
```

## Step 10: Connect Locally

### From Your Local Machine

**Option A: Direct SSH Connection**
```bash
ssh root@187.127.140.125
```

**Option B: Tailscale VPN (Recommended)**

1. Install Tailscale on VPS:
```bash
curl -fsSL https://tailscale.com/install.sh | sh
tailscale up
```

2. Install Tailscale locally
3. Connect via Tailscale IP

**Option C: Port Forwarding**
```bash
# From local machine
ssh -L 4096:localhost:4096 root@187.127.140.125
# Then access http://localhost:4096
```

## Step 11: Access Web UI

Once server is running:
- Local: http://187.127.140.125:4096
- Via Tailscale: http://[tailscale-ip]:4096

## Troubleshooting

### Check Services
```bash
# Check Omnigent status
omnigent server status

# Check OpenCode
which opencode
opencode --version

# Check if port is listening
netstat -tlnp | grep 4096
```

### Firewall Configuration
```bash
# Allow port 4096
ufw allow 4096/tcp

# Or using iptables
iptables -A INPUT -p tcp --dport 4096 -j ACCEPT
```

### View Logs
```bash
# Omnigent logs
omnigent logs

# System logs
journalctl -u omnigent -f
```

## Security Recommendations

1. **Change SSH port** (optional but recommended)
2. **Disable root login** after setting up a regular user
3. **Enable firewall** (ufw)
4. **Use Tailscale** for secure VPN access
5. **Set up SSL/TLS** with Let's Encrypt if exposing to public

---

## Quick Reference Commands

| Command | Description |
|---------|-------------|
| `omnigent server start` | Start Omnigent server |
| `omnigent server status` | Check server status |
| `omnigent stop` | Stop all services |
| `omnigent setup` | Configure credentials |
| `opencode --version` | Check OpenCode version |

---

*Guide created for connecting to VPS at 187.127.140.125*
