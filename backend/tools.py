"""
tools.py：把「既有功能」包成 tools，讓模型會用你的系統

三個落地重點都在這裡：
  1. 身分由後端注入：每個函式的第一個參數 user 來自登入 session，
     模型看到的 schema 裡「沒有」employee_id，它想查別人也查不到
  2. 規則寫在程式：天數、餘額、時段衝突都由程式算，模型只負責理解與轉述
  3. 讀寫分開：write=True 的工具不會直接執行，先回傳 preview 給使用者確認
"""
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

from config import TAIPEI
from db import connect, today

LEAVE_TYPES = ["特休", "病假", "事假"]


class ToolError(Exception):
    """參數或業務規則不符：訊息會回填給模型，讓它跟使用者解釋"""


# =====================================================================
# 小工具：驗證輸入（模型給的參數一律當成不可信）
# =====================================================================
def parse_date(s: str) -> date:
    try:
        return date.fromisoformat(s)
    except (TypeError, ValueError):
        raise ToolError(f"日期格式錯誤：{s}（請用 YYYY-MM-DD）")


def check_time(s: str) -> str:
    if not isinstance(s, str) or not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", s):
        raise ToolError(f"時間格式錯誤：{s}（請用 HH:MM，例如 14:00）")
    return s


def count_workdays(start: date, end: date) -> int:
    """計算區間內的工作天（週一～週五），國定假日此範例省略"""
    return sum(1 for i in range((end - start).days + 1)
               if (start + timedelta(days=i)).weekday() < 5)


def get_balance(employee_id: str, leave_type: str) -> float:
    with connect() as conn:
        row = conn.execute("SELECT total - used AS remain FROM leave_balance WHERE employee_id=? AND leave_type=?",
                           (employee_id, leave_type)).fetchone()
    return row["remain"] if row else 0


# =====================================================================
# 讀取類工具
# =====================================================================
def get_leave_balance(user: Dict[str, Any], leave_type: Optional[str] = None) -> Dict[str, Any]:
    with connect() as conn:
        rows = conn.execute("SELECT leave_type, total, used FROM leave_balance WHERE employee_id=?",
                            (user["id"],)).fetchall()
    if leave_type in LEAVE_TYPES:
        rows = [r for r in rows if r["leave_type"] == leave_type]
    return {"employee": user["name"],
            "balances": [{"假別": r["leave_type"], "總天數": r["total"], "已使用": r["used"],
                          "剩餘": r["total"] - r["used"]} for r in rows]}


LEAVE_STATUS = {"pending": "待簽核", "approved": "已核准", "rejected": "已駁回"}


def list_my_leave_requests(user: Dict[str, Any], status: Optional[str] = None) -> Dict[str, Any]:
    with connect() as conn:
        rows = conn.execute("SELECT id, leave_type, start_date, end_date, days, reason, status FROM leave_requests"
                            " WHERE employee_id=? ORDER BY start_date DESC LIMIT 10", (user["id"],)).fetchall()
    rows = [dict(r) for r in rows if status not in LEAVE_STATUS or r["status"] == status]
    return {"requests": [{**r, "status": LEAVE_STATUS.get(r["status"], r["status"])} for r in rows]}


POLICIES = [
    {"title": "請假規定",
     "content": "特休需於請假日前 3 個工作天提出申請；病假 3 天以上需附診斷證明；事假全年上限 14 天。"
                "請假單送出後由直屬主管簽核，簽核前可自行撤回。"},
    {"title": "加班規定",
     "content": "加班需事先於系統申請並經主管核准。平日加班前 2 小時以 1.34 倍計，之後以 1.67 倍計；"
                "也可選擇換補休，補休需於 6 個月內使用完畢。"},
    {"title": "出差與報帳",
     "content": "出差需於出發前 3 天提出申請。差旅費用請於返回後 7 天內檢附發票報帳，"
                "國內住宿上限每晚 2,500 元，高鐵可報標準車廂。"},
    {"title": "遠端工作",
     "content": "每週最多 2 天遠端工作，需於前一個工作日在系統登記。遠端工作日 10:00–16:00 需保持線上。"},
    {"title": "會議室使用",
     "content": "會議室開放時段 08:00–20:00，單次預約最長 4 小時。預約後未使用請提前取消，"
                "連續 3 次未到將暫停預約權限 2 週。"},
]


def search_policy(user: Dict[str, Any], keyword: str) -> Dict[str, Any]:
    """簡化版檢索：關鍵字比對。正式系統可換成向量檢索（RAG），對模型來說介面不變"""
    keyword = (keyword or "").strip()
    hits = [p for p in POLICIES if keyword and (keyword in p["title"] or keyword in p["content"])]
    if not hits:
        return {"found": False, "message": f"找不到與「{keyword}」相關的規章",
                "available_titles": [p["title"] for p in POLICIES]}
    return {"found": True, "policies": hits}


