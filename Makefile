.PHONY: help install dev test backup load load-test user verify audit init-db

# Default target: show help instead of running install.
.DEFAULT_GOAL := help

help:
	@echo "Available targets:"
	@echo "  help       Show this help message (default)"
	@echo "  install    Install dependencies via 'uv sync'"
	@echo "  init-db    Initialize the SQLite database"
	@echo "  dev        Run the FastAPI app with --reload (override port: 'make dev PORT=9000')"
	@echo "  test       Run the test suite"
	@echo "  backup     Snapshot data/tutor.db via scripts/backup.sh into BACKUP_DIR"
	@echo "  load       Load assignments/*.md into the database"
	@echo "  load-test  Run the locust load test (LOCUST_USERS, LOCUST_SPAWN_RATE, LOCUST_RUN_TIME)"
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

# Snapshot the SQLite DB. scripts/backup.sh respects BACKUP_DIR + RETAIN env.
BACKUP_DIR ?= $(HOME)/Dropbox/llm_homework_tutor_backups
backup:
	BACKUP_DIR="$(BACKUP_DIR)" bash scripts/backup.sh

load:
	uv run python cli/load_assignments.py

# Locust load test against a running staging server.
# Override LOCUST_USERS, LOCUST_SPAWN_RATE, LOCUST_RUN_TIME, LOCUST_HOST.
LOCUST_HOST       ?= http://127.0.0.1:8000
LOCUST_USERS      ?= 50
LOCUST_SPAWN_RATE ?= 5
LOCUST_RUN_TIME   ?= 2m
load-test:
	uv run locust -f scripts/locust_students.py \
		--host=$(LOCUST_HOST) \
		--headless \
		-u $(LOCUST_USERS) \
		-r $(LOCUST_SPAWN_RATE) \
		-t $(LOCUST_RUN_TIME)

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
