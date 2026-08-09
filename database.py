import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent / "errors.db"


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS errors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            robot TEXT,
            error_type TEXT,
            error_text TEXT,
            raw_text TEXT,
            chat_id TEXT,
            shift_date TEXT,   -- дата начала смены (YYYY-MM-DD)
            shift_name TEXT,   -- 'day' (06-18) или 'night' (18-06)
            created_at TEXT
        )
    """)
    conn.commit()
    conn.close()


def save_error(robot, error_type, error_text, raw_text, chat_id, shift_date, shift_name):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """INSERT INTO errors
           (robot, error_type, error_text, raw_text, chat_id, shift_date, shift_name, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (robot, error_type, error_text, raw_text, chat_id, shift_date, shift_name,
         datetime.now().isoformat())
    )
    conn.commit()
    conn.close()


def count_robot_errors_in_shift(robot, shift_date, shift_name):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "SELECT COUNT(*) FROM errors WHERE robot=? AND shift_date=? AND shift_name=?",
        (robot, shift_date, shift_name)
    )
    count = cur.fetchone()[0]
    conn.close()
    return count


def shift_stats(shift_date, shift_name):
    """Возвращает (total, {robot: count}, {error_type: count})"""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT robot, error_type FROM errors WHERE shift_date=? AND shift_name=?",
        (shift_date, shift_name)
    ).fetchall()
    conn.close()

    total = len(rows)
    by_robot, by_type = {}, {}
    for robot, error_type in rows:
        by_robot[robot] = by_robot.get(robot, 0) + 1
        by_type[error_type] = by_type.get(error_type, 0) + 1
    return total, by_robot, by_type