# Developer Guide

## Environment setup

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -e .[dev,docs]
```

## Run tests

```bash
pytest -q
```

## Format and lint

```bash
ruff check .
black .
mypy bluetooth_autoconnect
```

## Release checklist

1. Update the changelog
2. Ensure tests and lint pass
3. Build distributions
4. Publish Python package
5. Tag the release and upload GitHub assets
