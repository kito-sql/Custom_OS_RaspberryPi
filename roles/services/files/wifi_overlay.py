#!/usr/bin/env python3
"""
wifi_overlay.py - Fullscreen X11/Tkinter Wi-Fi setup overlay

Renders on top of the Screenflex kiosk via an always-on-top, fullscreen
Tkinter window with an active X11 keyboard grab.  All keystrokes route
exclusively to this overlay while it's visible — arrow keys, Enter, Tab,
etc. do not leak through to Chromium underneath.

Display technology: Tkinter on X11 (DISPLAY=:0)
Input: USB keyboard only (no mouse/touch required)

Overlay state machine:
    SSID_LIST -> PASSWORD_ENTRY -> CONNECTING -> SUCCESS_DISPLAY -> HIDE
        ^              |                |
        +----Esc-------+                |
        ^                               |
        +---failure (retries <= 3)------+
        ^
        +---failure (retries > 3)--- return to SSID_LIST

Features:
  - Signal strength bars (█░ blocks)
  - Security type and band badges
  - Password masking with Tab toggle
  - Auto-refresh every 10s (paused during password entry / connecting)
  - 1-minute inactivity timeout with visible countdown
  - 3 password retries per SSID before returning to list
  - IP address display after successful connection (3 sec)
  - Active X11 keyboard grab (grab_set_global)
"""

import logging
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from typing import List, Optional

from wifi_scanner import scan_networks, WifiNetwork, WifiScanError
from wifi_connector import connect, ConnectionResult

logger = logging.getLogger("wifi_overlay")
logger.addHandler(logging.NullHandler())

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Colors — dark theme, premium feel
BG_COLOR = "#0f0f1a"            # deep navy/black background
PANEL_BG = "#1a1a2e"            # slightly lighter panel
ACCENT = "#6c63ff"              # purple accent
ACCENT_HOVER = "#7f78ff"        # lighter purple
SUCCESS_COLOR = "#00c853"       # green for success
ERROR_COLOR = "#ff5252"         # red for errors
WARNING_COLOR = "#ffab40"       # amber for warnings
TEXT_PRIMARY = "#e8e8f0"        # near-white text
TEXT_SECONDARY = "#8888a0"      # muted text
TEXT_DIM = "#555570"            # very dim text
SELECTED_BG = "#2a2a4a"        # selected row highlight
INPUT_BG = "#12122a"           # input field background
INPUT_BORDER = "#3a3a5a"       # input field border
SIGNAL_HIGH = "#00e676"        # strong signal (green)
SIGNAL_MED = "#ffab40"         # medium signal (amber)
SIGNAL_LOW = "#ff5252"         # weak signal (red)

# Timing
AUTO_REFRESH_INTERVAL_MS = 10_000   # 10 seconds
SESSION_TIMEOUT_SECONDS = 60        # 1 minute
SUCCESS_DISPLAY_SECONDS = 3         # show IP for 3 seconds
MAX_RETRIES_PER_SSID = 3

# Signal bar rendering
SIGNAL_BLOCKS = 5               # number of bar segments


# ---------------------------------------------------------------------------
# Overlay states
# ---------------------------------------------------------------------------

class OverlayState:
    SSID_LIST = "ssid_list"
    PASSWORD_ENTRY = "password_entry"
    CONNECTING = "connecting"
    SUCCESS_DISPLAY = "success_display"
    ERROR_DISPLAY = "error_display"


# ---------------------------------------------------------------------------
# WifiOverlay — main class
# ---------------------------------------------------------------------------

