PYTHON := .venv/bin/python

.PHONY: install lint format typecheck test migration-test package-test check

install: .venv/.installed

.venv/.installed: pyproject.toml Makefile
	/usr/bin/python3 -m venv .venv
	$(PYTHON) -m pip install -e ".[dev]"
	/usr/bin/touch .venv/.installed

lint: install
	$(PYTHON) -m ruff check .
	$(PYTHON) -m ruff format --check .

format: install
	$(PYTHON) -m ruff format .

typecheck: install
	$(PYTHON) -m pyright src tests scripts

test: install
	$(PYTHON) -m pytest

migration-test: install
	$(PYTHON) -m pytest -m migration

package-test: install
	$(PYTHON) -m build --wheel --no-isolation
	$(PYTHON) scripts/verify_wheel.py dist/binkeeper-*.whl

check: lint typecheck test migration-test package-test
