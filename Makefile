.PHONY: help venv install install-dev install-system uninstall test lint format typecheck \
        clean build sdist wheel systemd-install systemd-user-install

PYTHON ?= python3
PIP ?= pip3
PREFIX ?= /usr/local
SYSTEMD_SYSTEM_DIR ?= /etc/systemd/system
SYSTEMD_USER_DIR ?= $(HOME)/.config/systemd/user

help:
	@echo "Common targets:"
	@echo "  make venv                 Create a local .venv virtualenv"
	@echo "  make install              pip install this package (runtime deps only)"
	@echo "  make install-dev          pip install with dev/test dependencies"
	@echo "  make install-system       Install into the system Python (requires sudo)"
	@echo "  make uninstall            pip uninstall bluetooth-autoconnect"
	@echo "  make test                 Run the pytest suite"
	@echo "  make lint                 Run flake8"
	@echo "  make format               Run black"
	@echo "  make typecheck            Run mypy"
	@echo "  make build                Build sdist + wheel into dist/"
	@echo "  make systemd-install      Install the system-wide systemd service"
	@echo "  make systemd-user-install Install the per-user systemd service"
	@echo "  make clean                Remove build artifacts"

venv:
	$(PYTHON) -m venv .venv
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install -e ".[dev]"
	@echo "Activate with: source .venv/bin/activate"

install:
	$(PIP) install --user .

install-dev:
	$(PIP) install --user -e ".[dev]"

install-system:
	sudo $(PIP) install .

uninstall:
	$(PIP) uninstall -y bluetooth-autoconnect

test:
	$(PYTHON) -m pytest -v

lint:
	$(PYTHON) -m flake8 bluetooth_autoconnect tests

format:
	$(PYTHON) -m black bluetooth_autoconnect tests

typecheck:
	$(PYTHON) -m mypy bluetooth_autoconnect

build: clean
	$(PYTHON) -m pip install --upgrade build
	$(PYTHON) -m build

sdist:
	$(PYTHON) -m build --sdist

wheel:
	$(PYTHON) -m build --wheel

systemd-install: systemd/bluetooth-autoconnect.service
	sudo install -Dm644 systemd/bluetooth-autoconnect.service \
		$(SYSTEMD_SYSTEM_DIR)/bluetooth-autoconnect.service
	sudo systemctl daemon-reload
	@echo "Enable with: sudo systemctl enable --now bluetooth-autoconnect.service"

systemd-user-install: systemd/bluetooth-autoconnect-user.service
	mkdir -p $(SYSTEMD_USER_DIR)
	install -Dm644 systemd/bluetooth-autoconnect-user.service \
		$(SYSTEMD_USER_DIR)/bluetooth-autoconnect.service
	systemctl --user daemon-reload
	@echo "Enable with: systemctl --user enable --now bluetooth-autoconnect.service"

clean:
	rm -rf build dist *.egg-info .pytest_cache .mypy_cache
	find . -type d -name "__pycache__" -exec rm -rf {} +
