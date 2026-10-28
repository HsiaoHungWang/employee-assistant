"""
db.py：SQLite 假資料（代表公司「既有系統」：人資系統、會議室預約系統）

日期以「啟動當天（台北時間）」為基準動態產生，哪一天上課資料都合理。
刪掉 data/company.db 再重啟，就會重建乾淨的資料。
"""
import os
import sqlite3
from datetime import date, datetime, timedelta

from config import DB_PATH, TAIPEI


def today() -> date:
    return datetime.now(TAIPEI).date()


def d(offset: int) -> str:
    return (today() + timedelta(days=offset)).isoformat()


def next_weekday(weekday: int) -> date:
    """下一個指定星期幾（0=週一）；今天剛好是也算下週"""
    t = today()
    return t + timedelta(days=(weekday - t.weekday() - 1) % 7 + 1)


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


SCHEMA = """
CREATE TABLE employees (
    id TEXT PRIMARY KEY, name TEXT, title TEXT, dept TEXT, role TEXT, manager_id TEXT
);
CREATE TABLE leave_balance (
    employee_id TEXT, leave_type TEXT, total REAL, used REAL,
    PRIMARY KEY (employee_id, leave_type)
);
CREATE TABLE leave_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT, employee_id TEXT, leave_type TEXT,
    start_date TEXT, end_date TEXT, days REAL, reason TEXT, status TEXT, created_at TEXT
);
CREATE TABLE rooms (
    id TEXT PRIMARY KEY, name TEXT, capacity INTEGER, equipment TEXT
);
CREATE TABLE bookings (
    id INTEGER PRIMARY KEY AUTOINCREMENT, room_id TEXT, booking_date TEXT,
    start_time TEXT, end_time TEXT, title TEXT, employee_id TEXT
);
"""


def init_db():
    if os.path.exists(DB_PATH):
        return
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = connect()
    conn.executescript(SCHEMA)

    conn.executemany("INSERT INTO employees VALUES (?,?,?,?,?,?)", [
        ("E001", "王小明", "後端工程師", "研發部", "staff",   "E002"),
        ("E002", "陳美玲", "研發部經理", "研發部", "manager", None),
        ("E003", "林志豪", "前端工程師", "研發部", "staff",   "E002"),
    ])
    conn.executemany("INSERT INTO leave_balance VALUES (?,?,?,?)", [
        ("E001", "特休", 10, 3), ("E001", "病假", 30, 1), ("E001", "事假", 14, 0),
        ("E002", "特休", 15, 6), ("E002", "病假", 30, 0), ("E002", "事假", 14, 2),
        ("E003", "特休", 7, 7),  ("E003", "病假", 30, 2), ("E003", "事假", 14, 1),
    ])
    now = datetime.now(TAIPEI).strftime("%Y-%m-%d %H:%M")
    conn.executemany(
        "INSERT INTO leave_requests (employee_id, leave_type, start_date, end_date, days, reason, status, created_at)"
        " VALUES (?,?,?,?,?,?,?,?)", [
            ("E001", "特休", d(-20), d(-18), 3, "家族旅遊", "approved", now),
            ("E001", "病假", d(-9), d(-9), 1, "感冒", "approved", now),
            ("E003", "事假", next_weekday(2).isoformat(), next_weekday(2).isoformat(), 1, "辦理證件", "pending", now),
        ])
    conn.executemany("INSERT INTO rooms VALUES (?,?,?,?)", [
        ("R101", "小會議室 101", 4,  "白板"),
        ("R201", "中會議室 201", 8,  "投影機、白板"),
        ("R202", "中會議室 202", 8,  "大螢幕"),
        ("R301", "大會議室 301", 20, "投影機、視訊設備"),
    ])
    conn.executemany(
        "INSERT INTO bookings (room_id, booking_date, start_time, end_time, title, employee_id) VALUES (?,?,?,?,?,?)", [
            ("R201", d(1), "14:00", "15:30", "產品週會", "E002"),
            ("R101", d(1), "10:00", "11:00", "1on1", "E002"),
            ("R301", d(2), "09:00", "12:00", "全公司季會", "E002"),
        ])
    conn.commit()
    conn.close()
