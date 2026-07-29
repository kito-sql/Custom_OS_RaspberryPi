#!/usr/bin/env python3
"""
wifi_connector.py - Wi-Fi connection manager for DietPi kiosk provisioning

Drives wpa_supplicant via its wpa_cli control interface to connect to a
selected network.  Never edits wpa_supplicant.conf directly — all changes
go through the live control socket (add_network / set_network /
enable_network / select_network) and only persist via save_config after
a verified connection.

Design constraints (from implementation plan v3):
  - No wpa_passphrase subprocess: PSK sent via set_network, wpa_supplicant
    hashes internally.  save_config writes only the hash to disk.
  - Plaintext password never touches disk.
  - Error classification via wpa_state transition tracking, not just
    final-state timeout.
  - Config file verified 0600 root:root after every save_config.
  - Interface is never hardcoded — always passed in by the caller
    (wifi_provision.py resolves it once via detect_interface()).

Usage (headless test over SSH):
    from wifi_connector import connect, check_connectivity
    result = connect("wlan0", "MySSID", "MyPassword")
    print(result)

    ok, ip = check_connectivity("wlan0")
    print(ok, ip)
"""

import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple

logger = logging.getLogger("wifi_connector")
logger.addHandler(logging.NullHandler())

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_CONNECT_TIMEOUT = 20      # max seconds to wait for association + IP
DHCP_TIMEOUT = 15                 # extra seconds after COMPLETED to wait for IPv4
POLL_INTERVAL = 0.5               # wpa_cli status poll interval
WPA_CONF_PATH = "/etc/wpa_supplicant/wpa_supplicant.conf"

# wpa_state values we track for error classification
STATE_SCANNING = "SCANNING"
STATE_ASSOCIATING = "ASSOCIATING"
STATE_ASSOCIATED = "ASSOCIATED"
STATE_4WAY_HANDSHAKE = "4WAY_HANDSHAKE"
STATE_GROUP_HANDSHAKE = "GROUP_HANDSHAKE"
STATE_COMPLETED = "COMPLETED"
STATE_DISCONNECTED = "DISCONNECTED"
STATE_INACTIVE = "INACTIVE"
STATE_INTERFACE_DISABLED = "INTERFACE_DISABLED"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class ConnectionResult:
    """Result of a connection attempt."""
    success: bool
    ip_address: Optional[str] = None
    error: Optional[str] = None
    error_type: str = "success"
    # error_type values:
    #   "success"       - connected, IP assigned
    #   "auth_failed"   - wrong password (4WAY_HANDSHAKE cycling)
    #   "not_found"     - SSID not found (stuck in SCANNING)
    #   "timeout"       - general timeout (ASSOCIATING issues, weak signal)
    #   "dhcp_failed"   - COMPLETED but no IPv4 assigned
    #   "command_error" - wpa_cli command failed unexpectedly

    def __repr__(self):
        if self.success:
            return f"ConnectionResult(success=True, ip={self.ip_address!r})"
        return f"ConnectionResult(success=False, error_type={self.error_type!r}, error={self.error!r})"


# ---------------------------------------------------------------------------
# Low-level wpa_cli helpers
# ---------------------------------------------------------------------------

class WpaCliError(Exception):
    """Raised when a wpa_cli command fails."""


