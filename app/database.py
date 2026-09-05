"""SQLite/MySQL persistence with additive migrations and bounded connection waits."""
import json
import os
import sqlite3
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from app.config import (
    DATABASE_BACKEND,
    DB_BUSY_TIMEOUT_MS,
    DB_CHARSET,
    DB_CONNECT_TIMEOUT_SECONDS,
    DB_HOST,
    DB_NAME,
    DB_PASSWORD,
    DB_POOL_SIZE,
    DB_PATH,
    DB_PORT,
    DB_READ_TIMEOUT_SECONDS,
    DB_USER,
    DB_WRITE_TIMEOUT_SECONDS,
    DEFAULT_AGENT_CONFIG,
    NIGHT_END_HOUR,
    NIGHT_START_HOUR,
    NIGHT_TIMEZONE,
)
from app.db_backend import MySQLConnection, MySQLPool

MAX_QUERY_LIMIT = 500
_mysql_pool = None
NIGHT_TZ = ZoneInfo(NIGHT_TIMEZONE)


def _now() -> int:
    return int(datetime.now().timestamp())


def _json_dict(value) -> dict:
    try:
        parsed = json.loads(value) if value else {}
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def get_conn():
    """Return a short-lived connection for the configured persistence backend."""
    if DATABASE_BACKEND == "mysql":
        try:
            import pymysql
        except ImportError as error:
            raise RuntimeError(
                "MySQL backend requires PyMySQL; install requirements.txt"
            ) from error
        if not all((DB_HOST, DB_USER, DB_NAME)):
            raise RuntimeError("DB_HOST, DB_USER and DB_NAME are required for MySQL")
        global _mysql_pool
        if _mysql_pool is None:
            def connect():
                return pymysql.connect(
                    host=DB_HOST,
                    port=DB_PORT,
                    user=DB_USER,
                    password=DB_PASSWORD,
                    database=DB_NAME,
                    charset=DB_CHARSET,
                    cursorclass=pymysql.cursors.DictCursor,
                    autocommit=False,
                    connect_timeout=max(1, int(DB_CONNECT_TIMEOUT_SECONDS)),
                    read_timeout=max(1, int(DB_READ_TIMEOUT_SECONDS)),
                    write_timeout=max(1, int(DB_WRITE_TIMEOUT_SECONDS)),
                )
            _mysql_pool = MySQLPool(connect, DB_POOL_SIZE)
        return MySQLConnection(_mysql_pool.acquire(), _mysql_pool)

    # Never share SQLite connections across requests.
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=DB_BUSY_TIMEOUT_MS / 1000, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={DB_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _add_column(conn, table: str, column: str, declaration: str):
    if DATABASE_BACKEND == "mysql":
        existing = {
            r["name"]
            for r in conn.execute(
                """SELECT COLUMN_NAME AS name
                   FROM information_schema.columns
                   WHERE table_schema=? AND table_name=?""",
                (DB_NAME, table),
            ).fetchall()
        }
    else:
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
        return True
    return False


MYSQL_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
 id BIGINT NOT NULL AUTO_INCREMENT,
 user_id VARCHAR(128) NOT NULL,
 openid VARCHAR(255) NOT NULL DEFAULT '',
 nickname VARCHAR(255) NOT NULL DEFAULT '',
 avatar VARCHAR(512) NOT NULL DEFAULT '',
 sleep_mode VARCHAR(32) NOT NULL DEFAULT 'shared',
 privacy_accepted TINYINT NOT NULL DEFAULT 0,
 ai_data_authorized TINYINT NOT NULL DEFAULT 0,
 ai_consent_version VARCHAR(64) NOT NULL DEFAULT '',
 session_token VARCHAR(255) NOT NULL DEFAULT '',
 session_expires_at BIGINT NOT NULL DEFAULT 0,
 created_at BIGINT NOT NULL DEFAULT 0,
 updated_at BIGINT NOT NULL DEFAULT 0,
 PRIMARY KEY (id),
 UNIQUE KEY uq_users_user_id (user_id),
 KEY idx_users_session_token (session_token),
 KEY idx_users_openid (openid)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS user_sessions (
 id BIGINT NOT NULL AUTO_INCREMENT,
 session_token VARCHAR(255) NOT NULL,
 user_id VARCHAR(128) NOT NULL,
 session_expires_at BIGINT NOT NULL DEFAULT 0,
 created_at BIGINT NOT NULL DEFAULT 0,
 revoked TINYINT NOT NULL DEFAULT 0,
 PRIMARY KEY (id),
 UNIQUE KEY uq_sessions_token (session_token),
 KEY idx_sessions_user_expiry (user_id, session_expires_at),
 KEY idx_sessions_token_expiry (session_token, session_expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS device_bindings (
 id BIGINT NOT NULL AUTO_INCREMENT,
 user_id VARCHAR(128) NOT NULL,
 device_id VARCHAR(128) NOT NULL,
 role VARCHAR(32) NOT NULL DEFAULT 'owner',
 binding_token VARCHAR(255) NOT NULL DEFAULT '',
 status VARCHAR(32) NOT NULL DEFAULT 'active',
 created_at BIGINT NOT NULL DEFAULT 0,
 PRIMARY KEY (id),
 UNIQUE KEY uq_binding_user_device (user_id, device_id),
 KEY idx_bindings_user_status (user_id, status),
 KEY idx_bindings_token (binding_token)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS devices (
 id BIGINT NOT NULL AUTO_INCREMENT,
 device_id VARCHAR(128) NOT NULL,
 name VARCHAR(255) NOT NULL DEFAULT '月石主机',
 mode VARCHAR(32) NOT NULL DEFAULT 'solo',
 agent_config LONGTEXT NULL,
 config_version INT NOT NULL DEFAULT 1,
 firmware_version VARCHAR(64) NOT NULL DEFAULT 'v1.0',
 last_online BIGINT NOT NULL DEFAULT 0,
 last_night_id VARCHAR(256) NOT NULL DEFAULT '',
 created_at BIGINT NOT NULL DEFAULT 0,
 PRIMARY KEY (id),
 UNIQUE KEY uq_devices_device_id (device_id),
 KEY idx_devices_last_online (last_online)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS events (
 id BIGINT NOT NULL AUTO_INCREMENT,
 device_id VARCHAR(128) NOT NULL,
 night_id VARCHAR(256) NOT NULL DEFAULT '',
 timestamp BIGINT NOT NULL,
 event_type VARCHAR(64) NOT NULL,
 snore_duration_sec DOUBLE NOT NULL DEFAULT 0,
 snore_confidence DOUBLE NOT NULL DEFAULT 0,
 in_bed TINYINT NOT NULL DEFAULT 1,
 body_motion_level DOUBLE NOT NULL DEFAULT 0,
 vibration_level INT NOT NULL DEFAULT 0,
 vibration_duration_ms INT NOT NULL DEFAULT 0,
 result VARCHAR(64) NOT NULL DEFAULT '',
 response_time_sec DOUBLE NOT NULL DEFAULT 0,
 round_in_night INT NOT NULL DEFAULT 0,
 model_version VARCHAR(128) NOT NULL DEFAULT 'rule_v1',
 error_code INT NOT NULL DEFAULT 0,
 note VARCHAR(1000) NOT NULL DEFAULT '',
 PRIMARY KEY (id),
 UNIQUE KEY uq_events_device_time_type (device_id, timestamp, event_type),
 KEY idx_events_device_time (device_id, timestamp),
 KEY idx_events_device_night_time (device_id, night_id, timestamp)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS daily_summaries (
 id BIGINT NOT NULL AUTO_INCREMENT,
 device_id VARCHAR(128) NOT NULL,
 night_id VARCHAR(256) NOT NULL,
 date VARCHAR(16) NOT NULL,
 total_rounds INT NOT NULL DEFAULT 0,
 success_rounds INT NOT NULL DEFAULT 0,
 event_count INT NOT NULL DEFAULT 0,
 avg_response_time DOUBLE NOT NULL DEFAULT 0,
 peak_hour VARCHAR(8) NOT NULL DEFAULT '',
 max_vibration_level INT NOT NULL DEFAULT 0,
 ai_summary TEXT NULL,
 ai_basis TEXT NULL,
 ai_suggestion TEXT NULL,
 ai_trend VARCHAR(32) NOT NULL DEFAULT '',
 created_at BIGINT NOT NULL DEFAULT 0,
 ai_source VARCHAR(64) NOT NULL DEFAULT '',
 ai_generated_at BIGINT NOT NULL DEFAULT 0,
 PRIMARY KEY (id),
 UNIQUE KEY uq_summaries_device_night (device_id, night_id),
 KEY idx_summaries_device_date (device_id, date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS config_candidates (
 id BIGINT NOT NULL AUTO_INCREMENT,
 candidate_id VARCHAR(128) NOT NULL,
 device_id VARCHAR(128) NOT NULL,
 suggested_config LONGTEXT NOT NULL,
 expected_config_version INT NOT NULL DEFAULT 0,
 source VARCHAR(32) NOT NULL DEFAULT 'ai',
 summary VARCHAR(1000) NOT NULL DEFAULT '',
 basis TEXT NULL,
 status VARCHAR(32) NOT NULL DEFAULT 'pending',
 created_at BIGINT NOT NULL DEFAULT 0,
 reviewed_at BIGINT NOT NULL DEFAULT 0,
 applied_at BIGINT NOT NULL DEFAULT 0,
 PRIMARY KEY (id),
 UNIQUE KEY uq_candidates_candidate_id (candidate_id),
 KEY idx_candidates_device_status_time (device_id, status, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS application_logs (
 id BIGINT NOT NULL AUTO_INCREMENT,
 idempotency_key VARCHAR(255) NOT NULL,
 device_id VARCHAR(128) NOT NULL,
 candidate_id VARCHAR(128) NOT NULL DEFAULT '',
 config_before LONGTEXT NULL,
 config_after LONGTEXT NULL,
 status VARCHAR(32) NOT NULL DEFAULT 'pending',
 error_msg VARCHAR(1000) NOT NULL DEFAULT '',
 created_at BIGINT NOT NULL DEFAULT 0,
 completed_at BIGINT NOT NULL DEFAULT 0,
 PRIMARY KEY (id),
 UNIQUE KEY uq_application_idempotency (idempotency_key),
 KEY idx_application_device_time (device_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS morning_feedback (
 id BIGINT NOT NULL AUTO_INCREMENT,
 device_id VARCHAR(128) NOT NULL,
 user_id VARCHAR(128) NOT NULL DEFAULT '',
 night_id VARCHAR(256) NOT NULL,
 was_disturbed TINYINT NOT NULL DEFAULT 0,
 morning_feeling INT NOT NULL DEFAULT 3,
 partner_affected TINYINT NULL,
 comment VARCHAR(1000) NOT NULL DEFAULT '',
 created_at BIGINT NOT NULL DEFAULT 0,
 PRIMARY KEY (id),
 UNIQUE KEY uq_feedback_device_night (device_id, night_id),
 KEY idx_feedback_device_time (device_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
"""


def _init_mysql_db():
    conn = get_conn()
    try:
        conn.executescript(MYSQL_SCHEMA)
        _add_column(conn, "users", "sleep_mode", "VARCHAR(32) NOT NULL DEFAULT 'shared'")
        _add_column(conn, "users", "privacy_accepted", "TINYINT NOT NULL DEFAULT 0")
        _add_column(conn, "users", "ai_data_authorized", "TINYINT NOT NULL DEFAULT 0")
        _add_column(conn, "users", "ai_consent_version", "VARCHAR(64) NOT NULL DEFAULT ''")
        _add_column(conn, "users", "session_expires_at", "BIGINT NOT NULL DEFAULT 0")
        _add_column(conn, "users", "updated_at", "BIGINT NOT NULL DEFAULT 0")
        _add_column(conn, "devices", "config_version", "INT NOT NULL DEFAULT 1")
        _add_column(conn, "events", "night_id", "VARCHAR(256) NOT NULL DEFAULT ''")
        _add_column(conn, "daily_summaries", "event_count", "INT NOT NULL DEFAULT 0")
        _add_column(conn, "daily_summaries", "ai_source", "VARCHAR(64) NOT NULL DEFAULT ''")
        _add_column(conn, "daily_summaries", "ai_generated_at", "BIGINT NOT NULL DEFAULT 0")
        _add_column(conn, "morning_feedback", "partner_affected", "TINYINT NULL")
        conn.commit()
    finally:
        conn.close()


def init_db():
    """Create the schema and apply only additive, idempotent migrations."""
    if DATABASE_BACKEND == "mysql":
        _init_mysql_db()
        repair_event_night_classification()
        return
    conn = get_conn()
    try:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT UNIQUE NOT NULL,
            openid TEXT DEFAULT '', nickname TEXT DEFAULT '', avatar TEXT DEFAULT '',
            sleep_mode TEXT DEFAULT 'shared', privacy_accepted INTEGER DEFAULT 0,
            ai_data_authorized INTEGER DEFAULT 0, ai_consent_version TEXT DEFAULT '',
            session_token TEXT DEFAULT '', session_expires_at INTEGER DEFAULT 0,
            created_at INTEGER DEFAULT 0, updated_at INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS user_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_token TEXT UNIQUE NOT NULL,
            user_id TEXT NOT NULL,
            session_expires_at INTEGER DEFAULT 0,
            created_at INTEGER DEFAULT 0,
            revoked INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS device_bindings (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, device_id TEXT NOT NULL,
            role TEXT DEFAULT 'owner', binding_token TEXT DEFAULT '', status TEXT DEFAULT 'active',
            created_at INTEGER DEFAULT 0, UNIQUE(user_id, device_id)
        );
        CREATE TABLE IF NOT EXISTS devices (
            id INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT UNIQUE NOT NULL,
            name TEXT DEFAULT '月石主机', mode TEXT DEFAULT 'solo', agent_config TEXT,
            config_version INTEGER DEFAULT 1, firmware_version TEXT DEFAULT 'v1.0',
            last_online INTEGER DEFAULT 0, last_night_id TEXT DEFAULT '', created_at INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT NOT NULL, night_id TEXT DEFAULT '',
            timestamp INTEGER NOT NULL, event_type TEXT NOT NULL, snore_duration_sec REAL DEFAULT 0,
            snore_confidence REAL DEFAULT 0, in_bed INTEGER DEFAULT 1, body_motion_level REAL DEFAULT 0,
            vibration_level INTEGER DEFAULT 0, vibration_duration_ms INTEGER DEFAULT 0, result TEXT DEFAULT '',
            response_time_sec REAL DEFAULT 0, round_in_night INTEGER DEFAULT 0,
            model_version TEXT DEFAULT 'rule_v1', error_code INTEGER DEFAULT 0, note TEXT DEFAULT '',
            UNIQUE(device_id, timestamp, event_type)
        );
        CREATE TABLE IF NOT EXISTS daily_summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT NOT NULL, night_id TEXT NOT NULL,
            date TEXT NOT NULL, total_rounds INTEGER DEFAULT 0, success_rounds INTEGER DEFAULT 0,
            event_count INTEGER DEFAULT 0,
            avg_response_time REAL DEFAULT 0, peak_hour TEXT DEFAULT '', max_vibration_level INTEGER DEFAULT 0,
            ai_summary TEXT DEFAULT '', ai_basis TEXT DEFAULT '', ai_suggestion TEXT DEFAULT '',
            ai_trend TEXT DEFAULT '', created_at INTEGER DEFAULT 0, UNIQUE(device_id, night_id)
        );
        CREATE TABLE IF NOT EXISTS config_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT, candidate_id TEXT UNIQUE NOT NULL, device_id TEXT NOT NULL,
            suggested_config TEXT NOT NULL, expected_config_version INTEGER DEFAULT 0, source TEXT DEFAULT 'ai',
            summary TEXT DEFAULT '', basis TEXT DEFAULT '', status TEXT DEFAULT 'pending',
            created_at INTEGER DEFAULT 0, reviewed_at INTEGER DEFAULT 0, applied_at INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS application_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, idempotency_key TEXT UNIQUE NOT NULL, device_id TEXT NOT NULL,
            candidate_id TEXT DEFAULT '', config_before TEXT DEFAULT '', config_after TEXT DEFAULT '',
            status TEXT DEFAULT 'pending', error_msg TEXT DEFAULT '', created_at INTEGER DEFAULT 0,
            completed_at INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS morning_feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT NOT NULL, user_id TEXT DEFAULT '',
            night_id TEXT NOT NULL, was_disturbed INTEGER DEFAULT 0, morning_feeling INTEGER DEFAULT 3,
            partner_affected INTEGER DEFAULT NULL, comment TEXT DEFAULT '', created_at INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_events_device_time ON events(device_id, timestamp DESC);
        CREATE INDEX IF NOT EXISTS idx_events_device_night_time ON events(device_id, night_id, timestamp);
        CREATE INDEX IF NOT EXISTS idx_summaries_device_date ON daily_summaries(device_id, date DESC);
        CREATE INDEX IF NOT EXISTS idx_bindings_user_status ON device_bindings(user_id, status);
        CREATE INDEX IF NOT EXISTS idx_users_session_token ON users(session_token);
        CREATE INDEX IF NOT EXISTS idx_sessions_user_expiry ON user_sessions(user_id, session_expires_at);
        CREATE INDEX IF NOT EXISTS idx_candidates_device_status_time ON config_candidates(device_id, status, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_feedback_device_time ON morning_feedback(device_id, created_at DESC);
        """)
        conn.execute("PRAGMA journal_mode=WAL")
        # Safe for databases created by earlier builds.
        _add_column(conn, "devices", "config_version", "INTEGER DEFAULT 1")
        _add_column(conn, "events", "night_id", "TEXT DEFAULT ''")
        _add_column(conn, "users", "sleep_mode", "TEXT DEFAULT 'shared'")
        _add_column(conn, "users", "privacy_accepted", "INTEGER DEFAULT 0")
        _add_column(conn, "users", "ai_data_authorized", "INTEGER DEFAULT 0")
        _add_column(conn, "users", "ai_consent_version", "TEXT DEFAULT ''")
        _add_column(conn, "users", "session_expires_at", "INTEGER DEFAULT 0")
        _add_column(conn, "users", "updated_at", "INTEGER DEFAULT 0")
        _add_column(conn, "morning_feedback", "partner_affected", "INTEGER DEFAULT NULL")
        added_event_count = _add_column(conn, "daily_summaries", "event_count", "INTEGER DEFAULT 0")
        _add_column(conn, "daily_summaries", "ai_source", "TEXT DEFAULT ''")
        _add_column(conn, "daily_summaries", "ai_generated_at", "INTEGER DEFAULT 0")
        if added_event_count:
            conn.execute(
                """UPDATE daily_summaries
                   SET event_count=(
                     SELECT COUNT(*) FROM events
                     WHERE events.device_id=daily_summaries.device_id
                       AND events.night_id=daily_summaries.night_id
                   )"""
            )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_users_openid ON users(openid)")
        conn.commit()
    finally:
        conn.close()
    repair_event_night_classification()


def gen_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def upsert_user_session(user_id: str, openid: str, session_token: str, expires_at: int) -> dict:
    now = _now()
    conn = get_conn()
    try:
        conn.execute(
            """INSERT INTO users(user_id,openid,nickname,session_token,session_expires_at,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET
                 openid=excluded.openid,
                 session_token=excluded.session_token,
                 session_expires_at=excluded.session_expires_at,
                 updated_at=excluded.updated_at""",
            (user_id, openid, "月石用户", session_token, expires_at, now, now),
        )
        conn.execute(
            """INSERT OR IGNORE INTO user_sessions
               (session_token,user_id,session_expires_at,created_at,revoked)
               VALUES(?,?,?,?,0)""",
            (session_token, user_id, expires_at, now),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        return dict(row)
    finally:
        conn.close()


def get_user_by_token(session_token: str) -> dict:
    if not session_token:
        return {}
    conn = get_conn()
    try:
        row = conn.execute(
            """SELECT u.* FROM user_sessions s
               JOIN users u ON u.user_id=s.user_id
               WHERE s.session_token=? AND s.session_expires_at>? AND s.revoked=0""",
            (session_token, _now()),
        ).fetchone()
        if not row:
            # Backward compatibility for databases created before user_sessions.
            row = conn.execute(
                "SELECT * FROM users WHERE session_token=? AND session_expires_at>?",
                (session_token, _now()),
            ).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


def get_user_profile(user_id: str) -> dict:
    conn = get_conn()
    try:
        row = conn.execute(
            """SELECT user_id,nickname,sleep_mode,privacy_accepted,
                      ai_data_authorized,ai_consent_version
               FROM users WHERE user_id=?""",
            (user_id,),
        ).fetchone()
        if not row:
            return {}
        return {
            "id": row["user_id"],
            "nickname": row["nickname"] or "月石用户",
            "sleepMode": row["sleep_mode"] or "shared",
            "privacyAccepted": bool(row["privacy_accepted"]),
            "aiDataAuthorized": bool(row["ai_data_authorized"]),
            "aiConsentVersion": row["ai_consent_version"] or "",
        }
    finally:
        conn.close()


def update_user_profile(user_id: str, patch: dict) -> dict:
    """Patch a profile without resetting fields omitted by the client."""
    columns = {
        "nickname": "nickname",
        "sleepMode": "sleep_mode",
        "privacyAccepted": "privacy_accepted",
        "aiDataAuthorized": "ai_data_authorized",
        "aiConsentVersion": "ai_consent_version",
    }
    updates, values = [], []
    for key, column in columns.items():
        if key not in patch:
            continue
        value = patch[key]
        if key in {"privacyAccepted", "aiDataAuthorized"}:
            value = int(bool(value))
        updates.append(f"{column}=?")
        values.append(value)
    if updates:
        updates.append("updated_at=?")
        values.extend((_now(), user_id))
        conn = get_conn()
        try:
            result = conn.execute(
                f"UPDATE users SET {','.join(updates)} WHERE user_id=?",
                values,
            )
            if result.rowcount != 1:
                raise ValueError("user not found")
            conn.commit()
        finally:
            conn.close()
    return get_user_profile(user_id)


def get_night_id(device_id: str, ts: int = None) -> str:
    """Return the night bucket for a timestamp, or empty outside night hours."""
    if not device_id:
        return ""
    dt = datetime.fromtimestamp(ts if ts is not None else _now(), NIGHT_TZ)
    if dt.hour >= NIGHT_START_HOUR:
        night_date = dt.date()
    elif dt.hour < NIGHT_END_HOUR:
        night_date = dt.date() - timedelta(days=1)
    else:
        return ""
    return f"{night_date:%Y%m%d}_{device_id}"


def repair_event_night_classification() -> dict:
    """Reclassify legacy events with the bounded night window.

    Older releases assigned every timestamp after 05:00 to a night. Correct
    those rows in place, remove orphaned summaries, and refresh summaries for
    buckets whose membership changed.
    """
    conn = get_conn()
    updates = []
    impacted = set()
    try:
        rows = conn.execute(
            "SELECT id,device_id,timestamp,night_id FROM events"
        ).fetchall()
        for row in rows:
            expected = get_night_id(row["device_id"], row["timestamp"])
            actual = row["night_id"] or ""
            if actual == expected:
                continue
            if actual:
                impacted.add((row["device_id"], actual))
            if expected:
                impacted.add((row["device_id"], expected))
            updates.append((expected, row["id"]))

        for night_id, event_id in updates:
            conn.execute("UPDATE events SET night_id=? WHERE id=?", (night_id, event_id))

        conn.execute(
            """DELETE FROM daily_summaries
               WHERE NOT EXISTS (
                 SELECT 1 FROM events
                 WHERE events.device_id=daily_summaries.device_id
                   AND events.night_id=daily_summaries.night_id
               )"""
        )
        conn.commit()
    finally:
        conn.close()

    for device_id, night_id in impacted:
        if night_id:
            compute_daily_summary(device_id, night_id)
    return {"reclassified": len(updates), "impacted_nights": len(impacted)}


def get_latest_night_id(device_id: str) -> str:
    """Return the newest night that actually contains a device event."""
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT night_id,timestamp FROM events
               WHERE device_id=? AND night_id<>''
               ORDER BY timestamp DESC LIMIT ?""",
            (device_id, MAX_QUERY_LIMIT),
        ).fetchall()
        for row in rows:
            if get_night_id(device_id, row["timestamp"]) == row["night_id"]:
                return row["night_id"]
        return ""
    finally:
        conn.close()


def _config_from_row(row) -> dict:
    cfg = dict(DEFAULT_AGENT_CONFIG)
    if row:
        cfg.update(_json_dict(row["agent_config"]))
    return cfg


def upsert_device(device_id: str, **kwargs):
    if not isinstance(device_id, str) or not device_id.strip():
        raise ValueError("device_id is required")
    allowed = {"name", "mode", "agent_config", "config_version", "firmware_version", "last_online", "last_night_id"}
    unknown = set(kwargs) - allowed
    if unknown:
        raise ValueError("invalid device field")
    conn = get_conn()
    try:
        conn.execute("""INSERT INTO devices(device_id, agent_config, config_version, created_at)
                     VALUES(?,?,?,?) ON CONFLICT(device_id) DO NOTHING""",
                     (device_id, json.dumps(DEFAULT_AGENT_CONFIG), 1, _now()))
        for key, value in kwargs.items():
            if key == "agent_config" and isinstance(value, dict):
                value = json.dumps(value, ensure_ascii=False)
            conn.execute(f"UPDATE devices SET {key}=? WHERE device_id=?", (value, device_id))
        conn.commit()
    finally:
        conn.close()


def get_device(device_id: str) -> dict:
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM devices WHERE device_id=?", (device_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_device_config(device_id: str) -> dict:
    return _config_from_row(get_device(device_id))


def update_device_config(device_id: str, new_config: dict) -> int:
    """Patch config rather than replacing it, preserving desired sound/light state."""
    if not isinstance(new_config, dict):
        raise ValueError("config must be an object")
    conn = get_conn()
    try:
        conn.execute("""INSERT INTO devices(device_id, agent_config, config_version, created_at)
                     VALUES(?,?,?,?) ON CONFLICT(device_id) DO NOTHING""",
                     (device_id, json.dumps(DEFAULT_AGENT_CONFIG), 1, _now()))
        row = conn.execute("SELECT agent_config, config_version FROM devices WHERE device_id=?", (device_id,)).fetchone()
        merged = _config_from_row(row)
        merged.update(new_config)
        version = int(row["config_version"] or 1) + 1
        conn.execute("UPDATE devices SET agent_config=?, config_version=? WHERE device_id=?",
                     (json.dumps(merged, ensure_ascii=False), version, device_id))
        conn.commit()
        return version
    finally:
        conn.close()


def insert_event(event: dict) -> int:
    event = dict(event)
    if not event.get("device_id"):
        raise ValueError("device_id is required")
    event["timestamp"] = int(event.get("timestamp") or _now())
    # The server is authoritative for night classification. A client-provided
    # bucket must not let a daytime diagnostic enter a sleep report.
    event["night_id"] = get_night_id(event["device_id"], event["timestamp"])
    allowed = {"device_id", "night_id", "timestamp", "event_type", "snore_duration_sec", "snore_confidence",
               "in_bed", "body_motion_level", "vibration_level", "vibration_duration_ms", "result",
               "response_time_sec", "round_in_night", "model_version", "error_code", "note"}
    values = {k: event[k] for k in event if k in allowed}
    conn = get_conn()
    try:
        keys = list(values)
        cur = conn.execute(f"INSERT OR IGNORE INTO events ({','.join(keys)}) VALUES ({','.join('?' for _ in keys)})",
                           [values[k] for k in keys])
        conn.commit()
        return cur.lastrowid if cur.rowcount == 1 else 0
    finally:
        conn.close()


def insert_events_batch(events: list) -> tuple[int, int, set]:
    """Insert a batch in one transaction and derive each missing night from its timestamp."""
    inserted = duplicates = 0
    night_ids = set()
    conn = get_conn()
    allowed = {"device_id", "night_id", "timestamp", "event_type", "snore_duration_sec", "snore_confidence",
               "in_bed", "body_motion_level", "vibration_level", "vibration_duration_ms", "result",
               "response_time_sec", "round_in_night", "model_version", "error_code", "note"}
    try:
        for raw in events:
            event = dict(raw)
            event["timestamp"] = int(event.get("timestamp") or _now())
            # Derive per event so a batch cannot force daytime records into a
            # night bucket or merge events across the 12:00/18:00 boundary.
            event["night_id"] = get_night_id(event["device_id"], event["timestamp"])
            if event["night_id"]:
                night_ids.add(event["night_id"])
            values = {k: event[k] for k in event if k in allowed}
            keys = list(values)
            cur = conn.execute(f"INSERT OR IGNORE INTO events ({','.join(keys)}) VALUES ({','.join('?' for _ in keys)})",
                               [values[k] for k in keys])
            if cur.rowcount == 1:
                inserted += 1
            else:
                duplicates += 1
        conn.commit()
        return inserted, duplicates, night_ids
    finally:
        conn.close()


def _limit(value: int, default: int) -> int:
    return max(1, min(int(value or default), MAX_QUERY_LIMIT))


def get_night_events(device_id: str, night_id: str, limit: int = MAX_QUERY_LIMIT) -> list:
    if not night_id:
        return []
    conn = get_conn()
    try:
        rows = conn.execute("""SELECT * FROM events WHERE device_id=? AND night_id=?
                            ORDER BY timestamp DESC LIMIT ?""", (device_id, night_id, _limit(limit, MAX_QUERY_LIMIT))).fetchall()
        return list(reversed([dict(r) for r in rows]))
    finally:
        conn.close()


def get_recent_events(device_id: str, limit: int = 50, night_id: str = "") -> list:
    conn = get_conn()
    try:
        if night_id:
            rows = conn.execute("""SELECT * FROM events WHERE device_id=? AND night_id=?
                                ORDER BY timestamp DESC LIMIT ?""", (device_id, night_id, _limit(limit, 50))).fetchall()
        else:
            rows = conn.execute("SELECT * FROM events WHERE device_id=? AND night_id<>'' ORDER BY timestamp DESC LIMIT ?",
                                (device_id, _limit(limit, 50))).fetchall()
        return list(reversed([dict(r) for r in rows]))
    finally:
        conn.close()


def get_latest_event_metadata(device_id: str) -> dict:
    conn = get_conn()
    try:
        row = conn.execute(
            """SELECT timestamp,night_id,model_version FROM events
               WHERE device_id=? AND night_id<>'' ORDER BY timestamp DESC LIMIT 1""",
            (device_id,),
        ).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


def compute_daily_summary(device_id: str, night_id: str) -> dict:
    if not night_id:
        return {
            "device_id": device_id,
            "night_id": "",
            "date": "",
            "total_rounds": 0,
            "success_rounds": 0,
            "event_count": 0,
            "avg_response_time": 0,
            "peak_hour": "",
            "max_vibration_level": 0,
        }
    conn = get_conn()
    try:
        events = [
            dict(row)
            for row in conn.execute(
                """SELECT timestamp,event_type,vibration_level,result,response_time_sec,round_in_night
                   FROM events WHERE device_id=? AND night_id=? ORDER BY timestamp ASC""",
                (device_id, night_id),
            ).fetchall()
        ]
    finally:
        conn.close()
    rounds = {}
    for event in events:
        round_no = event.get("round_in_night") or 0
        if not round_no:
            continue
        state = rounds.setdefault(round_no, {"success": False, "max_level": 0, "response_time": 0})
        state["max_level"] = max(state["max_level"], event.get("vibration_level") or 0)
        if event.get("result") == "success" and event.get("event_type") in {"vibration_stop", "intervention", "position_change"}:
            state["success"] = True
            state["response_time"] = max(state["response_time"], event.get("response_time_sec") or 0)
    total, success = len(rounds), sum(r["success"] for r in rounds.values())
    response = [r["response_time"] for r in rounds.values() if r["response_time"]]
    hours = {}
    for event in events:
        if event.get("round_in_night") and event.get("timestamp"):
            hour = datetime.fromtimestamp(event["timestamp"]).hour
            hours[hour] = hours.get(hour, 0) + 1
    result = {"device_id": device_id, "night_id": night_id, "date": night_id.split("_")[0],
              "total_rounds": total, "success_rounds": success,
              "event_count": len(events),
              "avg_response_time": round(sum(response) / len(response), 1) if response else 0,
              "peak_hour": str(max(hours, key=hours.get)) if hours else "",
              "max_vibration_level": max((r["max_level"] for r in rounds.values()), default=0)}
    if not events:
        return result
    conn = get_conn()
    try:
        conn.execute("""INSERT INTO daily_summaries(device_id,night_id,date,total_rounds,success_rounds,event_count,
                    avg_response_time,peak_hour,max_vibration_level,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(device_id,night_id) DO UPDATE SET date=excluded.date,
                    total_rounds=excluded.total_rounds,success_rounds=excluded.success_rounds,
                    event_count=excluded.event_count,
                    avg_response_time=excluded.avg_response_time,peak_hour=excluded.peak_hour,
                    max_vibration_level=excluded.max_vibration_level,created_at=excluded.created_at""",
                    (
                        result["device_id"], result["night_id"], result["date"],
                        result["total_rounds"], result["success_rounds"], result["event_count"],
                        result["avg_response_time"], result["peak_hour"],
                        result["max_vibration_level"], _now(),
                    ))
        conn.commit()
    finally:
        conn.close()
    return result


def get_daily_summary(device_id: str, night_id: str) -> dict:
    if not night_id:
        return {}
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM daily_summaries WHERE device_id=? AND night_id=?", (device_id, night_id)).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


def save_ai_interpretation(device_id: str, night_id: str, interpretation: dict):
    conn = get_conn()
    try:
        conn.execute(
            """UPDATE daily_summaries
               SET ai_summary=?, ai_basis=?, ai_suggestion=?, ai_trend=?,
                   ai_source=?, ai_generated_at=?
               WHERE device_id=? AND night_id=?""",
            (
                interpretation.get("summary", ""),
                json.dumps(interpretation.get("basis", []), ensure_ascii=False),
                interpretation.get("tonight_suggestion", ""),
                interpretation.get("trend_label", ""),
                interpretation.get("source", ""),
                _now(),
                device_id,
                night_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_saved_ai_interpretation(device_id: str, night_id: str) -> dict:
    conn = get_conn()
    try:
        row = conn.execute(
            """SELECT ai_summary,ai_basis,ai_suggestion,ai_trend,ai_source,ai_generated_at
               FROM daily_summaries WHERE device_id=? AND night_id=?""",
            (device_id, night_id),
        ).fetchone()
    finally:
        conn.close()
    if not row or not row["ai_summary"]:
        return {}
    try:
        basis = json.loads(row["ai_basis"] or "[]")
    except (TypeError, ValueError):
        basis = []
    return {
        "summary": row["ai_summary"],
        "basis": basis if isinstance(basis, list) else [],
        "tonight_suggestion": row["ai_suggestion"] or "",
        "trend_label": row["ai_trend"] or "stable",
        "config_suggestion": {
            "relevant": False,
            "summary": "保持当前参数即可",
            "params_to_adjust": {},
            "reason": "已保存的解读不自动修改设备参数",
        },
        "source": row["ai_source"] or "stored",
        "generated_at": row["ai_generated_at"] or 0,
    }


def get_weekly_stats(device_id: str, days: int = 7) -> dict:
    conn = get_conn()
    try:
        rows = [dict(r) for r in conn.execute("""SELECT * FROM daily_summaries
                                                WHERE device_id=? AND event_count>0
                                                ORDER BY date DESC LIMIT ?""", (device_id, _limit(days, 7))).fetchall()]
        source = "none"
        night_ids = [row["night_id"] for row in rows if row.get("night_id")]
        if night_ids:
            placeholders = ",".join("?" for _ in night_ids)
            versions = [
                str(row["model_version"] or "").lower()
                for row in conn.execute(
                    f"""SELECT model_version FROM events
                        WHERE device_id=? AND night_id IN ({placeholders})""",
                    (device_id, *night_ids),
                ).fetchall()
            ]
            simulated = sum(
                any(tag in version for tag in ("simulator", "demo", "mock"))
                for version in versions
            )
            source = (
                "none" if not versions else
                "simulated" if simulated == len(versions) else
                "mixed" if simulated else "device"
            )
    finally:
        conn.close()
    if not rows:
        return {"nights": 0, "total_rounds": 0, "success_rate": 0, "avg_rounds_per_night": 0, "trend": "insufficient_data", "data_source": "none", "daily": []}
    total = sum(r["total_rounds"] for r in rows)
    success = sum(r["success_rounds"] for r in rows)
    trend = "insufficient_data"
    if len(rows) >= 4:
        half = len(rows) // 2
        recent = sum(r["success_rounds"] for r in rows[:half]) / max(1, sum(r["total_rounds"] for r in rows[:half]))
        prior = sum(r["success_rounds"] for r in rows[half:]) / max(1, sum(r["total_rounds"] for r in rows[half:]))
        trend = "improving" if recent > prior + .1 else "worsening" if recent < prior - .1 else "stable"
    return {"nights": len(rows), "total_rounds": total, "success_rounds": success,
            "success_rate": round(success / total, 3) if total else 0, "avg_rounds_per_night": round(total / len(rows), 1),
            "trend": trend, "data_source": source, "daily": rows}


SOUND_DEFAULT = {"scene": "sleep", "sceneName": "睡眠", "trackName": "月石雨声", "playing": False,
                 "volume": 32, "timer": 30, "fadeSeconds": 3,
                 "scenes": [
                     {"id": "sleep", "name": "深睡白噪", "trackName": "月石雨声"},
                     {"id": "healing", "name": "疗愈雨声", "trackName": "林间细雨"},
                     {"id": "work", "name": "专注环境音", "trackName": "柔和棕噪"},
                     {"id": "reading", "name": "阅读轻音", "trackName": "纸页与壁炉"},
                 ]}
LIGHT_DEFAULT = {"enabled": True, "mode": "night-low", "modeName": "低亮", "brightness": 25, "color": "amber"}
EXTRA_KEYS = {"sound_state": "_sound_state", "light_state": "_light_state"}


def _get_extra(device_id: str, name: str, default: dict) -> dict:
    cfg = get_device_config(device_id)
    value = cfg.get(EXTRA_KEYS[name])
    return value if isinstance(value, dict) else dict(default)


def get_sound_state(device_id: str) -> dict:
    state = {**SOUND_DEFAULT, **_get_extra(device_id, "sound_state", SOUND_DEFAULT)}
    known = {item.get("id") for item in state.get("scenes", []) if isinstance(item, dict)}
    if known != {item["id"] for item in SOUND_DEFAULT["scenes"]}:
        state["scenes"] = [dict(item) for item in SOUND_DEFAULT["scenes"]]
    return state


def set_sound_state(device_id: str, patch: dict) -> dict:
    state = {**get_sound_state(device_id), **patch}
    if "scene" in patch and "sceneName" not in patch and "trackName" not in patch:
        selected = next((item for item in state["scenes"] if item["id"] == state["scene"]), None)
        if selected:
            state["sceneName"] = selected["name"]
            state["trackName"] = selected["trackName"]
    update_device_config(device_id, {EXTRA_KEYS["sound_state"]: state})
    return state


def get_light_state(device_id: str) -> dict:
    return _get_extra(device_id, "light_state", LIGHT_DEFAULT)


def set_light_state(device_id: str, patch: dict) -> dict:
    state = {**get_light_state(device_id), **patch}
    update_device_config(device_id, {EXTRA_KEYS["light_state"]: state})
    return state


def get_recent_feedback(device_id: str, limit: int = 3) -> list:
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT f.night_id,f.was_disturbed,f.morning_feeling,
                      f.partner_affected,f.comment,f.created_at
               FROM morning_feedback f
               JOIN (
                 SELECT MAX(id) AS id FROM morning_feedback
                 WHERE device_id=? GROUP BY night_id
               ) latest ON latest.id=f.id
               ORDER BY f.created_at DESC, f.id DESC LIMIT ?""",
            (device_id, _limit(limit, 3)),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def database_diagnostics() -> dict:
    if DATABASE_BACKEND == "mysql":
        configured = bool(DB_HOST and DB_USER and DB_NAME)
        return {
            "backend": "mysql",
            "database": DB_NAME,
            "host": DB_HOST,
            "port": DB_PORT,
            "configured": configured,
            "persistent": configured,
            "persistent_volume_configured": configured,
            "durability_warning": (
                None
                if configured
                else "DB_HOST, DB_USER and DB_NAME are required for the MySQL backend."
            ),
        }
    path = Path(DB_PATH)
    durable_hint = os.environ.get("SNOOZMATE_PERSISTENT_VOLUME", "").strip().lower() in {"1", "true", "yes"}
    return {
        "backend": "sqlite",
        "database_path": str(path),
        "database_exists": path.exists(),
        "sqlite_wal": True,
        "persistent_volume_configured": durable_hint,
        "persistent": durable_hint,
        "durability_warning": (
            None
            if durable_hint
            else "SQLite is only durable when SNOOZMATE_DB_PATH is on a mounted persistent volume."
        ),
    }


def backup_database(destination: str = "") -> str:
    """Create a local SQLite backup; callers must store it in durable object/volume storage."""
    if DATABASE_BACKEND == "mysql":
        raise RuntimeError(
            "MySQL backups are managed by CloudBase; use its backup/rollback controls."
        )
    source = Path(DB_PATH)
    target_dir = Path(destination).resolve() if destination else source.parent / "backups"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"snoozmate-{datetime.now():%Y%m%d%H%M%S}.db"
    source_conn, target_conn = get_conn(), sqlite3.connect(target)
    try:
        source_conn.backup(target_conn)
    finally:
        target_conn.close()
        source_conn.close()
    return str(target)