def find_free_rooms(user: Dict[str, Any], date: str, start_time: str, end_time: str,
                    capacity: int = 1) -> Dict[str, Any]:
    day = parse_date(date)
    start_time, end_time = check_time(start_time), check_time(end_time)
    if start_time >= end_time:
        raise ToolError("結束時間必須晚於開始時間")
    with connect() as conn:
        rooms = conn.execute("SELECT * FROM rooms WHERE capacity >= ? ORDER BY capacity",
                             (int(capacity or 1),)).fetchall()
        busy = {r["room_id"] for r in conn.execute(
            "SELECT room_id FROM bookings WHERE booking_date=? AND start_time < ? AND end_time > ?",
            (day.isoformat(), end_time, start_time))}
    free = [dict(r) for r in rooms if r["id"] not in busy]
    return {"date": day.isoformat(), "time": f"{start_time}-{end_time}", "free_rooms": free,
            "busy_rooms": sorted(busy)}


def list_team_leaves(user: Dict[str, Any], status: Optional[str] = None) -> Dict[str, Any]:
    """只有主管拿得到這支工具（見 TOOL_SPECS 的 roles）"""
    with connect() as conn:
        rows = conn.execute(
            "SELECT r.id AS request_id, e.name, r.leave_type, r.start_date, r.end_date, r.days, r.reason, r.status"
            " FROM leave_requests r"
            " JOIN employees e ON e.id = r.employee_id WHERE e.manager_id=? AND r.end_date >= ?"
            " ORDER BY r.start_date", (user["id"], today().isoformat())).fetchall()
    rows = [dict(r) for r in rows if status not in LEAVE_STATUS or r["status"] == status]
    return {"team_leaves": [{**r, "status": LEAVE_STATUS.get(r["status"], r["status"])} for r in rows]}


# =====================================================================
# 寫入類工具：preview（驗證 + 給使用者看的確認內容）與 execute（真正寫入）分開
# =====================================================================
def preview_apply_leave(user, leave_type: str, start_date: str, end_date: str, reason: str = "") -> Dict[str, Any]:
    if leave_type not in LEAVE_TYPES:
        raise ToolError(f"假別只能是 {LEAVE_TYPES}")
    start, end = parse_date(start_date), parse_date(end_date)
    if end < start:
        raise ToolError("結束日期不能早於開始日期")
    if start < today():
        raise ToolError("不能申請過去日期的假")
    days = count_workdays(start, end)
    if days == 0:
        raise ToolError("這段期間沒有工作天，不需要請假")
    remain = get_balance(user["id"], leave_type)
    if days > remain:
        raise ToolError(f"{leave_type}餘額不足：需要 {days} 天，剩餘 {remain} 天")
    warning = None
    if leave_type == "特休" and count_workdays(today() + timedelta(days=1), start - timedelta(days=1)) < 3:
        warning = "依請假規定，特休需於 3 個工作天前提出，主管可能退回"
    return {
        "title": "確認送出請假單",
        "details": [
            {"label": "假別", "value": leave_type},
            {"label": "期間", "value": f"{start} ～ {end}"},
            {"label": "天數", "value": f"{days} 天（剩餘 {remain} → {remain - days} 天）"},
            {"label": "事由", "value": reason or "（未填）"},
            {"label": "簽核主管", "value": user.get("manager_name") or "（無）"},
        ],
        "warning": warning,
        "computed": {"days": days},
    }


def apply_leave(user, leave_type: str, start_date: str, end_date: str, reason: str = "") -> Dict[str, Any]:
    p = preview_apply_leave(user, leave_type, start_date, end_date, reason)   # 執行前再驗一次（狀態可能變了）
    days = p["computed"]["days"]
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO leave_requests (employee_id, leave_type, start_date, end_date, days, reason, status, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (user["id"], leave_type, start_date, end_date, days, reason, "pending",
             datetime.now(TAIPEI).strftime("%Y-%m-%d %H:%M")))
        # 示範用：送出即預扣；實務上通常在主管核准後才扣
        conn.execute("UPDATE leave_balance SET used = used + ? WHERE employee_id=? AND leave_type=?",
                     (days, user["id"], leave_type))
    return {"success": True, "request_id": cur.lastrowid, "days": days, "status": "待主管簽核"}


