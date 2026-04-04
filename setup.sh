#!/bin/bash
# Laparoscopic Camera Trainer - Raspberry Pi Setup Script
# Run this after first SSH login: bash setup.sh

set -e

echo "========================================"
echo " Laparoscopic Camera Trainer Setup"
echo "========================================"
echo ""

# Update system
echo "[1/6] Updating system packages..."
sudo apt-get update
sudo apt-get upgrade -y

# Install GStreamer and plugins
echo "[2/6] Installing GStreamer..."
sudo apt-get install -y \
    gstreamer1.0-tools \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-ugly \
    gstreamer1.0-libav \
    gstreamer1.0-gl \
    gir1.2-gst-plugins-base-1.0 \
    gir1.2-gstreamer-1.0

# Install Python dependencies
echo "[3/6] Installing Python dependencies..."
sudo apt-get install -y \
    python3-gi \
    python3-gi-cairo \
    python3-gst-1.0 \
    python3-pip \
    python3-pil \
    python3-gpiozero \
    python3-lgpio

# Install V4L2 utilities for camera testing
echo "[4/6] Installing V4L2 utilities..."
sudo apt-get install -y \
    v4l-utils \
    fswebcam

# Install framebuffer/display tools
echo "[5/6] Installing display tools..."
sudo apt-get install -y \
    fbi \
    libdrm-dev

# Set up the project directory
echo "[6/6] Setting up project directory..."
mkdir -p /home/pi/lapcam
if [ -d "/home/pi/lapcam/.git" ]; then
    echo "Project directory already has git repo"
else
    echo "Copy project files to /home/pi/lapcam/"
fi

# Configure GPU memory split - give more to GPU for video
echo ""
echo "Configuring GPU memory..."
if ! grep -q "gpu_mem=" /boot/firmware/config.txt 2>/dev/null; then
    echo "gpu_mem=128" | sudo tee -a /boot/firmware/config.txt
    echo "Set GPU memory to 128MB"
else
    echo "GPU memory already configured"
fi

# Disable screen blanking
echo "Disabling screen blanking..."
if ! grep -q "consoleblank=0" /boot/firmware/cmdline.txt 2>/dev/null; then
    sudo sed -i 's/$/ consoleblank=0/' /boot/firmware/cmdline.txt
    echo "Screen blanking disabled"
else
    echo "Screen blanking already disabled"
fi

echo ""
echo "========================================"
echo " Setup complete!"
echo "========================================"
echo ""
echo "Next steps:"
echo "  1. Plug in the Endoskill camera"
echo "  2. Run: bash test_camera.sh"
echo "  3. If camera works, run: bash deploy.sh"
echo ""
echo "A reboot is recommended: sudo reboot"
