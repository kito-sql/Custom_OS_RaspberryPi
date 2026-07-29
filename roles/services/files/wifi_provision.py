#!/usr/bin/env python3
"""Boot-time Wi-Fi gate for the ScreenFlex kiosk.

The persistent kiosk X session runs this program before launching the kiosk.
Boot performs only a brief saved-network readiness check and always releases
the kiosk. Interactive Wi-Fi setup is available on demand through the Openbox
recovery shortcut, including when no saved network exists.
"""

import argparse
import logging
import os
import sys
import tempfile
import time
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from typing import Optional, Tuple

from wifi_connector import check_connectivity, get_connected_ssid, check_any_connectivity
from wifi_overlay import WifiOverlay
from wifi_scanner import detect_interface

logger = logging.getLogger("wifi_provision")


# Completion state is deliberately separate from saved wpa_supplicant profiles.
COMPLETION_MARKER = Path("/var/lib/screenflex/wifi-provisioned")
LEGACY_SENTINEL = Path("/opt/wifi-provision/.first_boot_done")

BOOT_POLL_TIMEOUT = 10
BOOT_POLL_INTERVAL_MS = 1_000
CONNECTED_STATUS_SECONDS = 3
RECOVERY_STATUS_SECONDS = 2

STATUS_BG = "#0f0f1a"
STATUS_PRIMARY = "#e8e8f0"
STATUS_SECONDARY = "#8888a0"
STATUS_SUCCESS = "#00c853"
STATUS_WARNING = "#ffab40"


def is_provisioned() -> bool:
    """Return whether interactive provisioning has completed successfully."""
    return COMPLETION_MARKER.is_file()


def write_completion_marker() -> None:
    """Atomically record successful interactive provisioning with restrictive mode."""
    COMPLETION_MARKER.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        prefix=f".{COMPLETION_MARKER.name}.", dir=COMPLETION_MARKER.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as marker_file:
            marker_file.write(f"provisioned_at={int(time.time())}\n")
            marker_file.flush()
            os.fsync(marker_file.fileno())
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, COMPLETION_MARKER)
        os.chmod(COMPLETION_MARKER, 0o600)
        logger.info("wrote Wi-Fi provisioning completion marker: %s", COMPLETION_MARKER)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


class BootStatusScreen:
    """Fullscreen status display used by the later-boot gate before kiosk X11."""

    def __init__(self, iface: str, timeout: int):
        self.iface = iface
        self.timeout = timeout
        self.root: Optional[tk.Tk] = None
        self.canvas: Optional[tk.Canvas] = None
        self.width = 0
        self.height = 0
        self.result: Tuple[bool, Optional[str]] = (False, None)
        self._deadline = 0.0
        self._title_font = None
        self._body_font = None

    def wait_for_connectivity(self) -> Tuple[bool, Optional[str]]:
        """Show connection progress until verified or the recovery timeout expires."""
        self.root = tk.Tk()
        # Do not map Tk's default small window during the boot splash transition.
        self.root.withdraw()
        self.root.title("Wi-Fi status")
        self.root.configure(bg=STATUS_BG)

        self.width = self.root.winfo_screenwidth()
        self.height = self.root.winfo_screenheight()
        self.root.geometry(f"{self.width}x{self.height}+0+0")
        self.root.attributes("-fullscreen", True)
        self.root.attributes("-topmost", True)
        self.root.overrideredirect(True)

        self.canvas = tk.Canvas(
            self.root, bg=STATUS_BG, highlightthickness=0, cursor="none"
        )
        self.canvas.pack(fill=tk.BOTH, expand=True)
        scale = max(min(self.width, self.height) / 1080, 0.6)
        self._title_font = tkfont.Font(
            family="Helvetica", size=int(32 * scale), weight="bold"
        )
        self._body_font = tkfont.Font(family="Helvetica", size=int(18 * scale))

        self._deadline = time.monotonic() + self.timeout
        self._render("Connecting to saved Wi-Fi", "Checking network readiness…", STATUS_SECONDARY)
        self.root.deiconify()
        self.root.update_idletasks()
        self.root.focus_force()
        self.root.after(0, self._poll)
        self.root.mainloop()
        self.root.destroy()
        self.root = None
        return self.result

    def _poll(self) -> None:
        if not self.root:
            return

        connected, ip, conn_iface = check_any_connectivity()
        if connected:
            if conn_iface.startswith("wlan"):
                ssid = get_connected_ssid(conn_iface)
                target = f"Connected to {ssid}" if ssid else "Wi-Fi connected"
            else:
                target = f"Connected to wired network ({conn_iface})"
            detail = ip or "Network ready"
            self.result = (True, ip)
            self._render(target, detail, STATUS_SUCCESS)
            self.root.after(CONNECTED_STATUS_SECONDS * 1000, self.root.quit)
            return

        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            self._render(
                "Saved Wi-Fi is unavailable",
                "Starting kiosk — Wi-Fi setup is available from the recovery shortcut",
                STATUS_WARNING,
            )
            self.root.after(0, self.root.quit)
            return

        self._render(
            "Connecting to saved Wi-Fi",
            f"Checking network readiness… {int(remaining) + 1}s remaining",
            STATUS_SECONDARY,
        )
        self.root.after(BOOT_POLL_INTERVAL_MS, self._poll)

    def _render(self, title: str, detail: str, color: str) -> None:
        if not self.canvas:
            return
        self.canvas.delete("all")
        center_x = self.width // 2
        center_y = self.height // 2
        self.canvas.create_text(
            center_x, center_y - 35, text=title, fill=STATUS_PRIMARY,
            font=self._title_font, anchor="center",
        )
        self.canvas.create_text(
            center_x, center_y + 35, text=detail, fill=color,
            font=self._body_font, anchor="center",
        )
        self.canvas.create_rectangle(
            center_x - 36, center_y + 95, center_x + 36, center_y + 101,
            fill=color, outline="",
        )


