#!/bin/bash

# Local Connection Script for VPS
# Run this on your LOCAL machine to connect to the VPS

VPS_HOST="187.127.140.125"
VPS_USER="root"
VPS_PORT="4096"

echo "=== Connecting to VPS: $VPS_HOST ==="
echo ""
echo "Choose connection method:"
echo "1) Direct SSH (terminal only)"
echo "2) SSH with port forwarding (recommended)"
echo "3) Exit"
echo ""
read -p "Enter choice (1-3): " choice

case $choice in
    1)
        echo "Connecting via SSH..."
        ssh $VPS_USER@$VPS_HOST
        ;;
    2)
        echo "Setting up SSH tunnel..."
        echo "Port $VPS_PORT forwarding to localhost:$VPS_PORT"
        echo "Once connected, open: http://localhost:$VPS_PORT"
        echo ""
        ssh -L $VPS_PORT:localhost:$VPS_PORT $VPS_USER@$VPS_HOST
        ;;
    3)
        echo "Exiting..."
        exit 0
        ;;
    *)
        echo "Invalid choice"
        exit 1
        ;;
esac
