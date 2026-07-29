#!/bin/bash
# prepare-offline-test.sh - Prepare the Pi for offline first-boot visual overlay test
set -euo pipefail

echo "=== Preparing offline test ==="

# 1. Temporarily corrupt saved SSID to force offline boot state
echo "Temporarily renaming saved SSIDs in wpa_supplicant.conf..."
sed -i 's/ssid="TP-Link_AP_70CA"/ssid="TP-Link_AP_70CA_TEMP"/g' /etc/wpa_supplicant/wpa_supplicant.conf

# 2. Delete sentinel file to restore first-boot state
echo "Deleting first-boot sentinel..."
rm -f /opt/wifi-provision/.first_boot_done

echo "Done! The system is now configured for offline first boot."
echo "Please reboot the Pi now: sudo reboot"
