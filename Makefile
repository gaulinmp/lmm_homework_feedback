.PHONY: help install dev test backup load user verify audit init-db

# Default target: show help instead of running install.
.DEFAULT_GOAL := help

help:
	@echo "Available targets:"
	@echo "  help       Show this help message (default)"
	@echo "  install    Install dependencies via 'uv sync'"
	@echo "  init-db    Initialize the SQLite database"
	@echo "  dev        Run the FastAPI app with --reload (override port: 'make dev PORT=9000')"
	@echo "  test       Run the test suite"
	@echo "  backup     Backup data/tutor.db into BACKUP_DIR (default: ~/Dropbox/backups/llm_homework_tutor)"
	@echo "  load       Load assignments/*.md into the database"
	@echo "  user       Add a user: 'make user USER=foo [ROLE=admin]'"
	@echo "  verify     Verify a proof token: 'make verify TOKEN=...'"
	@echo "  audit      Dump LLM exchange: 'make audit ID=<attempt_id>' or 'make audit USER=<username>'"

install:
	uv sync

init-db:
	uv run python -c "from app.db import init_db; init_db(); print('db initialized')"

PORT ?= 8000
dev:
	uv run uvicorn app.main:app --reload --host 127.0.0.1 --port $(PORT)

test:
	uv run pytest

# Backup the SQLite DB into the Dropbox folder via the .backup command.
# Override BACKUP_DIR if you want it elsewhere.
BACKUP_DIR ?= $(HOME)/Dropbox/backups/llm_homework_tutor
backup:
	mkdir -p "$(BACKUP_DIR)"
	sqlite3 data/tutor.db ".backup '$(BACKUP_DIR)/tutor-$$(date +%Y%m%d-%H%M%S).db'"

load:
	uv run python cli/load_assignments.py

# Stubs — wired up in later phases.

ROLE ?= student
user:
	@if [ -z "$(USER)" ]; then echo "usage: make user USER=<username> [ROLE=student|admin]"; exit 1; fi
	uv run python cli/manage_users.py add --username $(USER) --role $(ROLE)

verify:
	@if [ -z "$(TOKEN)" ]; then echo "usage: make verify TOKEN=<token>"; exit 1; fi
	uv run python cli/verify_token.py "$(TOKEN)"

audit:
	@if [ -z "$(ID)" ] && [ -z "$(USER)" ]; then \
	  echo "usage: make audit ID=<attempt_id> | USER=<username>"; exit 1; \
	fi
	@if [ -n "$(ID)" ]; then \
	  uv run python cli/audit.py --attempt $(ID); \
	else \
	  uv run python cli/audit.py --user $(USER); \
	fi
