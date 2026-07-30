# DietPi Kiosk & Gesture Control Signage Provisioning

This repository contains an Ansible-based configuration management framework that automates the installation, configuration, and orchestration of the ScreenFlex Signage Kiosk and MediaPipe Gesture Engine on a clean DietPi OS image.

---

## 1. Architectural Overview

### System Boot & Service Order
1. **Network Bring-up**: Automatically configures and brings up interface metrics (prioritizing wired Ethernet `eth0` over Wi-Fi `wlan0`).
2. **Splash Screen**: Spawns `screenflex-framebuffer-splash.service` to draw the boot image to the frame buffer immediately.
3. **Kiosk Session**: Launches `screenflex-kiosk.service` starting an unprivileged X11 environment (`DISPLAY=:0`) running the Openbox window manager and picom compositor.
4. **Boot Gate & Wi-Fi Provisioning**:
   * **First Boot**: Bypassed automatically if Ethernet is connected (Completion marker `/var/lib/screenflex/wifi-provisioned` is written). Otherwise, launches the interactive Wi-Fi setup overlay.
   * **Later Boots**: Displays the `BootStatusScreen` for 3 seconds detailing connection status (e.g., `"Connected to wired network (eth0)"` or `"Connected to [SSID]"`), then proceeds to the kiosk.
5. **Kiosk Player**: Launches the Screenflex Player (Chromium-based application) configured with SwiftShader CPU-rendering overrides (`--use-gl=swiftshader --disable-gpu`) for graphics driver stability.
6. **Gesture Engine (Hardware-Activated)**:
   * The `gesture-engine.service` is disabled from starting unconditionally at boot.
   * A custom udev rule triggers the service automatically when the USB camera `/dev/video0` is registered.
   * Systemd binds the lifecycle of the service to the camera device (`BindsTo=dev-video0.device`), stopping the daemon immediately if the camera is unplugged and restarting it upon reconnection.
7. **Gesture Overlay**: Renders the MediaPipe-powered Qt hand-tracking visual overlays inside the running X11 session.

---

## 2. Directory Structure

```text
kiosk-os-provisioning/
├── group_vars/
│   └── all.yml                 # Target configuration variables
├── playbooks/
│   └── site.yml                # Main provisioning playbook entrypoint
├── roles/
│   ├── common/                 # Swap space & 24 core system APT packages
│   │   └── tasks/main.yml
│   ├── network/                # Network interfaces & hardware udev configurations
│   │   ├── files/
│   │   │   ├── 99-gesture-camera.rules   # USB camera systemd udev tagger
│   │   │   ├── interfaces                 # eth0 DHCP auto-bringup config
│   │   │   └── *.conf                     # Custom modprobe audio/video disables
│   │   └── tasks/main.yml
│   ├── kiosk/                  # Persistent graphical kiosk configurations
│   │   ├── files/
│   │   │   ├── rc.xml                     # Openbox shortcuts (C-W-w, C-W-o)
│   │   │   ├── picom.conf                 # Compositor configuration
│   │   │   ├── xorg.conf                  # Main Xorg driver setup
│   │   │   └── xorg.conf.d/
│   │   │       └── 99-screenflex-rotation.conf  # Portrait screen rotation (Rotate right)
│   │   ├── handlers/
│   │   │   └── main.yml                   # Handler: reconfigures Openbox on update
│   │   └── tasks/main.yml
│   ├── python_env/             # Python 3.11 virtualenv & dependencies management
│   │   ├── files/
│   │   │   └── requirements.txt           # Pinned python packages
│   │   └── tasks/main.yml
│   └── services/               # Signage wrappers, daemon scripts, and unit drop-ins
│       ├── files/
│       │   ├── gesture_engine.py          # WebSocket landmark server
│       │   ├── overlay.py                 # MediaPipe Qt overlay drawing script
│       │   ├── wifi_provision.py          # Wi-Fi provisioning controller
│       │   ├── screenflex-kiosk           # Kiosk Chromium launcher wrapper
│       │   ├── screenflex-boot-session    # Xinit target X11 initializer script
│       │   ├── screenflex-x-splash        # Graphical portrait boot splash
│       │   ├── *.service                  # Systemd service unit files
│       │   ├── wifi-provision.conf        # Drop-in: orders kiosk after setup
│       │   ├── 10-wait-camera.conf        # Drop-in: binds gesture engine to camera
│       │   └── 10-p1-camera-restart-guard.conf # Drop-in: service restart storm guard
│       ├── handlers/
│       │   └── main.yml                   # Handler: restarts gesture services on update
│       └── tasks/main.yml
└── README.md                   # This instruction file
```

---

## 3. Prerequisites

### Target Machine (Signage Player)
* Raspberry Pi 5 (or compatible ARM64 Pi) running a fresh installation of **DietPi OS**.
* SSH service enabled and reachable over the local network.
* Root access (or user with passwordless sudo).

### Control Machine (Developer PC)
* A Linux/macOS workstation or Windows running WSL.
* **Ansible** installed (`pip install ansible`).
* Network connectivity to the target Pi.

---

## 4. Setup & Deployment

<<<<<<< HEAD
### Step 1: Configure Fleet Inventory
Add your target Raspberry Pis to `inventory.ini`:
```ini
[kiosks]
kiosk-1 ansible_host=172.16.137.113
# kiosk-2 ansible_host=172.16.137.114
# kiosk-3 ansible_host=172.16.137.115

[kiosks:vars]
ansible_user=root
ansible_ssh_pass=flex
ansible_python_interpreter=/usr/bin/python3
```