def run_interactive_setup(iface: str) -> bool:
    """Run setup and persist the marker only after a verified successful connection."""
    logger.info("starting interactive Wi-Fi setup on %s", iface)
    overlay = WifiOverlay(iface=iface)
    overlay.run()
    if not overlay.connected_ip:
        logger.warning("Wi-Fi setup closed without a verified connection")
        return False

    write_completion_marker()
    logger.info("Wi-Fi setup completed: connected to %s (%s)", overlay.connected_ssid, overlay.connected_ip)
    return True


def run_boot_flow(iface: str, poll_timeout: int) -> int:
    """Apply first-boot or later-boot behavior and return a process status code."""
    # Check if we already have interface-agnostic connectivity (e.g., Ethernet)
    connected, ip, conn_iface = check_any_connectivity()
    # ONLY skip the boot flow immediately if we are on FIRST BOOT and have Ethernet/Wired connected!
    if connected and not is_provisioned():
        logger.info("Active connectivity already present on %s (%s) on first boot. Skipping setup.", conn_iface, ip)
        write_completion_marker()
        return 0

    if not is_provisioned():
        if LEGACY_SENTINEL.exists():
            logger.warning(
                "legacy sentinel exists but is ignored; interactive provisioning is required"
            )
        logger.info("no completion marker: starting first-boot Wi-Fi setup")
        return 0 if run_interactive_setup(iface) else 1

    logger.info("completion marker found: showing saved-network status")
    # Note: BootStatusScreen wait_for_connectivity will poll check_any_connectivity internally
    connected, ip = BootStatusScreen(iface, poll_timeout).wait_for_connectivity()
    if connected:
        logger.info("saved Wi-Fi verified on %s (%s)", iface, ip)
        return 0

    logger.warning("saved Wi-Fi unavailable: entering recovery setup")
    return 0 if run_interactive_setup(iface) else 1


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="ScreenFlex Wi-Fi provisioning boot gate")
    parser.add_argument("-v", "--verbose", action="store_true", help="enable debug logging")
    parser.add_argument(
        "--skip-poll", action="store_true",
        help="open interactive setup immediately (used by the Openbox recovery shortcut)",
    )
    parser.add_argument("--iface", help="wireless interface (auto-detect if omitted)")
    parser.add_argument(
        "--poll-timeout", type=int, default=BOOT_POLL_TIMEOUT,
        help=f"saved-network status timeout in seconds (default: {BOOT_POLL_TIMEOUT})",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    iface = args.iface or detect_interface()
    if not iface:
        logger.error("no wireless interface found — Wi-Fi setup cannot start")
        return 1
    logger.info("using wireless interface: %s", iface)

    if args.skip_poll:
        return 0 if run_interactive_setup(iface) else 1
    return run_boot_flow(iface, args.poll_timeout)


if __name__ == "__main__":
    sys.exit(main())
