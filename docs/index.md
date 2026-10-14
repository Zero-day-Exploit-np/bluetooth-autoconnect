# bluetooth-autoconnect

A production-ready Linux daemon for automatically reconnecting trusted Bluetooth devices via BlueZ.

## Overview

This project monitors D-Bus events from BlueZ, identifies paired and trusted devices, and reconnects them when adapters are powered on or devices return in range.

## Features

- Multi-adapter support
- Event-driven reconnect logic
- systemd integration
- Packaging for Debian, Fedora, and Arch
- Structured logging and journal support

## Quick start

```bash
python -m pip install bluetooth-autoconnect
bluetooth-autoconnect --daemon --verbose
```
