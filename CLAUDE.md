# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

SnoozMate (好眠) cloud backend: a FastAPI service for an anti-snoring device (ESP32-S3 "月石" host + under-pillow vibration pad). The device does all real-time detection/intervention locally and offline; the cloud only ingests events after the fact, computes summaries, produces AI/rule-based morning interpretations, and proposes config candidates that the user must approve. **The cloud never directly controls the vibration pad and must never bypass the hard safety limits.** Deployed to WeChat Cloud Hosting (微信云托管); the WeChat mini program client lives in a sibling repo (`../miniprogram/miniprogram`) and is not in this folder.

This folder is an upload snapshot: README references `tests/` and `scripts/` (e.g. `scripts/migrate_sqlite_to_mysql.py`, `scripts/check_mysql.py`, `scripts/seed_demo_data_cloud.py`) that are excluded by `.dockerignore` and not present here. Do not assume they exist locally.

## Commands

```bash
pip install -r requirements.txt
python main.py            # http://localhost:8000 (optional port arg: python main.py 8080)
uvicorn main:app --port 8000 --reload

# Tests (when tests/ is present; pytest.ini restricts collection to tests/test_*.py)
python -m unittest discover -s tests -v
pytest
pytest tests/test_x.py::TestClass::test_name

# Docker (production image; listens on port 80)
docker build -t snoozmate-api . && docker run -p 8000:80 snoozmate-api
```

Useful URLs when running: `/docs` (Swagger), `/dashboard` (static admin page), `/healthz` (no DB), `/readyz` (checks DB, 503 on failure), `/diagnostics` (backend/LLM/WeChat mode, never secrets).

Copy `.env.example` to `.env` for local config. All secrets come from env vars only; never hardcode keys.

## Architecture

```
main.py                     FastAPI app, CORS (wide open), startup DB init, probes, dashboard mount
app/config.py               All env-driven settings; DATABASE_BACKEND auto-selects mysql if DB_HOST/USER/NAME set
app/database.py             All persistence (schema + CRUD for every table), night bucketing, summaries
app/db_backend.py           SQLite→MySQL SQL translation layer + bounded connection pool
app/models/schemas.py       Pydantic request/response models (EventIn, DeviceConfig, AgentTick, ...)
app/api/auth.py             Phase ① login (WeChat code2session or debug/test_code_*), binding, profile
app/api/events.py           Phase ③ single/batch event ingest, night event queries
app/api/agent.py            Phases ②④⑤: agent tick, device config, morning report, weekly, candidates,
                            feedback, bandit, AI chat/interpretation, sound/light state, demo/settings-command state
app/agent/engine.py         SnoozMateAgent state machine ("five questions" loop) + rule-based weekly_adjustment
app/agent/candidates.py     Candidate config lifecycle: create → approve (idempotent, versioned, safety-clamped) / reject
app/agent/ai_interpretation.py  LLM call (OpenAI-compatible, hard timeouts) with rule-based fallback + grounding
app/agent/bandit.py         Thompson-sampling parameter tuner; own tables bandit_state / bandit_history
app/audio/                  Standalone snore-detection baseline + PyTorch training script; NOT imported by the
                            service and torch is not in requirements.txt
app/seed_on_startup.py      Demo data seeding, only when SNOOZMATE_SEED_DEMO_DATA=true
```

### Dual database backend

`database.py` writes SQLite-dialect SQL (`INSERT OR IGNORE`, `ON CONFLICT ... DO UPDATE SET ... excluded.x`, `BEGIN IMMEDIATE`, `?` placeholders). When `DATABASE_BACKEND == "mysql"`, `db_backend.translate_mysql_sql` rewrites these on the fly. Consequences for new SQL:
- Stick to the translated subset. Anything outside it (e.g. `PRAGMA`, SQLite-only functions, `RETURNING`) silently breaks MySQL.
- MySQL rows come back as dicts (`DictCursor`), SQLite rows as `sqlite3.Row`; code does `dict(row)` and `row["col"]` which works on both. Don't index rows by position.
- Schema changes go in two places: the SQLite `CREATE TABLE` script in `init_db()` and `MYSQL_SCHEMA`, plus an `_add_column(...)` call in `_init_mysql_db()` for additive migration on existing MySQL deployments. Migrations must be additive and idempotent.
- Always `get_conn()` per operation and `close()` in `finally`. Never share SQLite connections; MySQL connections return to the pool on close.

