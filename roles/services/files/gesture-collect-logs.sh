#!/bin/bash
echo "=== Gesture Boot Log $(date) ===" > /var/log/gesture/boot.log
echo "" >> /var/log/gesture/boot.log
echo "-- Engine --" >> /var/log/gesture/boot.log
journalctl -u gesture-engine -n 50 --no-pager >> /var/log/gesture/boot.log 2>&1
echo "" >> /var/log/gesture/boot.log
echo "-- Overlay --" >> /var/log/gesture/boot.log
journalctl -u gesture-overlay -n 50 --no-pager >> /var/log/gesture/boot.log 2>&1
chmod 644 /var/log/gesture/boot.log