def get_team_request(user, request_id) -> Dict[str, Any]:
    """權限檢查寫在程式：只能簽核「自己直屬部屬」且「待簽核」的假單"""
    try:
        request_id = int(request_id)
    except (TypeError, ValueError):
        raise ToolError(f"假單編號錯誤：{request_id}")
    with connect() as conn:
        row = conn.execute(
            "SELECT r.*, e.name, e.manager_id FROM leave_requests r JOIN employees e ON e.id = r.employee_id"
            " WHERE r.id=?", (request_id,)).fetchone()
    if not row or row["manager_id"] != user["id"]:
        raise ToolError(f"找不到你可以簽核的假單 #{request_id}")
    if row["status"] != "pending":
        raise ToolError(f"假單 #{request_id} 已經是「{LEAVE_STATUS.get(row['status'], row['status'])}」，不能再簽核")
    return dict(row)


def preview_review_leave(user, request_id: int, decision: str, comment: str = "") -> Dict[str, Any]:
    if decision not in ("approve", "reject"):
        raise ToolError("decision 只能是 approve（核准）或 reject（駁回）")
    if decision == "reject" and not comment.strip():
        raise ToolError("駁回時請填寫駁回原因")
    r = get_team_request(user, request_id)
    return {
        "title": "確認核准假單" if decision == "approve" else "確認駁回假單",
        "details": [
            {"label": "申請人", "value": r["name"]},
            {"label": "假別", "value": r["leave_type"]},
            {"label": "期間", "value": f"{r['start_date']} ～ {r['end_date']}（{r['days']:g} 天）"},
            {"label": "事由", "value": r["reason"] or "（未填）"},
            {"label": "簽核結果", "value": "核准" if decision == "approve" else "駁回"},
            {"label": "主管意見", "value": comment or "（無）"},
        ],
        "warning": "駁回後會退還申請人的假別天數" if decision == "reject" else None,
    }


def review_leave(user, request_id: int, decision: str, comment: str = "") -> Dict[str, Any]:
    preview_review_leave(user, request_id, decision, comment)
    r = get_team_request(user, request_id)
    status = "approved" if decision == "approve" else "rejected"
    with connect() as conn:
        conn.execute("UPDATE leave_requests SET status=? WHERE id=?", (status, r["id"]))
        if status == "rejected":                 # 送出時預扣過，駁回要退回
            conn.execute("UPDATE leave_balance SET used = used - ? WHERE employee_id=? AND leave_type=?",
                         (r["days"], r["employee_id"], r["leave_type"]))
    return {"success": True, "request_id": r["id"], "applicant": r["name"], "status": LEAVE_STATUS[status]}


def preview_book_room(user, room_id: str, date: str, start_time: str, end_time: str, title: str) -> Dict[str, Any]:
    day = parse_date(date)
    start_time, end_time = check_time(start_time), check_time(end_time)
    if start_time >= end_time:
        raise ToolError("結束時間必須晚於開始時間")
    if start_time < "08:00" or end_time > "20:00":
        raise ToolError("會議室開放時段為 08:00–20:00")
    if day < today():
        raise ToolError("不能預約過去的日期")
    with connect() as conn:
        room = conn.execute("SELECT * FROM rooms WHERE id=?", (room_id,)).fetchone()
        if not room:
            raise ToolError(f"找不到會議室 {room_id}")
        clash = conn.execute(
            "SELECT title, start_time, end_time FROM bookings WHERE room_id=? AND booking_date=?"
            " AND start_time < ? AND end_time > ?", (room_id, day.isoformat(), end_time, start_time)).fetchone()
    if clash:
        raise ToolError(f"{room['name']} 該時段已被預約（{clash['start_time']}-{clash['end_time']} {clash['title']}）")
    return {
        "title": "確認預約會議室",
        "details": [
            {"label": "會議室", "value": f"{room['name']}（{room['capacity']} 人，{room['equipment']}）"},
            {"label": "日期", "value": day.isoformat()},
            {"label": "時段", "value": f"{start_time} – {end_time}"},
            {"label": "會議名稱", "value": title},
        ],
        "warning": None,
    }


def book_room(user, room_id: str, date: str, start_time: str, end_time: str, title: str) -> Dict[str, Any]:
    preview_book_room(user, room_id, date, start_time, end_time, title)
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO bookings (room_id, booking_date, start_time, end_time, title, employee_id) VALUES (?,?,?,?,?,?)",
            (room_id, date, start_time, end_time, title, user["id"]))
    return {"success": True, "booking_id": cur.lastrowid}


# =====================================================================
# 工具註冊表：模型看到的（description、parameters）與真正執行的（fn）放在一起
# =====================================================================
@dataclass
class ToolSpec:
    name: str
    label: str                                  # 前端顯示用的中文名稱
    description: str
    parameters: Dict[str, Any]
    fn: Callable[..., Dict[str, Any]]
    write: bool = False                         # True = 需要使用者確認
    preview: Optional[Callable[..., Dict[str, Any]]] = None
    roles: Optional[set] = None                 # None = 所有人可用

    def schema(self) -> Dict[str, Any]:
        return {"type": "function",
                "function": {"name": self.name, "description": self.description, "parameters": self.parameters}}


