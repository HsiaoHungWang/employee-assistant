"""
main.py：FastAPI 後端（前端只呼叫這裡，API Key 永遠不進瀏覽器）

執行：uvicorn main:app --reload --port 8000
文件：http://localhost:8000/docs

API：
  GET  /api/users      示範用：可登入的員工清單
  POST /api/login      示範用登入，回傳 token（正式系統換成公司 SSO / JWT）
  POST /api/chat       送出訊息，回傳 SSE 串流
  POST /api/confirm    確認或取消寫入操作，回傳 SSE 串流
  POST /api/reset      清除對話記憶（並重給 token 額度）
  GET  /api/stats      觀測摘要
"""
import json
import uuid
from typing import Dict, Iterator

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import harness
from db import connect, init_db
from tools import tools_for

app = FastAPI(title="員工內部助理 API")
init_db()

SESSIONS: Dict[str, harness.Session] = {}   # 示範用：存在記憶體；正式系統改放 Redis 或資料庫


def load_user(employee_id: str):
    with connect() as conn:
        row = conn.execute("SELECT e.*, m.name AS manager_name FROM employees e"
                           " LEFT JOIN employees m ON m.id = e.manager_id WHERE e.id=?", (employee_id,)).fetchone()
    return dict(row) if row else None


def get_session(authorization: str) -> harness.Session:
    token = (authorization or "").removeprefix("Bearer ").strip()
    session = SESSIONS.get(token)
    if not session:
        raise HTTPException(401, "請先登入")
    return session


def sse(events: Iterator[dict]) -> StreamingResponse:
    def gen():
        try:
            for e in events:
                yield f"data: {json.dumps(e, ensure_ascii=False)}\n\n"
        except Exception as e:                       # 最後一道防線：任何錯誤都回成事件，不讓連線直接斷掉
            print(f"[ERROR] {e!r}")
            yield f"data: {json.dumps({'type': 'error', 'message': '系統發生錯誤，請稍後再試'}, ensure_ascii=False)}\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


class LoginIn(BaseModel):
    employee_id: str


class ChatIn(BaseModel):
    message: str


class ConfirmIn(BaseModel):
    action_id: str
    approved: bool


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/users")
def users():
    with connect() as conn:
        rows = conn.execute("SELECT id, name, title, dept, role FROM employees").fetchall()
    return [dict(r) for r in rows]


@app.post("/api/login")
def login(body: LoginIn):
    user = load_user(body.employee_id)
    if not user:
        raise HTTPException(404, "查無此員工")
    token = uuid.uuid4().hex
    SESSIONS[token] = harness.Session(user=user)
    return {"token": token, "user": user,
            "tools": [{"name": t.name, "label": t.label, "write": t.write} for t in tools_for(user).values()]}


@app.post("/api/chat")
def chat(body: ChatIn, authorization: str = Header(default="")):
    return sse(harness.chat(get_session(authorization), body.message))


@app.post("/api/confirm")
def confirm(body: ConfirmIn, authorization: str = Header(default="")):
    return sse(harness.confirm(get_session(authorization), body.action_id, body.approved))


@app.post("/api/reset")
def reset(authorization: str = Header(default="")):
    harness.reset(get_session(authorization))
    return {"ok": True}


@app.get("/api/stats")
def stats():
    """觀測摘要（harness_core.CallLogger.summary）：呼叫次數、失敗率、P95 耗時、tokens、被擋下次數"""
    return harness.LOGGER.summary()
