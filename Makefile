PYTHON ?= python3
PREFIX ?= $(HOME)/.local
BIN_DIR := $(PREFIX)/bin
ROOT := $(CURDIR)

.PHONY: test validate-package install-local migrate-mail-store

test:
	$(PYTHON) -m pytest -q

validate-package:
	$(PYTHON) -m pytest -q tests/test_package_descriptor.py

install-local:
	mkdir -p "$(BIN_DIR)"
	printf '%s\n' '#!/usr/bin/env bash' 'PYTHONPATH="$(ROOT)/src" exec "$(PYTHON)" -m codex_mail_workbench.cli "$$@"' > "$(BIN_DIR)/opl-relay"
	chmod +x "$(BIN_DIR)/opl-relay"
	rm -f "$(BIN_DIR)/codex-mail"
	rm -f "$(BIN_DIR)/codex-mail-mcp"

migrate-mail-store:
	bash scripts/migrate-mail-store.sh
