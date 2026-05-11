# Deploy run-book — ADA Homework Tutor

This directory ships everything needed to run the tutor as a managed service
on the university Linux box. Targets a single-host install behind caddy.

```
              ┌──────────┐
   internet ──▶ caddy 443 │ (TLS, reverse proxy)
              └────┬─────┘
                   │ 127.0.0.1:8001
              ┌────▼─────────────────┐
              │ uvicorn (2 workers)  │  tutor-app.service
              │ app.main:app         │
              └──┬───────────────────┘
                 │ 127.0.0.1:8080  (OpenAI-compat)
              ┌──▼─────────────────┐
              │ llama-server       │  tutor-llama.service
              └────────────────────┘
```

## One-time install

```bash
# 1. system user + repo
sudo useradd -m -s /bin/bash tutor
sudo -u tutor git clone <repo-url> /home/tutor/llm_homework_tutor
sudo -u tutor bash -lc 'cd ~/llm_homework_tutor && curl -LsSf https://astral.sh/uv/install.sh | sh && make install'

# 2. config — copy .env.example, fill HMAC_SECRET, SESSION_SECRET, ENV=prod
sudo -u tutor cp /home/tutor/llm_homework_tutor/.env.example /home/tutor/llm_homework_tutor/.env
sudo -u tutor vim /home/tutor/llm_homework_tutor/.env

# 3. DB + assignments
sudo -u tutor bash -lc 'cd ~/llm_homework_tutor && make init-db && make load'

# 4. systemd units
sudo install -m 644 deploy/tutor-llama.service /etc/systemd/system/
sudo install -m 644 deploy/tutor-app.service   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tutor-llama.service tutor-app.service

# 5. caddy — edit deploy/caddyfile to swap tutor.example.edu for the real host
sudo cp deploy/caddyfile /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

Verify with::

    systemctl status tutor-app tutor-llama caddy
    curl -fsS https://tutor.example.edu/healthz

## Operations

### Roll an LLM config change *without* restarting llama-server

Most config changes live in [config/llm.toml](../config/llm.toml). The router
reads it once at process startup, so:

- **Provider or model change for a cloud role** (anthropic/openai/gemini):
  edit `config/llm.toml`, restart only the app:
  `sudo systemctl restart tutor-app.service`.
- **Sampling params for the local model** (temperature, top_p, ctx):
  edit `scripts/llama_server.sh`, restart only llama:
  `sudo systemctl restart tutor-llama.service`.
- **Routing a role at the local server from the cloud or vice versa**: edit
  `config/llm.toml`, restart app only. The llama-server keeps running.

### Backups

`scripts/backup.sh` runs `sqlite3 .backup` (safe with WAL + a running
app) and drops the snapshot in `~/Dropbox/llm_homework_tutor_backups/`.
Retains the most recent 30 by default (`RETAIN=`).

Cron line on the prod box (under the `tutor` user's crontab):

    15 2 * * *  /home/tutor/llm_homework_tutor/scripts/backup.sh >/dev/null 2>&1

### Restore

```bash
sudo systemctl stop tutor-app.service
sudo -u tutor cp ~/Dropbox/llm_homework_tutor_backups/tutor-YYYYMMDD-HHMMSS.db \
                 /home/tutor/llm_homework_tutor/data/tutor.db
sudo systemctl start tutor-app.service
```

Verify with `make audit USER=<some_student>` to confirm history is intact.

### Logs

`journalctl -u tutor-app.service -f` for the FastAPI/uvicorn process,
`journalctl -u tutor-llama.service -f` for llama-server.

Process logs are structured JSON when `LOG_JSON=1` (the systemd unit defaults
to that via `scripts/start_server.sh`). Every LLM exchange is *also* appended
to `data/logs/llm_messages.jsonl` for offline grep — that file is in addition
to the `llm_messages` SQLite table, not a replacement for it.

### Load test

Hit a staging box (`ENV=staging` with mock LLM keys) with::

    make load-test                 # 50 students, 2 minutes
    LOCUST_STUDENTS=100 make load-test

Locust will fail if any 5xx is observed. See `scripts/locust_students.py` for
the user model — log in, pick assignment, submit, poll queue-status.

### Pre-flight checklist before each restart in prod

1. `make test` on the dev machine. All green.
2. `git pull` on the prod box.
3. `make install` (only if `pyproject.toml` changed).
4. `make init-db` is idempotent — safe to run anytime.
5. `sudo systemctl restart tutor-app.service`.
6. `curl -fsS https://tutor.example.edu/healthz`.
7. Tail `journalctl -u tutor-app.service -f` for one student request.

## Out of scope

Multi-host, CI/CD, automated rollback, container/k8s. See `docs/design/todo.md`
for the full follow-up list.
