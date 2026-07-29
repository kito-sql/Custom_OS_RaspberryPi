#!/usr/bin/env python3
"""wifi_scanner.py - Wi-Fi interface detection and network scanning.

Uses wpa_cli to scan for available networks and detect the Wi-Fi interface.
Exposes WifiNetwork, WifiScanError, detect_interface(), and scan_networks().
"""

import logging
import subprocess
import time
from dataclasses import dataclass
from typing import List

logger = logging.getLogger("wifi_scanner")

WPA_CLI = "wpa_cli"


class WifiScanError(Exception):
    """Raised when a Wi-Fi scan fails."""
    pass


@dataclass
class WifiNetwork:
    ssid: str
    bssid: str = ""
    signal: int = -70  # dBm signal level e.g. -60
    frequency: int = 2412
    flags: str = ""

    @property
    def signal_quality(self) -> int:
        """Calculate signal quality percentage (0..100) from dBm."""
        if self.signal >= -50:
            return 100
        elif self.signal <= -100:
            return 0
        return 2 * (self.signal + 100)

    @property
    def band(self) -> str:
        """Return '5 GHz' or '2.4 GHz' based on frequency."""
        if self.frequency >= 5000:
            return "5 GHz"
        return "2.4 GHz"

    @property
    def security(self) -> str:
        """Parse flags to return human-friendly security type."""
        f = self.flags.upper()
        if "WPA3" in f or "SAE" in f:
            return "WPA3"
        elif "WPA2" in f and "WPA" in f:
            return "WPA/WPA2"
        elif "WPA2" in f:
            return "WPA2"
        elif "WPA" in f:
            return "WPA"
        elif "WEP" in f:
            return "WEP"
        elif not f or ("ESS" in f and not any(k in f for k in ("WPA", "WEP", "PSK", "SAE", "EAP"))):
            return "Open"
        return "Open"


def detect_interface() -> str:
    """Return the name of the first Wi-Fi interface wpa_supplicant is bound to or in sysfs."""
    try:
        result = subprocess.run(
            [WPA_CLI, "status"],
            capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.splitlines():
            if line.startswith("Selected interface"):
                parts = line.split("'")
                if len(parts) >= 2:
                    return parts[1]
    except Exception:
        pass

    try:
        import os
        for net_if in os.listdir("/sys/class/net"):
            if net_if.startswith("wlan") or net_if.startswith("wlp"):
                return net_if
    except Exception:
        pass

    return "wlan0"


def parse_wpa_cli_scan_results(stdout: str) -> List[WifiNetwork]:
    """Parse output of wpa_cli scan_results into WifiNetwork objects."""
    networks = []
    lines = stdout.splitlines()
    for line in lines:
        parts = line.split("\t")
        if len(parts) >= 5:
            bssid, freq_str, signal_str, flags, ssid = parts[:5]
            ssid = ssid.strip()
            if not ssid:
                continue
            try:
                freq = int(freq_str)
            except ValueError:
                freq = 2412
            try:
                signal = int(signal_str)
            except ValueError:
                signal = -70

            networks.append(WifiNetwork(
                ssid=ssid,
                bssid=bssid,
                signal=signal,
                frequency=freq,
                flags=flags
            ))
    return networks


def scan_networks(iface: str = "wlan0", timeout: int = 10, dedup: bool = True) -> List[WifiNetwork]:
    """Trigger a scan and return a list of WifiNetwork instances."""
    iface = iface or detect_interface()
    try:
        subprocess.run(
            [WPA_CLI, "-i", iface, "scan"],
            capture_output=True, timeout=5
        )
        time.sleep(1.5)
        result = subprocess.run(
            [WPA_CLI, "-i", iface, "scan_results"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            raise WifiScanError(f"wpa_cli scan_results failed with code {result.returncode}")

        raw_networks = parse_wpa_cli_scan_results(result.stdout)

        if dedup:
            raw_networks.sort(key=lambda n: n.signal, reverse=True)
            seen_ssids = set()
            dedup_networks = []
            for net in raw_networks:
                if net.ssid not in seen_ssids:
                    seen_ssids.add(net.ssid)
                    dedup_networks.append(net)
            return dedup_networks

        return raw_networks
    except WifiScanError:
        raise
    except Exception as e:
        logger.warning("Scan failed: %s", e)
        raise WifiScanError(str(e)) from e


def scan(iface: str = "wlan0", timeout: int = 10) -> list:
    """Backward compatibility helper returning dicts."""
    try:
        nets = scan_networks(iface=iface, timeout=timeout, dedup=True)
        return [{"ssid": n.ssid, "signal": str(n.signal), "flags": n.flags} for n in nets]
    except Exception:
        return []


if __name__ == "__main__":
    iface = detect_interface()
    print(f"Interface: {iface}")
    try:
        nets = scan_networks(iface)
        print(f"Found {len(nets)} networks:")
        for n in nets:
            print(f"  {n.ssid:<25} Signal: {n.signal_quality}% ({n.signal}dBm)  Sec: {n.security:<8} Band: {n.band}")
    except Exception as e:
        print(f"Scan error: {e}")
