"""Allows running the package as ``python -m bluetooth_autoconnect``."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