class WifiOverlay:
    """
    Fullscreen X11/Tkinter Wi-Fi setup overlay.

    Lifecycle:
      1. Instantiate with interface name
      2. Call run() — blocks until connection succeeds or user closes
      3. On success, connected_ip is set and the overlay hides itself
    """

    def __init__(self, iface: str):
        self.iface = iface
        self.connected_ip: Optional[str] = None
        self.connected_ssid: Optional[str] = None

        # State
        self.state = OverlayState.SSID_LIST
        self.networks: List[WifiNetwork] = []
        self.selected_index = 0
        self.scroll_offset = 0
        self.password = ""
        self.password_visible = False
        self.retry_count = 0
        self.status_message = ""
        self.status_color = TEXT_SECONDARY
        self.last_keypress_time = time.time()

        # Tkinter root — created in run()
        self.root: Optional[tk.Tk] = None
        self.canvas: Optional[tk.Canvas] = None

        # Dimensions (set after window maps)
        self.width = 0
        self.height = 0

        # Fonts (created after root exists)
        self.font_title = None
        self.font_normal = None
        self.font_small = None
        self.font_mono = None
        self.font_large = None
        self.font_icon = None

        # Auto-refresh timer ID
        self._refresh_timer_id = None
        # Session timeout timer ID
        self._timeout_timer_id = None
        # Countdown display timer
        self._countdown_timer_id = None

        # Max visible SSIDs in the list
        self.max_visible_ssids = 8

    # -------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------

    def run(self):
        """
        Launch the overlay.  Blocks until the user connects or the window
        is destroyed.  After returning, check self.connected_ip.
        """
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("Wi-Fi Setup")
        self.root.configure(bg=BG_COLOR)

        # Get screen size and set geometry explicitly to prevent overrideredirect scaling issues
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        self.root.geometry(f"{screen_width}x{screen_height}+0+0")

        # Fullscreen + always on top
        self.root.attributes("-fullscreen", True)
        self.root.attributes("-topmost", True)

        # Remove window decorations (explicitly overriding window manager constraints)
        self.root.overrideredirect(True)

        # Canvas for all rendering
        self.canvas = tk.Canvas(
            self.root, bg=BG_COLOR,
            highlightthickness=0, cursor="none"
        )
        self.canvas.pack(fill=tk.BOTH, expand=True)

        # Flush geometry requests while the window is still withdrawn.
        self.root.update_idletasks()
        self.width = self.root.winfo_screenwidth()
        self.height = self.root.winfo_screenheight()

        # Create fonts
        self._create_fonts()

        # Bind all key events
        self.root.bind("<Key>", self._on_key)

        # Initial scan
        self._do_scan()

        # Start auto-refresh
        self._schedule_auto_refresh()

        # Start session timeout
        self._reset_session_timeout()

        # Initial render
        self._render()

        # Map only the fully configured and rendered window. This prevents a
        # partially initialized Tk window from appearing before the kiosk
        # splash/boot transition has completed.
        self.root.deiconify()
        self.root.update_idletasks()

        # Focus and keyboard grab must happen after the window is mapped.
        self.root.focus_force()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self._schedule_focus_enforcement()
        self._acquire_grab()

        # Main loop
        self.root.mainloop()

    # -------------------------------------------------------------------
    # Font creation
    # -------------------------------------------------------------------

    def _create_fonts(self):
        """Create scaled fonts based on screen resolution."""
        # Base size scaling: use min(width, height) to ensure fonts scale to the narrowest dimension
        scale = max(min(self.width, self.height) / 1080, 0.6)

        self.font_title = tkfont.Font(family="Helvetica", size=int(28 * scale), weight="bold")
        self.font_large = tkfont.Font(family="Helvetica", size=int(20 * scale), weight="bold")
        self.font_normal = tkfont.Font(family="Helvetica", size=int(16 * scale))
        self.font_small = tkfont.Font(family="Helvetica", size=int(12 * scale))
        self.font_mono = tkfont.Font(family="Courier", size=int(16 * scale))
        self.font_icon = tkfont.Font(family="Helvetica", size=int(14 * scale))

    # -------------------------------------------------------------------
    # Keyboard grab
    # -------------------------------------------------------------------

    def _acquire_grab(self, retries: int = 10):
        """
        Acquire an X11 keyboard grab so keystrokes don't leak to Chromium.
        Retries a few times if another client holds the grab.
        """
        # Sleep briefly initially to let Openbox release its shortcut grab
        time.sleep(0.5)
        for attempt in range(retries):
            try:
                self.root.grab_set_global()
                logger.info("X11 keyboard grab acquired (attempt %d)", attempt + 1)
                return True
            except tk.TclError as e:
                logger.warning("grab_set_global failed (attempt %d): %s", attempt + 1, e)
                time.sleep(0.5)  # wait before retry
                self.root.update()
        logger.warning("keyboard grab failed after %d attempts — proceeding without grab", retries)
        return False

    def _release_grab(self):
        """Release the keyboard grab."""
        try:
            self.root.grab_release()
            logger.info("X11 keyboard grab released")
        except tk.TclError:
            pass

    def _schedule_focus_enforcement(self):
        """Schedule periodic focus and lift to keep the overlay topmost."""
        if self.root:
            self._enforce_focus()
            self._focus_enforcements = 0
            self._enforce_focus_loop()

    def _enforce_focus_loop(self):
        if self.root and self._focus_enforcements < 6:
            self._enforce_focus()
            self._focus_enforcements += 1
            self.root.after(500, self._enforce_focus_loop)

    def _enforce_focus(self):
        if self.root:
            try:
                self.root.lift()
                self.root.focus_force()
                self.root.attributes("-topmost", True)
            except tk.TclError:
                pass

    # -------------------------------------------------------------------
    # Key handling
    # -------------------------------------------------------------------

    def _on_key(self, event):
        """Route key events based on current overlay state."""
        self.last_keypress_time = time.time()
        self._reset_session_timeout()

        if self.state == OverlayState.SSID_LIST:
            self._handle_ssid_list_key(event)
        elif self.state == OverlayState.PASSWORD_ENTRY:
            self._handle_password_key(event)
        elif self.state == OverlayState.ERROR_DISPLAY:
            self._handle_error_key(event)
        elif self.state == OverlayState.SUCCESS_DISPLAY:
            pass  # no interaction during success countdown
        elif self.state == OverlayState.CONNECTING:
            pass  # no interaction during connection attempt

    def _handle_ssid_list_key(self, event):
        if event.keysym == "Up":
            if self.selected_index > 0:
                self.selected_index -= 1
                self._adjust_scroll()
                self._render()
        elif event.keysym == "Down":
            if self.selected_index < len(self.networks) - 1:
                self.selected_index += 1
                self._adjust_scroll()
                self._render()
        elif event.keysym == "Return":
            if self.networks:
                net = self.networks[self.selected_index]
                if net.security == "Open":
                    # Open network: connect directly, no password needed
                    self._start_connection(net.ssid, "", net.security)
                else:
                    self.state = OverlayState.PASSWORD_ENTRY
                    self.password = ""
                    self.password_visible = False
                    self.retry_count = 0
                    self._cancel_auto_refresh()  # pause refresh
                    self._render()
        elif event.keysym.lower() == "r":
            self._do_scan()
            self._render()
        elif event.keysym == "Escape":
            logger.info("Escape pressed on network list — closing overlay")
            self._hide()

    def _handle_password_key(self, event):
        if event.keysym == "Escape":
            self.state = OverlayState.SSID_LIST
            self.password = ""
            self._schedule_auto_refresh()  # resume refresh
            self._render()
        elif event.keysym == "Return":
            if self.password or self.networks[self.selected_index].security == "Open":
                net = self.networks[self.selected_index]
                self._start_connection(net.ssid, self.password, net.security)
        elif event.keysym == "Tab":
            self.password_visible = not self.password_visible
            self._render()
        elif event.keysym == "BackSpace":
            self.password = self.password[:-1]
            self._render()
        elif event.char and event.char.isprintable() and len(event.char) == 1:
            self.password += event.char
            self._render()

    def _handle_error_key(self, event):
        """From error display, any key returns to the appropriate state."""
        if event.keysym == "Escape" or self.retry_count >= MAX_RETRIES_PER_SSID:
            # Back to SSID list
            self.state = OverlayState.SSID_LIST
            self.retry_count = 0
            self._schedule_auto_refresh()
            self._render()
        elif event.keysym == "Return" or event.char:
            # Retry: back to password entry
            self.state = OverlayState.PASSWORD_ENTRY
            self.password = ""
            self._render()

    # -------------------------------------------------------------------
    # Scroll management
    # -------------------------------------------------------------------

    def _adjust_scroll(self):
        """Keep the selected index visible in the scroll window."""
        if self.selected_index < self.scroll_offset:
            self.scroll_offset = self.selected_index
        elif self.selected_index >= self.scroll_offset + self.max_visible_ssids:
            self.scroll_offset = self.selected_index - self.max_visible_ssids + 1

    # -------------------------------------------------------------------
    # Connection (runs in a thread to keep UI responsive)
    # -------------------------------------------------------------------

    def _start_connection(self, ssid: str, password: str, security: str):
        """Transition to CONNECTING state and attempt connection in a thread."""
        self.state = OverlayState.CONNECTING
        self.status_message = f"Connecting to {ssid}..."
        self.status_color = TEXT_SECONDARY
        self._cancel_auto_refresh()
        self._render()

        # Run connection in a background thread so the UI stays responsive
        thread = threading.Thread(
            target=self._connection_thread,
            args=(ssid, password, security),
            daemon=True,
        )
        thread.start()

    def _connection_thread(self, ssid: str, password: str, security: str):
        """Background thread: attempt connection, then schedule UI update."""
        try:
            result = connect(
                iface=self.iface,
                ssid=ssid,
                password=password,
                security=security,
            )
        except Exception as e:
            logger.exception("connect() raised unexpectedly")
            result = ConnectionResult(
                success=False,
                error=f"Internal error: {e}",
                error_type="command_error",
            )

        # Schedule UI update on the main thread
        if self.root:
            self.root.after(0, lambda: self._on_connection_result(result, ssid))

    def _on_connection_result(self, result: ConnectionResult, ssid: str):
        """Handle connection result on the main thread."""
        if result.success:
            self.connected_ip = result.ip_address
            self.connected_ssid = ssid
            self.state = OverlayState.SUCCESS_DISPLAY
            self.status_message = f"Connected to {ssid}"
            self.status_color = SUCCESS_COLOR
            self._render()
            # Auto-hide after SUCCESS_DISPLAY_SECONDS
            self.root.after(SUCCESS_DISPLAY_SECONDS * 1000, self._hide)
        else:
            self.retry_count += 1
            self.state = OverlayState.ERROR_DISPLAY
            self.status_message = result.error or "Connection failed"
            self.status_color = ERROR_COLOR

            if self.retry_count >= MAX_RETRIES_PER_SSID:
                self.status_message += "\n\nMax retries reached — press any key to return to network list"
            else:
                retries_left = MAX_RETRIES_PER_SSID - self.retry_count
                self.status_message += f"\n\nPress Enter to retry ({retries_left} left) or Esc for network list"

            self._render()

    # -------------------------------------------------------------------
    # Scanning
    # -------------------------------------------------------------------

    def _do_scan(self):
        """Run a network scan and update the list."""
        try:
            self.networks = scan_networks(iface=self.iface, dedup=True)
            logger.info("scan found %d networks", len(self.networks))
        except WifiScanError as e:
            logger.warning("scan failed: %s", e)
            self.networks = []
            self.status_message = f"Scan failed: {e}"
            self.status_color = WARNING_COLOR

        # Clamp selection
        if self.selected_index >= len(self.networks):
            self.selected_index = max(0, len(self.networks) - 1)
        self._adjust_scroll()

    def _schedule_auto_refresh(self):
        """Schedule the next auto-refresh (only in SSID_LIST state)."""
        self._cancel_auto_refresh()
        if self.state == OverlayState.SSID_LIST and self.root:
            self._refresh_timer_id = self.root.after(
                AUTO_REFRESH_INTERVAL_MS, self._auto_refresh_tick
            )

    def _cancel_auto_refresh(self):
        if self._refresh_timer_id is not None and self.root:
            self.root.after_cancel(self._refresh_timer_id)
            self._refresh_timer_id = None

    def _auto_refresh_tick(self):
        """Auto-refresh callback — only fires in SSID_LIST state."""
        if self.state == OverlayState.SSID_LIST:
            # Preserve current selection by SSID name
            prev_ssid = None
            if self.networks and self.selected_index < len(self.networks):
                prev_ssid = self.networks[self.selected_index].ssid

            self._do_scan()

            # Restore selection if the same SSID still exists
            if prev_ssid:
                for i, net in enumerate(self.networks):
                    if net.ssid == prev_ssid:
                        self.selected_index = i
                        self._adjust_scroll()
                        break

            self._render()
            self._schedule_auto_refresh()

    # -------------------------------------------------------------------
    # Session timeout
    # -------------------------------------------------------------------

    def _reset_session_timeout(self):
        """Reset the 1-minute inactivity timeout."""
        if self._timeout_timer_id is not None and self.root:
            self.root.after_cancel(self._timeout_timer_id)
        if self._countdown_timer_id is not None and self.root:
            self.root.after_cancel(self._countdown_timer_id)

        self.last_keypress_time = time.time()
        self._schedule_countdown_update()

    def _schedule_countdown_update(self):
        """Update the countdown display every second."""
        if self.root:
            self._countdown_timer_id = self.root.after(1000, self._countdown_tick)

    def _countdown_tick(self):
        """Check if session has timed out, update countdown display."""
        elapsed = time.time() - self.last_keypress_time
        remaining = SESSION_TIMEOUT_SECONDS - elapsed

        if remaining <= 0:
            logger.info("Session inactivity timeout reached (0:00). Closing Wi-Fi overlay.")
            self._hide()
            return

        # Re-render to update the countdown in the UI smoothly every second
        self._render()

        self._schedule_countdown_update()

    # -------------------------------------------------------------------
    # Hide / destroy
    # -------------------------------------------------------------------

    def _hide(self):
        """Release grab and destroy the overlay window."""
        self._cancel_auto_refresh()
        if self._timeout_timer_id is not None:
            self.root.after_cancel(self._timeout_timer_id)
        if self._countdown_timer_id is not None:
            self.root.after_cancel(self._countdown_timer_id)
        self._release_grab()
        if self.root:
            self.root.destroy()
            self.root = None

    # -------------------------------------------------------------------
    # Rendering
    # -------------------------------------------------------------------

    def _render(self):
        """Clear and redraw the entire overlay based on current state."""
        if not self.canvas:
            return

        self.canvas.delete("all")

        if self.state == OverlayState.SUCCESS_DISPLAY:
            self._render_success()
        elif self.state == OverlayState.CONNECTING:
            self._render_connecting()
        else:
            self._render_main_panel()

    def _render_main_panel(self):
        """Render the main panel with SSID list, password entry, or error."""
        cx = self.width // 2
        cy = self.height // 2

        # Panel dimensions (centered, ~60% of screen width)
        pw = int(self.width * 0.6)
        ph = int(self.height * 0.8)
        px1 = cx - pw // 2
        py1 = cy - ph // 2
        px2 = cx + pw // 2
        py2 = cy + ph // 2

        # Semi-transparent overlay effect (dim background)
        self.canvas.create_rectangle(
            0, 0, self.width, self.height,
            fill=BG_COLOR, outline=""
        )

        # Main panel
        self._draw_rounded_rect(px1, py1, px2, py2, radius=20, fill=PANEL_BG)

        # Title
        title_y = py1 + 40
        self.canvas.create_text(
            cx, title_y, text="Wi-Fi Setup",
            font=self.font_title, fill=TEXT_PRIMARY, anchor="n"
        )

        # Session timeout countdown
        elapsed = time.time() - self.last_keypress_time
        remaining = max(0, SESSION_TIMEOUT_SECONDS - elapsed)
        mins = int(remaining) // 60
        secs = int(remaining) % 60
        timeout_text = f"Session: {mins}:{secs:02d}"
        self.canvas.create_text(
            px2 - 20, py1 + 20, text=timeout_text,
            font=self.font_small, fill=TEXT_DIM, anchor="ne"
        )

        # Content area starts below title
        content_y = title_y + 60
        content_x = px1 + 30
        content_w = pw - 60

        if self.state == OverlayState.SSID_LIST:
            self._render_ssid_list(content_x, content_y, content_w, py2 - 80)
        elif self.state == OverlayState.PASSWORD_ENTRY:
            self._render_password_entry(content_x, content_y, content_w, py2 - 80)
        elif self.state == OverlayState.ERROR_DISPLAY:
            self._render_error(content_x, content_y, content_w, py2 - 80)

        # Status bar at bottom
        if self.status_message and self.state in (OverlayState.SSID_LIST,):
            self.canvas.create_text(
                cx, py2 - 50, text=self.status_message,
                font=self.font_small, fill=self.status_color, anchor="n",
                width=content_w
            )

        # Key hints at bottom
        self._render_key_hints(cx, py2 - 20)

    def _render_ssid_list(self, x, y, w, max_y):
        """Render the scrollable SSID list."""
        # Header
        self.canvas.create_text(
            x, y, text="Available Networks",
            font=self.font_normal, fill=TEXT_PRIMARY, anchor="nw"
        )
        self.canvas.create_text(
            x + w, y, text=f"[R] Refresh  ({len(self.networks)} found)",
            font=self.font_small, fill=TEXT_SECONDARY, anchor="ne"
        )

        y += 35

        if not self.networks:
            self.canvas.create_text(
                x + w // 2, y + 40,
                text="No networks found\nPress R to scan again",
                font=self.font_normal, fill=TEXT_DIM, anchor="n",
                justify="center"
            )
            return

        # List items
        row_height = 44
        visible_end = min(
            self.scroll_offset + self.max_visible_ssids,
            len(self.networks)
        )

        # Scroll indicators
        if self.scroll_offset > 0:
            self.canvas.create_text(
                x + w // 2, y - 5, text="▲ more",
                font=self.font_small, fill=TEXT_DIM, anchor="s"
            )

        for i in range(self.scroll_offset, visible_end):
            net = self.networks[i]
            ry = y + (i - self.scroll_offset) * row_height
            is_selected = (i == self.selected_index)

            # Row background
            if is_selected:
                self._draw_rounded_rect(
                    x - 5, ry - 2, x + w + 5, ry + row_height - 6,
                    radius=8, fill=SELECTED_BG
                )
                # Selection indicator
                self.canvas.create_text(
                    x + 5, ry + (row_height - 4) // 2,
                    text="▸", font=self.font_normal, fill=ACCENT, anchor="w"
                )

            # SSID name
            ssid_display = net.ssid[:28] + "…" if len(net.ssid) > 28 else net.ssid
            name_x = x + 25
            self.canvas.create_text(
                name_x, ry + (row_height - 4) // 2,
                text=ssid_display,
                font=self.font_normal,
                fill=TEXT_PRIMARY if is_selected else TEXT_SECONDARY,
                anchor="w"
            )

            # Signal bars
            bars_x = x + w - 200
            self._render_signal_bars(
                bars_x, ry + 6, net.signal_quality
            )

            # Security badge
            sec_x = x + w - 130
            sec_color = SUCCESS_COLOR if net.security in ("WPA2", "WPA3") else (
                WARNING_COLOR if net.security in ("WPA", "WPA/WPA2") else (
                    ERROR_COLOR if net.security == "WEP" else TEXT_DIM
                )
            )
            self.canvas.create_text(
                sec_x, ry + (row_height - 4) // 2,
                text=net.security, font=self.font_small,
                fill=sec_color, anchor="w"
            )

            # Band
            band_x = x + w - 45
            self.canvas.create_text(
                band_x, ry + (row_height - 4) // 2,
                text=net.band.replace("GHz", "G"),
                font=self.font_small, fill=TEXT_DIM, anchor="w"
            )

        if visible_end < len(self.networks):
            bottom_y = y + self.max_visible_ssids * row_height
            self.canvas.create_text(
                x + w // 2, bottom_y + 5, text="▼ more",
                font=self.font_small, fill=TEXT_DIM, anchor="n"
            )

    def _render_signal_bars(self, x, y, quality):
        """Render signal strength as filled/empty blocks."""
        filled = max(1, round(quality / 100 * SIGNAL_BLOCKS))
        bar_w = 8
        bar_gap = 3
        max_h = 22

        for i in range(SIGNAL_BLOCKS):
            bh = int(max_h * (i + 1) / SIGNAL_BLOCKS)
            bx = x + i * (bar_w + bar_gap)
            by = y + (max_h - bh)

            if i < filled:
                # Filled bar — color based on quality
                if quality >= 60:
                    color = SIGNAL_HIGH
                elif quality >= 30:
                    color = SIGNAL_MED
                else:
                    color = SIGNAL_LOW
            else:
                color = TEXT_DIM

            self.canvas.create_rectangle(
                bx, by, bx + bar_w, y + max_h,
                fill=color, outline=""
            )

    def _render_password_entry(self, x, y, w, max_y):
        """Render the password entry screen."""
        net = self.networks[self.selected_index]

        # Network name
        self.canvas.create_text(
            x, y, text=f"Connect to: {net.ssid}",
            font=self.font_large, fill=TEXT_PRIMARY, anchor="nw"
        )
        y += 40

        # Security info
        self.canvas.create_text(
            x, y, text=f"Security: {net.security}  |  Signal: {net.signal_quality}%  |  {net.band}",
            font=self.font_small, fill=TEXT_SECONDARY, anchor="nw"
        )
        y += 40

        # Password label
        self.canvas.create_text(
            x, y, text="Password:",
            font=self.font_normal, fill=TEXT_PRIMARY, anchor="nw"
        )
        y += 30

        # Password input field
        field_h = 44
        self._draw_rounded_rect(
            x, y, x + w, y + field_h,
            radius=8, fill=INPUT_BG, outline=ACCENT
        )

        # Password text (masked or visible)
        if self.password_visible:
            display_pass = self.password
        else:
            display_pass = "•" * len(self.password)

        # Add cursor
        display_pass += "│"

        self.canvas.create_text(
            x + 15, y + field_h // 2,
            text=display_pass,
            font=self.font_mono, fill=TEXT_PRIMARY, anchor="w"
        )

        # Visibility toggle hint
        vis_text = "Tab: Show password" if not self.password_visible else "Tab: Hide password"
        self.canvas.create_text(
            x + w, y + field_h + 10,
            text=vis_text,
            font=self.font_small, fill=TEXT_DIM, anchor="ne"
        )

        y += field_h + 40

        # Retry counter (if retrying)
        if self.retry_count > 0:
            retries_left = MAX_RETRIES_PER_SSID - self.retry_count
            self.canvas.create_text(
                x, y,
                text=f"Attempt {self.retry_count + 1} of {MAX_RETRIES_PER_SSID}",
                font=self.font_small, fill=WARNING_COLOR, anchor="nw"
            )

    def _render_connecting(self):
        """Render the connecting state (centered spinner message)."""
        cx = self.width // 2
        cy = self.height // 2

        self.canvas.create_rectangle(
            0, 0, self.width, self.height,
            fill=BG_COLOR, outline=""
        )

        # Connecting animation — pulsing dots
        dots = "." * (int(time.time() * 2) % 4)
        self.canvas.create_text(
            cx, cy - 20,
            text=f"Connecting{dots}",
            font=self.font_large, fill=ACCENT, anchor="center"
        )

        net = self.networks[self.selected_index]
        self.canvas.create_text(
            cx, cy + 30,
            text=net.ssid,
            font=self.font_normal, fill=TEXT_SECONDARY, anchor="center"
        )

        # Animate the dots
        if self.state == OverlayState.CONNECTING and self.root:
            self.root.after(500, self._render)

    def _render_success(self):
        """Render the success screen with IP address."""
        cx = self.width // 2
        cy = self.height // 2

        self.canvas.create_rectangle(
            0, 0, self.width, self.height,
            fill=BG_COLOR, outline=""
        )

        # Success checkmark
        self.canvas.create_text(
            cx, cy - 80,
            text="✓",
            font=tkfont.Font(family="Helvetica", size=60, weight="bold"),
            fill=SUCCESS_COLOR, anchor="center"
        )

        # Connected message
        self.canvas.create_text(
            cx, cy - 10,
            text=f"Connected to {self.connected_ssid}",
            font=self.font_large, fill=TEXT_PRIMARY, anchor="center"
        )

        # IP address
        self.canvas.create_text(
            cx, cy + 40,
            text=f"IP Address: {self.connected_ip}",
            font=self.font_normal, fill=SUCCESS_COLOR, anchor="center"
        )

        # Countdown
        self.canvas.create_text(
            cx, cy + 90,
            text=f"Continuing in {SUCCESS_DISPLAY_SECONDS} seconds...",
            font=self.font_small, fill=TEXT_DIM, anchor="center"
        )

    def _render_error(self, x, y, w, max_y):
        """Render the error display."""
        # Error icon
        cx = x + w // 2
        self.canvas.create_text(
            cx, y + 20,
            text="✗",
            font=tkfont.Font(family="Helvetica", size=48, weight="bold"),
            fill=ERROR_COLOR, anchor="center"
        )

        # Error message (may be multi-line)
        self.canvas.create_text(
            cx, y + 90,
            text=self.status_message,
            font=self.font_normal, fill=TEXT_PRIMARY, anchor="n",
            justify="center", width=w - 40
        )

    def _render_key_hints(self, cx, y):
        """Render context-sensitive keyboard shortcut hints."""
        if self.state == OverlayState.SSID_LIST:
            hints = "[↑↓] Navigate    [Enter] Select    [R] Refresh    [Esc] Close"
        elif self.state == OverlayState.PASSWORD_ENTRY:
            hints = "[Enter] Connect    [Tab] Show/Hide    [Esc] Back"
        elif self.state == OverlayState.ERROR_DISPLAY:
            if self.retry_count >= MAX_RETRIES_PER_SSID:
                hints = "Press any key to return to network list"
            else:
                hints = "[Enter] Retry    [Esc] Network list"
        else:
            hints = ""

        if hints:
            self.canvas.create_text(
                cx, y, text=hints,
                font=self.font_small, fill=TEXT_DIM, anchor="s"
            )

    # -------------------------------------------------------------------
    # Drawing helpers
    # -------------------------------------------------------------------

    def _draw_rounded_rect(self, x1, y1, x2, y2, radius=10,
                           fill="", outline=""):
        """Draw a rounded rectangle on the canvas."""
        # Use polygon approximation for rounded corners
        r = radius
        points = [
            x1 + r, y1,
            x2 - r, y1,
            x2, y1,
            x2, y1 + r,
            x2, y2 - r,
            x2, y2,
            x2 - r, y2,
            x1 + r, y2,
            x1, y2,
            x1, y2 - r,
            x1, y1 + r,
            x1, y1,
        ]
        kwargs = {"fill": fill, "smooth": True}
        if outline:
            kwargs["outline"] = outline
            kwargs["width"] = 2
        else:
            kwargs["outline"] = ""
        self.canvas.create_polygon(points, **kwargs)


# ---------------------------------------------------------------------------
# Standalone launcher (for testing outside of wifi_provision.py)
# ---------------------------------------------------------------------------

def main():
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Wi-Fi overlay (standalone)")
    parser.add_argument("--iface", help="wireless interface (auto-detect if omitted)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    iface = args.iface
    if iface is None:
        from wifi_scanner import detect_interface
        iface = detect_interface()
        if iface is None:
            print("error: no wireless interface found", file=sys.stderr)
            sys.exit(1)

    overlay = WifiOverlay(iface=iface)
    overlay.run()

    if overlay.connected_ip:
        print(f"connected: {overlay.connected_ssid} ({overlay.connected_ip})")
    else:
        print("overlay closed without connecting")
        sys.exit(1)


if __name__ == "__main__":
    main()
