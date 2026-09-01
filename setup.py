"""Legacy setuptools entry point.

Project metadata lives in ``pyproject.toml`` (PEP 621). This file exists
only for compatibility with older tooling (e.g. ``debuild``/``dh_python3``
on some distributions) that expects ``python3 setup.py`` to work; it does
not duplicate metadata.
"""

from setuptools import setup

if __name__ == "__main__":
    setup()