def _wpa_cli(iface: str, command: str, timeout: int = 5) -> str:
    """
    Run a wpa_cli command and return stdout.

    Raises WpaCliError on non-zero exit or timeout.
    """
    cmd = ["wpa_cli", "-i", iface, command]
    logger.debug("wpa_cli: %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
    except FileNotFoundError:
        raise WpaCliError("wpa_cli not found on PATH")
    except subprocess.TimeoutExpired:
        raise WpaCliError(f"wpa_cli timed out: {' '.join(cmd)}")
    if result.returncode != 0:
        raise WpaCliError(
            f"wpa_cli failed ({result.returncode}): {' '.join(cmd)} "
            f"stderr={result.stderr.strip()!r}"
        )
    return result.stdout.strip()


def _wpa_cli_with_args(iface: str, *args: str, timeout: int = 5) -> str:
    """
    Run a wpa_cli command with multiple arguments.

    Example: _wpa_cli_with_args("wlan0", "set_network", "0", "ssid", '"MyNet"')
    """
    cmd = ["wpa_cli", "-i", iface] + list(args)
    logger.debug("wpa_cli: %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
    except FileNotFoundError:
        raise WpaCliError("wpa_cli not found on PATH")
    except subprocess.TimeoutExpired:
        raise WpaCliError(f"wpa_cli timed out: {' '.join(cmd)}")
    if result.returncode != 0:
        raise WpaCliError(
            f"wpa_cli failed ({result.returncode}): {' '.join(cmd)} "
            f"stderr={result.stderr.strip()!r}"
        )
    return result.stdout.strip()


def _wpa_cli_ok(iface: str, *args: str) -> bool:
    """Run a wpa_cli command and return True if the response contains 'OK'."""
    try:
        response = _wpa_cli_with_args(iface, *args)
        return "OK" in response
    except WpaCliError as e:
        logger.warning("wpa_cli command failed: %s", e)
        return False


# ---------------------------------------------------------------------------
# Status parsing
# ---------------------------------------------------------------------------

def _parse_wpa_status(output: str) -> dict:
    """
    Parse 'wpa_cli status' output into a dict.

    Format is key=value lines, e.g.:
        bssid=aa:bb:cc:dd:ee:ff
        freq=2412
        ssid=HomeNet
        wpa_state=COMPLETED
        ip_address=192.168.1.42
        ...
    """
    status = {}
    for line in output.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            status[key.strip()] = value.strip()
    return status


def _get_wpa_state(iface: str) -> Tuple[str, dict]:
    """
    Get current wpa_state and full status dict.
    Returns (wpa_state, full_status_dict).
    """
    try:
        output = _wpa_cli(iface, "status")
        status = _parse_wpa_status(output)
        state = status.get("wpa_state", "UNKNOWN")
        return state, status
    except WpaCliError as e:
        logger.warning("failed to get wpa_state: %s", e)
        return "UNKNOWN", {}


# ---------------------------------------------------------------------------
# IP address retrieval
# ---------------------------------------------------------------------------

def _get_ipv4_address(iface: str) -> Optional[str]:
    """
    Get the IPv4 address assigned to the interface via 'ip -4 addr show'.

    Returns the IP address string (without prefix length) or None.
    """
    try:
        result = subprocess.run(
            ["ip", "-4", "addr", "show", iface],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            return None
        # Match: inet 192.168.1.42/24
        match = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)/", result.stdout)
        return match.group(1) if match else None
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


def _has_default_route(iface: str) -> bool:
    """Check if a default route exists through the given interface."""
    try:
        result = subprocess.run(
            ["ip", "route", "show", "default"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            return False
        # Look for: default via x.x.x.x dev <iface>
        for line in result.stdout.splitlines():
            if "default" in line and iface in line:
                return True
        return False
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


# ---------------------------------------------------------------------------
# Config file permissions
# ---------------------------------------------------------------------------

def _verify_config_permissions():
    """
    Ensure wpa_supplicant.conf is 0600 root:root.

    Called after every save_config to guarantee plaintext PSK hashes
    (which are still sensitive) aren't world-readable.
    """
    try:
        if os.path.exists(WPA_CONF_PATH):
            os.chmod(WPA_CONF_PATH, 0o600)
            os.chown(WPA_CONF_PATH, 0, 0)  # root:root
            logger.debug("verified %s permissions: 0600 root:root", WPA_CONF_PATH)
    except OSError as e:
        logger.warning("failed to set permissions on %s: %s", WPA_CONF_PATH, e)


# ---------------------------------------------------------------------------
# Public API: check_any_connectivity and check_connectivity
# ---------------------------------------------------------------------------

def check_any_connectivity() -> Tuple[bool, Optional[str], Optional[str]]:
    """
    Check if any interface is connected to the network (has an IP address).
    Prioritizes Ethernet interfaces (eth* / en*) over wireless.
    Returns (connected: bool, ip_address: Optional[str], interface: Optional[str]).
    """
    try:
        # Step 1: Check wired Ethernet interfaces (eth*, en*) first
        if os.path.exists("/sys/class/net"):
            for iface in sorted(os.listdir("/sys/class/net")):
                if iface.startswith("eth") or iface.startswith("en"):
                    ip = _get_ipv4_address(iface)
                    if ip and not ip.startswith("169.254."):
                        logger.info("found active network connectivity on prioritized wired interface %s with IP %s", iface, ip)
                        return True, ip, iface

        # Step 2: Check default route candidates
        result = subprocess.run(
            ["ip", "route", "show", "default"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                parts = line.split()
                if "default" in parts:
                    try:
                        dev_idx = parts.index("dev")
                        iface = parts[dev_idx + 1]
                        if iface == "lo":
                            continue
                        ip = _get_ipv4_address(iface)
                        if ip and not ip.startswith("169.254."):
                            logger.info("found active network connectivity on default route interface %s with IP %s", iface, ip)
                            return True, ip, iface
                    except (ValueError, IndexError):
                        continue

        # Step 3: Check wireless interfaces (wlan*, wlp*)
        if os.path.exists("/sys/class/net"):
            for iface in sorted(os.listdir("/sys/class/net")):
                if iface.startswith("wlan") or iface.startswith("wlp"):
                    ip = _get_ipv4_address(iface)
                    if ip and not ip.startswith("169.254."):
                        logger.info("found active network connectivity on wireless interface %s with IP %s", iface, ip)
                        return True, ip, iface

        return False, None, None
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.warning("check_any_connectivity error: %s", exc)
        return False, None, None


def check_connectivity(iface: str) -> Tuple[bool, Optional[str]]:
    """
    Check if the interface is currently connected with full connectivity.

    Returns (connected: bool, ip_address: Optional[str]).

    Checks:
      1. wpa_cli status -> wpa_state == COMPLETED
      2. Interface has an IPv4 address
      3. A default route exists through the interface

    Gateway ICMP (ping) is deliberately NOT checked — many healthy gateways
    block ping, and making it a gate would cause false negatives.
    """
    state, status = _get_wpa_state(iface)

    if state != STATE_COMPLETED:
        logger.debug("wpa_state=%s (not COMPLETED) on %s", state, iface)
        return False, None

    ip = _get_ipv4_address(iface)
    if not ip:
        logger.debug("no IPv4 address on %s despite wpa_state=COMPLETED", iface)
        return False, None

    if not _has_default_route(iface):
        logger.debug("no default route through %s despite IPv4 %s", iface, ip)
        return False, None

    logger.info("connectivity check passed: %s has %s with default route", iface, ip)
    return True, ip


def get_connected_ssid(iface: str) -> Optional[str]:
    """Return the connected SSID when wpa_supplicant reports a completed link."""
    state, status = _get_wpa_state(iface)
    if state != STATE_COMPLETED:
        return None
    return status.get("ssid") or None


# ---------------------------------------------------------------------------
# Public API: connect
# ---------------------------------------------------------------------------

def connect(
    iface: str,
    ssid: str,
    password: str,
    security: str = "WPA2",
    timeout: int = DEFAULT_CONNECT_TIMEOUT,
) -> ConnectionResult:
    """
    Attempt to connect to the given SSID using the wpa_cli control interface.

    Workflow:
      1. add_network -> get network ID
      2. set_network <id> ssid "<ssid>"
      3. set_network <id> psk "<password>"  (or key_mgmt=NONE for open)
      4. enable_network <id> + select_network <id>
      5. Poll wpa_cli status, tracking state transitions for error classification
      6. On COMPLETED + IPv4 -> remove older profiles for this SSID -> save_config
         -> verify permissions -> return success
      7. On failure -> remove only the new network -> return typed error

    Args:
        iface:    Wireless interface name (from detect_interface())
        ssid:     Network SSID to connect to
        password: Network password (ignored for open networks)
        security: Security type from wifi_scanner ("Open", "WEP", "WPA", "WPA2", etc.)
        timeout:  Max seconds to wait for association + IP

    Returns:
        ConnectionResult with success/failure details
    """
    network_id: Optional[str] = None

    try:
        # Step 1: Add a new network
        try:
            response = _wpa_cli(iface, "add_network")
            network_id = response.strip()
            # wpa_cli add_network returns just the numeric network ID
            if not network_id.isdigit():
                return ConnectionResult(
                    success=False,
                    error=f"Unexpected add_network response: {response!r}",
                    error_type="command_error",
                )
            logger.info("added network id=%s for SSID=%r", network_id, ssid)
        except WpaCliError as e:
            return ConnectionResult(
                success=False,
                error=f"Failed to add network: {e}",
                error_type="command_error",
            )

        # Step 2: Set SSID
        # wpa_cli set_network expects SSID in double quotes
        if not _wpa_cli_ok(iface, "set_network", network_id, "ssid", f'"{ssid}"'):
            _cleanup_network(iface, network_id)
            return ConnectionResult(
                success=False,
                error="Failed to set SSID",
                error_type="command_error",
            )

        # Step 3: Set credentials based on security type
        if security == "Open":
            # Open network: no password needed
            if not _wpa_cli_ok(iface, "set_network", network_id, "key_mgmt", "NONE"):
                _cleanup_network(iface, network_id)
                return ConnectionResult(
                    success=False,
                    error="Failed to configure open network",
                    error_type="command_error",
                )
            logger.info("configured network %s as open (key_mgmt=NONE)", network_id)
        else:
            # WPA/WPA2/WPA3: set PSK via control interface
            # wpa_supplicant hashes internally; plaintext never hits disk
            if not _wpa_cli_ok(iface, "set_network", network_id, "psk", f'"{password}"'):
                _cleanup_network(iface, network_id)
                return ConnectionResult(
                    success=False,
                    error="Failed to set password",
                    error_type="command_error",
                )
            logger.info("set PSK for network %s (via control interface)", network_id)

        # Step 4: Enable and select the network
        if not _wpa_cli_ok(iface, "enable_network", network_id):
            _cleanup_network(iface, network_id)
            return ConnectionResult(
                success=False,
                error="Failed to enable network",
                error_type="command_error",
            )

        if not _wpa_cli_ok(iface, "select_network", network_id):
            _cleanup_network(iface, network_id)
            return ConnectionResult(
                success=False,
                error="Failed to select network",
                error_type="command_error",
            )

        logger.info("network %s enabled+selected, polling for connection...", network_id)

        # Step 5: Poll for connection with state-transition tracking
        result = _poll_connection(iface, network_id, ssid, timeout)
        return result

    except Exception as e:
        # Catch-all: ensure cleanup on unexpected errors
        logger.exception("unexpected error during connect")
        if network_id is not None:
            _cleanup_network(iface, network_id)
        return ConnectionResult(
            success=False,
            error=f"Unexpected error: {e}",
            error_type="command_error",
        )


# ---------------------------------------------------------------------------
# Connection polling with state-transition classification
# ---------------------------------------------------------------------------

def _poll_connection(
    iface: str,
    network_id: str,
    ssid: str,
    timeout: int,
) -> ConnectionResult:
    """
    Poll wpa_cli status and classify the outcome based on observed state
    transitions, not just the final state.

    State classification:
      - Stuck in SCANNING the entire time  -> "not_found"
      - Cycling through 4WAY_HANDSHAKE -> DISCONNECTED -> 4WAY_HANDSHAKE  -> "auth_failed"
      - COMPLETED but no IPv4 after DHCP_TIMEOUT  -> "dhcp_failed"
      - COMPLETED + IPv4  -> "success"
      - ASSOCIATING cycling / other  -> "timeout"
    """
    deadline = time.time() + timeout
    completed_at: Optional[float] = None  # timestamp when COMPLETED first observed

    # State transition counters for classification
    handshake_failures = 0    # 4WAY_HANDSHAKE -> DISCONNECTED transitions
    scanning_only = True      # never left SCANNING state
    prev_state = ""

    while time.time() < deadline:
        state, status = _get_wpa_state(iface)
        logger.debug("poll: wpa_state=%s (prev=%s)", state, prev_state)

        # Track transitions
        if state != STATE_SCANNING:
            scanning_only = False

        # Detect auth failure: 4WAY_HANDSHAKE -> DISCONNECTED transition
        if (prev_state in (STATE_4WAY_HANDSHAKE, STATE_GROUP_HANDSHAKE)
                and state == STATE_DISCONNECTED):
            handshake_failures += 1
            logger.debug("handshake failure #%d detected", handshake_failures)
            if handshake_failures >= 2:
                # Definitive: password is wrong
                _cleanup_network(iface, network_id)
                return ConnectionResult(
                    success=False,
                    error="Incorrect password — please try again",
                    error_type="auth_failed",
                )

        # Check for COMPLETED state
        if state == STATE_COMPLETED:
            if completed_at is None:
                completed_at = time.time()
                logger.info("wpa_state=COMPLETED, waiting for IPv4...")

            # Check for IPv4
            ip = _get_ipv4_address(iface)
            if ip:
                # Success! Persist to disk and verify permissions
                logger.info("connected to %r with IP %s", ssid, ip)
                _save_and_secure(iface, network_id, ssid)
                return ConnectionResult(
                    success=True,
                    ip_address=ip,
                    error_type="success",
                )

            # COMPLETED but no IPv4 yet — check DHCP timeout
            if time.time() - completed_at > DHCP_TIMEOUT:
                _cleanup_network(iface, network_id)
                return ConnectionResult(
                    success=False,
                    error="Connected but no IP address — check router DHCP settings",
                    error_type="dhcp_failed",
                )

        prev_state = state
        time.sleep(POLL_INTERVAL)

    # Timed out — classify based on what we observed
    if scanning_only:
        _cleanup_network(iface, network_id)
        return ConnectionResult(
            success=False,
            error="Network not found — check if the router is in range",
            error_type="not_found",
        )

    if handshake_failures >= 1:
        # Saw at least one handshake failure but didn't hit the threshold above
        _cleanup_network(iface, network_id)
        return ConnectionResult(
            success=False,
            error="Incorrect password — please try again",
            error_type="auth_failed",
        )

    # General timeout: saw ASSOCIATING or other states, never completed
    _cleanup_network(iface, network_id)
    return ConnectionResult(
        success=False,
        error="Unable to connect — signal may be too weak or network is busy",
        error_type="timeout",
    )


# ---------------------------------------------------------------------------
# Cleanup and persistence helpers
# ---------------------------------------------------------------------------

def _cleanup_network(iface: str, network_id: str):
    """Remove a failed network from wpa_supplicant's live config (not disk)."""
    try:
        _wpa_cli_ok(iface, "disable_network", network_id)
        _wpa_cli_ok(iface, "remove_network", network_id)
        logger.info("cleaned up failed network id=%s", network_id)
    except Exception as e:
        logger.warning("cleanup failed for network %s: %s", network_id, e)


def _remove_duplicate_profiles(iface: str, ssid: str, keep_network_id: str) -> None:
    """Remove older saved profiles for *ssid* only after a new link is verified."""
    try:
        output = _wpa_cli(iface, "list_networks")
    except WpaCliError as exc:
        logger.warning("could not inspect profiles for duplicate cleanup: %s", exc)
        return

    for row in output.splitlines()[1:]:
        fields = row.split("\t")
        if len(fields) < 2:
            continue
        profile_id, profile_ssid = fields[0], fields[1]
        if profile_id == keep_network_id or profile_ssid != ssid or not profile_id.isdigit():
            continue
        if _wpa_cli_ok(iface, "remove_network", profile_id):
            logger.info("removed older profile id=%s for SSID=%r", profile_id, ssid)
        else:
            logger.warning("failed to remove older profile id=%s for SSID=%r", profile_id, ssid)


def _save_and_secure(iface: str, network_id: str, ssid: str) -> None:
    """Persist a verified connection and remove only older duplicate profiles."""
    _remove_duplicate_profiles(iface, ssid, network_id)
    if _wpa_cli_ok(iface, "save_config"):
        logger.info("save_config succeeded — credentials persisted to disk")
        _verify_config_permissions()
    else:
        # Non-fatal: connection works but won't survive reboot
        logger.warning(
            "save_config failed — connection is live but won't persist across reboot. "
            "This may happen if update_config=1 is not set in wpa_supplicant.conf"
        )


# ---------------------------------------------------------------------------
# CLI (for headless testing over SSH)
# ---------------------------------------------------------------------------

def main():
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Wi-Fi connection manager (headless test)")
    sub = parser.add_subparsers(dest="command")

    # 'check' subcommand
    check_p = sub.add_parser("check", help="check current connectivity")
    check_p.add_argument("--iface", help="interface (auto-detect if omitted)")

    # 'connect' subcommand
    conn_p = sub.add_parser("connect", help="connect to a network")
    conn_p.add_argument("ssid", help="SSID to connect to")
    conn_p.add_argument("--password", "-p", default="", help="network password")
    conn_p.add_argument("--security", default="WPA2",
                        help="security type: Open, WEP, WPA, WPA2 (default: WPA2)")
    conn_p.add_argument("--iface", help="interface (auto-detect if omitted)")
    conn_p.add_argument("--timeout", type=int, default=DEFAULT_CONNECT_TIMEOUT,
                        help=f"connection timeout in seconds (default: {DEFAULT_CONNECT_TIMEOUT})")

    parser.add_argument("-v", "--verbose", action="store_true",
                        help="enable debug logging")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    # Interface resolution
    iface = args.iface if hasattr(args, "iface") and args.iface else None
    if iface is None:
        # Import here to avoid circular dependency at module level
        from wifi_scanner import detect_interface
        iface = detect_interface()
        if iface is None:
            print("error: no wireless interface found", file=sys.stderr)
            sys.exit(1)
        print(f"detected interface: {iface}", file=sys.stderr)

    if args.command == "check":
        connected, ip = check_connectivity(iface)
        if connected:
            print(f"connected: {ip}")
        else:
            print("not connected")
            sys.exit(1)

    elif args.command == "connect":
        result = connect(
            iface=iface,
            ssid=args.ssid,
            password=args.password,
            security=args.security,
            timeout=args.timeout,
        )
        if result.success:
            print(f"connected: {result.ip_address}")
        else:
            print(f"failed ({result.error_type}): {result.error}", file=sys.stderr)
            sys.exit(1)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
