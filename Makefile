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
	@echo "  load       (phase 2 stub)"
	@echo "  user       (phase 3 stub)"
	@echo "  verify     (phase 6 stub)"
	@echo "  audit      (phase 7 stub)"

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

# Stubs — wired up in later phases.
load:
	@echo "load: not implemented yet (phase 2)"

user:
	@echo "user: not implemented yet (phase 3)"

verify:
	@echo "verify: not implemented yet (phase 6)"

audit:
	@echo "audit: not implemented yet (phase 7)"
