#!/bin/bash
# Camera detection and test script
# Run after setup.sh and plugging in the Endoskill camera

echo "========================================"
echo " Camera Detection Test"
echo "========================================"
echo ""

# Check for video devices
echo "[1] Looking for video devices..."
if ls /dev/video* 1>/dev/null 2>&1; then
    ls -la /dev/video*
else
    echo "ERROR: No video devices found!"
    echo "Make sure the Endoskill camera is plugged in."
    exit 1
fi

echo ""

# Check USB devices for Endoskill (VID:1BCF PID:0B09)
echo "[2] Looking for Endoskill USB device..."
if lsusb | grep -i "1bcf:0b09"; then
    echo "Endoskill camera found on USB bus"
else
    echo "WARNING: Endoskill USB ID (1BCF:0B09) not found"
    echo "Camera may use a different ID on Linux. Showing all USB devices:"
    lsusb
fi

echo ""

# Query camera capabilities
echo "[3] Querying camera capabilities..."
CAMERA_DEV=""
for dev in /dev/video*; do
    if v4l2-ctl --device="$dev" --list-formats-ext 2>/dev/null | grep -q "MJPEG\|Motion-JPEG"; then
        CAMERA_DEV="$dev"
        echo "Found MJPEG-capable camera at: $dev"
        break
    fi
done

if [ -z "$CAMERA_DEV" ]; then
    echo "No MJPEG camera found. Showing all formats on /dev/video0:"
    CAMERA_DEV="/dev/video0"
fi

echo ""
echo "Supported formats:"
v4l2-ctl --device="$CAMERA_DEV" --list-formats-ext

echo ""

# Test GStreamer pipeline (5 second test, no display needed)
echo "[4] Testing GStreamer MJPEG pipeline (5 seconds)..."
echo "    Using device: $CAMERA_DEV"

timeout 5 gst-launch-1.0 \
    v4l2src device="$CAMERA_DEV" num-buffers=50 \
    ! image/jpeg,width=1920,height=1080,framerate=30/1 \
    ! jpegdec \
    ! videoconvert \
    ! fakesink sync=false \
    2>&1

if [ $? -eq 0 ] || [ $? -eq 124 ]; then
    echo ""
    echo "SUCCESS: GStreamer MJPEG pipeline works!"
else
    echo ""
    echo "MJPEG at 1080p failed. Trying 720p..."
    timeout 5 gst-launch-1.0 \
        v4l2src device="$CAMERA_DEV" num-buffers=50 \
        ! image/jpeg,width=1280,height=720,framerate=30/1 \
        ! jpegdec \
        ! videoconvert \
        ! fakesink sync=false \
        2>&1

    if [ $? -eq 0 ] || [ $? -eq 124 ]; then
        echo ""
        echo "SUCCESS: GStreamer MJPEG pipeline works at 720p"
        echo "NOTE: 1080p may not be supported on this device under Linux"
    else
        echo ""
        echo "MJPEG failed. Trying YUY2 at 640x480..."
        timeout 5 gst-launch-1.0 \
            v4l2src device="$CAMERA_DEV" num-buffers=50 \
            ! video/x-raw,format=YUY2,width=640,height=480 \
            ! videoconvert \
            ! fakesink sync=false \
            2>&1
        if [ $? -eq 0 ] || [ $? -eq 124 ]; then
            echo "SUCCESS: YUY2 640x480 works (fallback mode)"
        else
            echo "ERROR: No working pipeline found. Check camera connection."
        fi
    fi
fi

echo ""

# Test display pipeline (shows feed on HDMI for 5 seconds)
echo "[5] Testing HDMI output (5 seconds on screen)..."
echo "    Look at the monitor connected to the Pi!"

timeout 5 gst-launch-1.0 \
    v4l2src device="$CAMERA_DEV" \
    ! image/jpeg,width=1920,height=1080,framerate=30/1 \
    ! jpegdec \
    ! videoconvert \
    ! kmssink sync=false \
    2>&1

if [ $? -eq 0 ] || [ $? -eq 124 ]; then
    echo "SUCCESS: HDMI output works!"
else
    echo "kmssink failed, trying autovideosink..."
    timeout 5 gst-launch-1.0 \
        v4l2src device="$CAMERA_DEV" \
        ! image/jpeg,width=1920,height=1080,framerate=30/1 \
        ! jpegdec \
        ! videoconvert \
        ! autovideosink sync=false \
        2>&1
fi

echo ""
echo "========================================"
echo " Camera test complete"
echo "========================================"
