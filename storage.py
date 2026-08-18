import json
import os
import sqlite3
import threading
from pathlib import Path


def _default_database_path():
    volume_path = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
    if volume_path:
        return Path(volume_path) / "players.db"
    return Path("players.db")


DATABASE_PATH = Path(os.environ.get("DATABASE_PATH", "").strip() or _default_database_path())

_LOCK = threading.RLock()
_INITIALIZED = False


def storage_description():
    return f"SQLite ({DATABASE_PATH})"


def _connect():
    connection = sqlite3.connect(DATABASE_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 10000")
    return connection


def init_storage():
    """Создаёт постоянную SQLite-базу без импорта старого players.json."""
    global _INITIALIZED
    if _INITIALIZED:
        return

    with _LOCK:
        if _INITIALIZED:
            return
        if (
            os.environ.get("RAILWAY_ENVIRONMENT_ID")
            and not os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
            and not os.environ.get("DATABASE_PATH")
        ):
            raise RuntimeError(
                "Railway Volume не подключён. Добавьте Volume с mount path /app/data, "
                "чтобы база игроков не находилась на временном диске."
            )
        DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS players (
                    discord_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS bot_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS player_archives (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    archive_name TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        _INITIALIZED = True


def load_data():
    init_storage()
    with _LOCK, _connect() as conn:
        rows = conn.execute("SELECT discord_id, payload FROM players").fetchall()
        return {row["discord_id"]: json.loads(row["payload"]) for row in rows}


def save_data(data):
    if not isinstance(data, dict):
        raise TypeError("База игроков должна быть словарём")

    init_storage()
    with _LOCK, _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing_ids = {
            row[0] for row in conn.execute("SELECT discord_id FROM players").fetchall()
        }
        current_ids = {str(discord_id) for discord_id in data}

        conn.executemany(
            """
            INSERT INTO players (discord_id, payload, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(discord_id) DO UPDATE SET
                payload = excluded.payload,
                updated_at = CURRENT_TIMESTAMP
            """,
            [
                (
                    str(discord_id),
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                )
                for discord_id, payload in data.items()
            ],
        )

        removed_ids = existing_ids - current_ids
        if removed_ids:
            conn.executemany(
                "DELETE FROM players WHERE discord_id = ?",
                [(discord_id,) for discord_id in removed_ids],
            )


def get_setting(key, default=None):
    init_storage()
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT value FROM bot_settings WHERE key = ?", (key,)
        ).fetchone()
        return default if row is None else json.loads(row["value"])


def set_setting(key, value):
    init_storage()
    encoded_value = json.dumps(value, ensure_ascii=False)
    with _LOCK, _connect() as conn:
        conn.execute(
            """
            INSERT INTO bot_settings (key, value, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = CURRENT_TIMESTAMP
            """,
            (key, encoded_value),
        )


def create_archive(archive_name, data):
    init_storage()
    encoded_data = json.dumps(data, ensure_ascii=False)
    with _LOCK, _connect() as conn:
        conn.execute(
            "INSERT INTO player_archives (archive_name, payload) VALUES (?, ?)",
            (archive_name, encoded_data),
        )
    return archive_name
