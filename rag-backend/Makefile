# Makefile — rag-backend test shortcuts
# Cara pakai:
#   Windows (Git Bash / MSYS2):  make test
#   Linux / Mac:                 make test
#   Claude Code:                 make test  (auto-detect Python)

# ── Python interpreter ────────────────────────────────────────────────────────
# Prioritas: .venv (jika ada) → python3 → python
ifeq ($(OS),Windows_NT)
    VENV_PYTHON := .venv/Scripts/python.exe
else
    VENV_PYTHON := .venv/bin/python
endif

# Gunakan .venv jika ada, fallback ke system python
ifeq ($(shell test -f $(VENV_PYTHON) && echo yes),yes)
    PYTHON := $(VENV_PYTHON)
else ifeq ($(shell which python3 2>/dev/null),)
    PYTHON := python
else
    PYTHON := python3
endif

.PHONY: test unit regression file all venv

# Default: unit tests saja (no ES/Redis needed)
test: unit

unit:
	$(PYTHON) -m pytest -m unit -q

regression:
	$(PYTHON) -m pytest tests/test_regression.py -v -m unit

# Jalankan satu file: make file FILE=tests/test_comparison_handler.py
file:
	$(PYTHON) -m pytest $(FILE) -v

# Semua test (termasuk non-unit, butuh ES+Redis aktif)
all:
	$(PYTHON) -m pytest -q

venv:
	python -m venv .venv
	$(PYTHON) -m pip install -r requirements.txt
