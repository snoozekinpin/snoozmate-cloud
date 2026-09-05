"""Small DB-API compatibility layer for the SQLite/MySQL backends."""
from __future__ import annotations

import re
import queue
import threading
from typing import Iterable


def _replace_qmarks(sql: str) -> str:
    """Convert DB-API qmark placeholders without touching quoted strings."""
    output = []
    quote = None
    index = 0
    while index < len(sql):
        char = sql[index]
        if quote:
            output.append(char)
            if char == quote:
                if index + 1 < len(sql) and sql[index + 1] == quote:
                    output.append(sql[index + 1])
                    index += 1
                else:
                    quote = None
            elif char == "\\" and index + 1 < len(sql):
                output.append(sql[index + 1])
                index += 1
        else:
            if char in {"'", '"', "`"}:
                quote = char
                output.append(char)
            elif char == "?":
                output.append("%s")
            else:
                output.append(char)
        index += 1
    return "".join(output)


def translate_mysql_sql(sql: str) -> str:
    """Translate the small SQLite SQL subset used by the application."""
    statement = sql.strip()
    if re.match(r"^PRAGMA\b", statement, flags=re.IGNORECASE):
        return "SELECT 1"
    statement = re.sub(r"\bBEGIN\s+IMMEDIATE\b", "START TRANSACTION", statement, flags=re.IGNORECASE)
    statement = re.sub(r"\bAUTOINCREMENT\b", "AUTO_INCREMENT", statement, flags=re.IGNORECASE)
    statement = re.sub(r"\bINSERT\s+OR\s+IGNORE\b", "INSERT IGNORE", statement, flags=re.IGNORECASE)

    nothing = re.search(
        r"\s+ON\s+CONFLICT\s*\([^)]*\)\s+DO\s+NOTHING\s*;?\s*$",
        statement,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if nothing:
        statement = statement[:nothing.start()]
        statement = re.sub(r"^\s*INSERT\s+INTO\b", "INSERT IGNORE INTO", statement, flags=re.IGNORECASE)

    update = re.search(
        r"\s+ON\s+CONFLICT\s*\([^)]*\)\s+DO\s+UPDATE\s+SET\s+(.+?)\s*;?\s*$",
        statement,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if update:
        assignments = re.sub(
            r"\bexcluded\.([A-Za-z_][A-Za-z0-9_]*)",
            r"VALUES(\1)",
            update.group(1),
            flags=re.IGNORECASE,
        )
        statement = statement[:update.start()] + " ON DUPLICATE KEY UPDATE " + assignments

    statement = re.sub(
        r"\bCREATE\s+INDEX\s+IF\s+NOT\s+EXISTS\b",
        "CREATE INDEX",
        statement,
        flags=re.IGNORECASE,
    )
    return _replace_qmarks(statement)


def split_sql_script(script: str) -> Iterable[str]:
    """Split semicolon-delimited SQL while respecting quoted semicolons."""
    current = []
    quote = None
    index = 0
    while index < len(script):
        char = script[index]
        if quote:
            current.append(char)
            if char == quote:
                if index + 1 < len(script) and script[index + 1] == quote:
                    current.append(script[index + 1])
                    index += 1
                else:
                    quote = None
        elif char in {"'", '"', "`"}:
            quote = char
            current.append(char)
        elif char == ";":
            statement = "".join(current).strip()
            if statement:
                yield statement
            current = []
        else:
            current.append(char)
        index += 1
    statement = "".join(current).strip()
    if statement:
        yield statement


class MySQLCursor:
    def __init__(self, cursor):
        self._cursor = cursor

    def execute(self, sql, params=None):
        translated = translate_mysql_sql(sql)
        self._cursor.execute(translated, params or ())
        return self

    def executemany(self, sql, params):
        self._cursor.executemany(translate_mysql_sql(sql), params)
        return self

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()

    @property
    def rowcount(self):
        return self._cursor.rowcount

    @property
    def lastrowid(self):
        return self._cursor.lastrowid

    def close(self):
        self._cursor.close()


class MySQLPool:
    """Bounded per-process pool; CloudBase may run several independent workers."""

    def __init__(self, factory, max_size: int):
        self._factory = factory
        self._available = queue.LifoQueue(maxsize=max_size)
        self._max_size = max_size
        self._created = 0
        self._lock = threading.Lock()

    def acquire(self):
        try:
            connection = self._available.get_nowait()
            try:
                connection.ping(reconnect=True)
                return connection
            except Exception:
                connection.close()
                with self._lock:
                    self._created -= 1
        except queue.Empty:
            pass

        with self._lock:
            if self._created < self._max_size:
                self._created += 1
                try:
                    return self._factory()
                except Exception:
                    self._created -= 1
                    raise
        return self._available.get(timeout=10)

    def release(self, connection):
        try:
            connection.rollback()
            connection.ping(reconnect=True)
            self._available.put_nowait(connection)
        except Exception:
            try:
                connection.close()
            finally:
                with self._lock:
                    self._created = max(0, self._created - 1)


class MySQLConnection:
    """Expose the methods used by the service while keeping connections explicit."""

    def __init__(self, connection, pool=None):
        self._connection = connection
        self._pool = pool
        self._closed = False

    def cursor(self):
        return MySQLCursor(self._connection.cursor())

    def execute(self, sql, params=None):
        cursor = self.cursor()
        cursor.execute(sql, params)
        return cursor

    def executescript(self, script):
        for statement in split_sql_script(script):
            try:
                self.execute(statement)
            except Exception as error:
                # CREATE TABLE/INDEX is idempotent at the application level;
                # MySQL reports duplicates when an old deployment already has it.
                code = getattr(error, "args", [None])[0]
                message = str(error).lower()
                if code not in {1050, 1061} and "already exists" not in message:
                    raise

    def commit(self):
        self._connection.commit()

    def rollback(self):
        self._connection.rollback()

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self._pool:
            self._pool.release(self._connection)
        else:
            self._connection.close()
