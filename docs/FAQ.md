# FAQ

## Why does BlueZ need to be installed?

This project relies on BlueZ's D-Bus API to discover adapters and devices.

## How do I enable verbose logs?

```bash
bluetooth-autoconnect --daemon --verbose
```

## Can I block a device from auto-connecting?

Yes. Add the device MAC address to the blacklist configuration in `/etc/bluetooth-autoconnect/config.yaml`.
