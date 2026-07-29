#!/bin/bash
XRANDR="$(which xrandr)"
OUTPUT="$($XRANDR 2>/dev/null | grep ' connected' | head -1 | awk '{print $1}')"
if [ -z "$OUTPUT" ]; then
  OUTPUT="HDMI-1"
fi

CURRENT="$($XRANDR --query --current 2>/dev/null | grep "^$OUTPUT" | grep -oP '(normal|left|right|inverted)' | head -1)"
case "$CURRENT" in
    normal)   NEW="right" ;;
    right)    NEW="inverted" ;;
    inverted) NEW="left" ;;
    left)     NEW="normal" ;;
    *)        NEW="right" ;;
esac

$XRANDR --output "$OUTPUT" --rotate "$NEW"

# Persist the new rotation as the default for next boot/reboot
CONF_FILE="/etc/X11/xorg.conf.d/99-screenflex-rotation.conf"
if [ -f "$CONF_FILE" ]; then
  cp "$CONF_FILE" /tmp/rotation.conf
  sed -i "s/Option \"Rotate\" \".*\"/Option \"Rotate\" \"$NEW\"/g" /tmp/rotation.conf
  cp /tmp/rotation.conf "$CONF_FILE" 2>/dev/null || true
  rm -f /tmp/rotation.conf
fi

# Dynamically resize active Screenflex player window to fit rotated resolution
if command -v xdotool >/dev/null 2>&1; then
  sleep 0.2
  GEOM=$($XRANDR --current 2>/dev/null | grep "^$OUTPUT" | grep -oE '[0-9]+x[0-9]+\+[0-9]+\+[0-9]+' | head -n 1 || echo "")
  if [[ "${GEOM:-}" =~ ^([0-9]+)x([0-9]+)\+ ]]; then
    W=${BASH_REMATCH[1]}
    H=${BASH_REMATCH[2]}
    WIN=$(xdotool search --name '^Screenflex$' 2>/dev/null | head -1 || true)
    if [ -n "${WIN:-}" ]; then
      xdotool windowmove "$WIN" 0 0 2>/dev/null || true
      xdotool windowsize "$WIN" "$W" "$H" 2>/dev/null || true
      if command -v wmctrl >/dev/null 2>&1; then
        wmctrl -i -r "$WIN" -b add,fullscreen 2>/dev/null || true
      fi
    fi
  fi
fi
