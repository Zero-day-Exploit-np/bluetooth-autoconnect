"""bluetooth-autoconnect: automatically reconnect trusted, paired Bluetooth devices.

This package talks to BlueZ over D-Bus to discover Bluetooth adapters and
their paired/trusted devices, and connects them either as a one-shot scan
or as a long-running daemon that reacts to D-Bus events (adapter powered
on, device discovered, etc.).
"""

__version__ = "1.0.0"
__author__ = "bluetooth-autoconnect contributors"
__license__ = "MIT"