### Step 2: Establish SSH Access
Ensure you can connect to the target machines without password prompts. Copy your SSH key to all Pis in the fleet:
```bash
ssh-copy-id root@172.16.137.113
```
*(Use the root password `flex` if deploying to a default ScreenFlex image).*

### Step 3: Run Full Provisioning Across the Fleet
Execute the playbook against all inventory hosts:
```bash
ansible-playbook -i inventory.ini playbooks/site.yml
```
*(Or specify a single host directly: `ansible-playbook -i "172.16.137.113," -u root playbooks/site.yml`)*
=======
### Step 1: Configure Target Variables
Open `group_vars/all.yml` and adjust the configuration to match your environment:
```yaml
---
kiosk_user: kiosk
python_version: 3.11
venv_path: /home/.venv
target_ip: 172.16.137.109  # Change this to your Pi's current IP address
```

### Step 2: Establish SSH Access
Ensure you can connect to the target machine without password prompts. Copy your SSH key to the Pi:
```bash
ssh-copy-id root@172.16.137.109
```
*(Use the root password `flex` if deploying to a default ScreenFlex image).*

### Step 3: Run the Provisioning Playbook
Execute the playbook from the root of this folder:
```bash
ansible-playbook -i "172.16.137.109," -u root playbooks/site.yml
```
*(Note: The comma after the IP address is required when passing a single host directly to the `-i` parameter).*
>>>>>>> c259889183fa7e66bde170dcc76207c4d4a99a21

---

## 5. Post-Deployment & Maintenance

### Recovery Keyboard Shortcut
If you need to manually re-configure the Wi-Fi credentials while the kiosk player is active:
* Connect a physical keyboard directly to the Pi.
* Press **Ctrl + Windows + w** (`Ctrl + Super + w`).
* The system will capture the shortcut, interrupt the screen saver, and display the interactive Wi-Fi setup overlay. 

*Note: If accessing the screen remotely via VNC/RDP, your local operating system may intercept the Windows key shortcut. Ensure "Send System Keys" is enabled in your remote control client settings.*

### Checking Service Statuses
Verify critical services are active and running:
```bash
# Check Kiosk Player and Xorg
systemctl status screenflex-kiosk.service

# Check Gesture Detection Engine (Only active when camera is plugged in!)
systemctl status gesture-engine.service

# Check systemd Device Unit representing the camera
systemctl status dev-video0.device
```

### Reading System Logs
```bash
# View Wi-Fi Provisioning log entries
journalctl -u screenflex-kiosk.service --since "1 hour ago" | grep wifi_provision

# View Real-time Gesture Engine outputs
journalctl -u gesture-engine.service -f
```

---

## 6. Modifying & Updating the System

<<<<<<< HEAD
Any changes to code, configurations, or shortcuts should be made locally on your workstation first, then pushed to your Pi fleet using Ansible.

### Fast Python Script Updates Across Fleet
To update Python scripts (`gesture_engine.py`, `overlay.py`, `wifi_provision.py`, etc.) across all Pis at once without re-running full OS provisioning:
```bash
ansible-playbook -i inventory.ini playbooks/site.yml --tags "python"
```
This task:
1. Compares local Python scripts against every Pi in `inventory.ini`.
2. Uploads updated `.py` files in parallel.
3. Automatically triggers handlers to restart dependent services (`gesture-engine.service`) on all updated devices.

### Full Fleet Sync
To push all system, configuration, and script changes to the entire fleet:
```bash
ansible-playbook -i inventory.ini playbooks/site.yml
```

### Automatic Service Reloading & Restarts (Handlers)
Ansible evaluates differences between your local workspace and the Pis. It only uploads files that have changed. It is also configured with **handlers** that automatically reload the updated service components without requiring a full system reboot:
* **Openbox configuration (`rc.xml`) changes** automatically trigger an Openbox reconfigure inside the active GUI session.
* **Python script updates** automatically trigger a restart of `gesture-engine.service` on the target Pis.

=======
Any changes to code, configurations, or shortcuts should be made locally on your workstation first, then pushed to the Pi using Ansible.

### Step 1: Make Local Modifications
* **Update python scripts**: Edit the source files under `roles/services/files/` (e.g., `gesture_engine.py`, `overlay.py`).
* **Modify keyboard shortcuts**: Edit the Openbox configuration at `roles/kiosk/files/rc.xml`.
* **Adjust system configuration**: Edit udev rules or interfaces in `roles/network/files/`.

### Step 2: Push Changes
Run the playbook again:
```bash
ansible-playbook -i "172.16.137.109," -u root playbooks/site.yml
```

### Automatic Service Reloading & Restarts (Handlers)
Ansible evaluates differences between your local workspace and the Pi. It only uploads files that have changed. It is also configured with **handlers** that automatically reload the updated service components without requiring a full system reboot:
* **Openbox configuration (`rc.xml`) changes** automatically trigger an Openbox reconfigure inside the Pi's active GUI session.
* **Python script updates** automatically trigger a restart of `gesture-engine.service` on the Pi.
>>>>>>> c259889183fa7e66bde170dcc76207c4d4a99a21

