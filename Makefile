.PHONY: install dev test backup load user verify audit init-db

install:
	uv sync

init-db:
	uv run python -c "from app.db import init_db; init_db(); print('db initialized')"

dev:
	uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

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
