PYTHON ?= python3
PREFIX ?= $(HOME)/.local
BIN_DIR := $(PREFIX)/bin
ROOT := $(CURDIR)

.PHONY: test install-local migrate-mail-store

test:
	$(PYTHON) -m pytest -q

install-local:
	mkdir -p "$(BIN_DIR)"
	for command in opl-relay codex-mail; do \
		printf '%s\n' '#!/usr/bin/env bash' 'PYTHONPATH="$(ROOT)/src" exec "$(PYTHON)" -m codex_mail_workbench.cli "$$@"' > "$(BIN_DIR)/$$command"; \
		chmod +x "$(BIN_DIR)/$$command"; \
	done
	rm -f "$(BIN_DIR)/codex-mail-mcp"

migrate-mail-store:
	bash scripts/migrate-mail-store.sh
