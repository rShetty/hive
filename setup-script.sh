#!/bin/bash

# Omnigent + OpenCode VPS Setup Script
# Run this on your VPS as root

set -e

echo "=== Omnigent + OpenCode VPS Setup ==="
echo "Target: $(hostname) at $(curl -s ifconfig.me)"
echo ""

# Update system
echo "[1/7] Updating system..."
apt update && apt upgrade -y

# Install dependencies
echo "[2/7] Installing dependencies..."
apt install -y python3 python3-pip python3-venv curl git tmux

# Install Node.js 22 LTS
echo "[3/7] Installing Node.js 22..."
curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
apt install -y nodejs

# Install OpenCode
echo "[4/7] Installing OpenCode..."
curl -fsSL https://opencode.ai/install | bash
export PATH="$HOME/.opencode/bin:$PATH"
echo 'export PATH="$HOME/.opencode/bin:$PATH"' >> ~/.bashrc

# Verify OpenCode
opencode --version

# Install Omnigent
echo "[5/7] Installing Omnigent..."
curl -fsSL https://omnigent.ai/install.sh | sh

# Configure for remote access
echo "[6/7] Configuring OpenCode for remote access..."
mkdir -p ~/.config/opencode

cat > ~/.config/opencode/opencode.json << 'EOF'
{
  "$schema": "https://opencode.ai/config.json",
  "server": {
    "port": 4096,
    "hostname": "0.0.0.0",
    "mdns": false
  }
}
EOF

# Configure firewall
echo "[7/7] Configuring firewall..."
if command -v ufw &> /dev/null; then
    ufw allow 4096/tcp
    echo "Port 4096 allowed through firewall"
fi

echo ""
echo "=== Setup Complete ==="
echo ""
echo "Next steps:"
echo "1. Set your MiMo API key:"
echo "   export MIMO_API_KEY='your-key-here'"
echo ""
echo "2. Start Omnigent:"
echo "   omnigent server start"
echo ""
echo "3. Connect locally:"
echo "   ssh -L 4096:localhost:4096 root@$(curl -s ifconfig.me)"
echo "   Then open: http://localhost:4096"
echo ""
echo "Or use Tailscale for secure VPN access."
