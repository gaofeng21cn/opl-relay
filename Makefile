PYTHON ?= python3
PREFIX ?= $(HOME)/.local
BIN_DIR := $(PREFIX)/bin
ROOT := $(CURDIR)

.PHONY: test check-framework-support validate-package install-local migrate-mail-store

test:
	@if [ -n "$(OPL_FRAMEWORK_ROOT)" ]; then $(MAKE) check-framework-support; fi
	$(PYTHON) -m pytest -q

check-framework-support:
	@test -n "$(OPL_FRAMEWORK_ROOT)" || { echo 'Set OPL_FRAMEWORK_ROOT to the Framework checkout'; exit 1; }
	node "$(OPL_FRAMEWORK_ROOT)/scripts/materialize-python-support.mjs" --module profile_workspace --target plugins/opl-relay/runtime/codex_mail_workbench/_opl_profile_workspace.py --check
	node "$(OPL_FRAMEWORK_ROOT)/scripts/materialize-python-support.mjs" --module package_content_lock --target workbench/_opl_package_content_lock.py --check

validate-package:
	$(PYTHON) -m pytest -q tests/test_package_descriptor.py

install-local:
	mkdir -p "$(BIN_DIR)"
	printf '%s\n' '#!/usr/bin/env bash' 'PYTHONPATH="$(ROOT)/plugins/opl-relay/runtime" exec "$(PYTHON)" -m codex_mail_workbench.cli "$$@"' > "$(BIN_DIR)/opl-relay"
	chmod +x "$(BIN_DIR)/opl-relay"
	rm -f "$(BIN_DIR)/codex-mail"
	rm -f "$(BIN_DIR)/codex-mail-mcp"

migrate-mail-store:
	bash scripts/migrate-mail-store.sh
