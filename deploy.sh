#!/bin/bash
# Deploy the laparoscopic camera trainer application
# Run this after test_camera.sh confirms the camera works

set -e

echo "========================================"
echo " Deploying Laparoscopic Camera Trainer"
echo "========================================"
echo ""

PROJECT_DIR="/home/pi/lapcam"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Copy application files
echo "[1/3] Copying application files..."
cp "$SCRIPT_DIR/lapcam.py" "$PROJECT_DIR/"
cp "$SCRIPT_DIR/config.json" "$PROJECT_DIR/"
echo "Files copied to $PROJECT_DIR"

# Install systemd service
echo "[2/3] Installing systemd service..."
sudo cp "$SCRIPT_DIR/lapcam.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable lapcam.service
echo "Service installed and enabled"

# Start the service
echo "[3/3] Starting service..."
sudo systemctl start lapcam.service
sleep 2

if sudo systemctl is-active --quiet lapcam.service; then
    echo "Service is running!"
else
    echo "Service failed to start. Check logs:"
    echo "  sudo journalctl -u lapcam.service -n 20"
fi

echo ""
echo "========================================"
echo " Deployment complete!"
echo "========================================"
echo ""
echo "Useful commands:"
echo "  sudo systemctl status lapcam.service   - check status"
echo "  sudo systemctl restart lapcam.service   - restart"
echo "  sudo systemctl stop lapcam.service      - stop"
echo "  sudo journalctl -u lapcam.service -f    - follow logs"