### Night bucketing

`get_night_id(device_id, ts)` in `database.py` is the single source of truth. A night runs 18:00 to next-day 12:00 in `Asia/Shanghai` (env-configurable); timestamps 12:00–18:00 get an empty `night_id` and are stored for audit only. The server always overrides any client-supplied `night_id`, per event, even inside a batch. `night_id` format is `YYYYMMDD_<device_id>`.

### Event → summary → report pipeline

`events` is the authoritative table (UNIQUE on `device_id, timestamp, event_type` gives idempotent ingest). `compute_daily_summary` groups by `round_in_night` and is recomputed on relevant event inserts and before every morning report. `has_data=false` means no events, and must not be presented as "a quiet night". `model_version` containing `simulator`/`demo`/`mock` marks data as simulated and reports surface `data_source` accordingly; real devices use e.g. `rule_v1`.

### Config versioning and safety limits

Each device has `agent_config` + `config_version`. Candidates record `expected_config_version`; approval fails on mismatch (optimistic concurrency). Approval also takes an `idempotency_key` written to `application_logs`. The hard safety limits (`max_rounds_per_night ≤ 15`, `max_vibration_duration_ms ≤ 300000`, `start_vibration_level ≤ 3`, `max_vibration_level 1–5`, `cooldown_seconds 60–1800`) are duplicated in `api/agent.py:update_config`, `agent/candidates.py:approve_candidate`, and `agent/bandit.py:SAFETY_LIMITS`. Keep all three in sync when touching them; LLM suggestions are also clamped through `ground_ai_interpretation`.

### LLM usage

LLM is optional. If `LLM_API_KEY` is empty everything is deterministic rule-based and never touches the network. Only `/ai/interpretation` and `/ai/chat` and `morning_report?generate_ai=true&cached=false` call the model; `cached=true` uses stored interpretation or rule fallback. All calls go through `request_llm_chat` with a hard overall deadline and must fall back on any error without leaking provider details. Volcengine Ark Coding Plan quirk: base URL `/api/coding/v1` is normalized to `/v3`; `/chat/completions` is appended if missing. Generated copy has compliance wording constraints (no "monitor/treat/diagnose/AHI" style language; see the prompt in `ai_interpretation.py`).

### In-memory state (lost on restart, per-process)

`_agents` (live SnoozMateAgent per device), `_morning_report_cache`, `_DEMO_STATES`, `_SETTINGS_COMMANDS` in `api/agent.py`. These are demo/debug conveniences; anything that must survive restarts or multiple Cloud Hosting workers belongs in the DB.

### Auth

Bearer `session_token` in `Authorization` header, resolved by `_get_user_from_token`. Login codes starting with `test_code_` map to deterministic test users. Without WeChat credentials, identity falls back to the mini program's persistent `client_id`.

## Conventions

- Comments, docstrings, and API messages are largely Chinese; keep that style in user-facing strings. Phase numbers ①–⑤ in docstrings refer to the product sequence diagram (login/bind → bedtime setup → night guard → morning interpretation → user confirmation).
- Sound/light endpoints store *desired* state only; they do not imply the hardware acted.
- Blocking DB calls inside `async` routes are wrapped with `run_in_threadpool`; follow that pattern in new async handlers.
- `MAX_QUERY_LIMIT = 500` caps list queries; batch ingest caps at 500 events.