# 每支工具都給至少一個「選填」參數：
# 部分模型／推論框架在串流模式下，遇到「完全沒有參數」的工具會漏掉函式名稱（實測 gemma 會）
STATUS_ARG = {"type": "object",
              "properties": {"status": {"type": "string", "enum": list(LEAVE_STATUS),
                                        "description": "選填：只列出 pending（待簽核）或 approved（已核准）"}}}

TOOL_SPECS: List[ToolSpec] = [
    ToolSpec("get_leave_balance", "查詢假別餘額",
             "查詢目前登入員工的假別（特休、病假、事假）總天數、已使用與剩餘天數",
             {"type": "object",
              "properties": {"leave_type": {"type": "string", "enum": LEAVE_TYPES,
                                            "description": "選填：只查某一種假別；不填則列出全部"}}},
             get_leave_balance),
    ToolSpec("list_my_leave_requests", "查詢請假紀錄",
             "列出目前登入員工最近的請假申請與簽核狀態", STATUS_ARG, list_my_leave_requests),
    ToolSpec("search_policy", "搜尋公司規章",
             "用關鍵字搜尋公司規章（請假、加班、出差、報帳、遠端工作、會議室）。回答規章問題前一定要先查",
             {"type": "object",
              "properties": {"keyword": {"type": "string", "description": "關鍵字，例如：特休、加班、報帳"}},
              "required": ["keyword"]},
             search_policy),
    ToolSpec("find_free_rooms", "查詢空會議室",
             "查詢指定日期與時段、可容納指定人數的空會議室",
             {"type": "object",
              "properties": {
                  "date": {"type": "string", "description": "日期 YYYY-MM-DD"},
                  "start_time": {"type": "string", "description": "開始時間 HH:MM"},
                  "end_time": {"type": "string", "description": "結束時間 HH:MM"},
                  "capacity": {"type": "integer", "description": "與會人數"}},
              "required": ["date", "start_time", "end_time"]},
             find_free_rooms),
    ToolSpec("apply_leave", "送出請假單",
             "替目前登入員工送出請假申請。系統會先請使用者確認，確認後才真正送出",
             {"type": "object",
              "properties": {
                  "leave_type": {"type": "string", "enum": LEAVE_TYPES},
                  "start_date": {"type": "string", "description": "開始日期 YYYY-MM-DD"},
                  "end_date": {"type": "string", "description": "結束日期 YYYY-MM-DD（單日請假與開始日期相同）"},
                  "reason": {"type": "string", "description": "請假事由"}},
              "required": ["leave_type", "start_date", "end_date"]},
             apply_leave, write=True, preview=preview_apply_leave),
    ToolSpec("book_room", "預約會議室",
             "預約會議室。請先用 find_free_rooms 確認有空，系統會請使用者確認後才真正預約",
             {"type": "object",
              "properties": {
                  "room_id": {"type": "string", "description": "會議室代號，例如 R201"},
                  "date": {"type": "string", "description": "日期 YYYY-MM-DD"},
                  "start_time": {"type": "string", "description": "開始時間 HH:MM"},
                  "end_time": {"type": "string", "description": "結束時間 HH:MM"},
                  "title": {"type": "string", "description": "會議名稱"}},
              "required": ["room_id", "date", "start_time", "end_time", "title"]},
             book_room, write=True, preview=preview_book_room),
    ToolSpec("list_team_leaves", "查詢部屬請假",
             "（主管專用）列出直屬部屬今天之後的請假狀況，包含假單編號 request_id", STATUS_ARG, list_team_leaves,
             roles={"manager"}),
    ToolSpec("review_leave", "簽核假單",
             "（主管專用）核准或駁回直屬部屬的假單。request_id 請先用 list_team_leaves 查詢，"
             "系統會請主管確認後才真正簽核",
             {"type": "object",
              "properties": {
                  "request_id": {"type": "integer", "description": "假單編號"},
                  "decision": {"type": "string", "enum": ["approve", "reject"],
                               "description": "approve = 核准，reject = 駁回"},
                  "comment": {"type": "string", "description": "主管意見；駁回時必填"}},
              "required": ["request_id", "decision"]},
             review_leave, write=True, preview=preview_review_leave, roles={"manager"}),
]


def tools_for(user: Dict[str, Any]) -> Dict[str, ToolSpec]:
    """依角色決定「這個人能用哪些工具」：不給 schema，模型連知道都不知道"""
    return {t.name: t for t in TOOL_SPECS if t.roles is None or user["role"] in t.roles}
