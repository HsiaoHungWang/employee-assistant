"""
harness.py：Harness 住在後端這一層（提示詞、記憶、工具、控制迴圈、確認關卡、觀測）

防護與觀測元件直接沿用課程的 harness_core.py（= ai-practice/harness.py，原封不動）：
  check_input   輸入防護：長度上限、提示詞注入擋下、個資遮罩後放行
  TokenBudget   成本護欄：每個工作階段的 token／呼叫次數上限，呼叫「之前」先擋
  call_llm      逾時 + 429/5xx 指數退避重試 + 觀測紀錄 + 計入預算（非串流）
  CallLogger    每次呼叫記一筆 JSONL（logs/llm_calls_YYYYMMDD.jsonl）
→ Notebook 裡練習的核心元件，落地時換了外殼（FastAPI + Vue）照樣使用

對外只有兩個入口，都回傳「事件」的 generator，由 main.py 轉成 SSE 給前端：
  chat(session, text)               使用者送出訊息
  confirm(session, action_id, ok)   使用者按下「確認／取消」

事件格式（前端照 type 處理）：
  {"type": "delta",   "text": "..."}                         模型回覆的文字片段
  {"type": "tool",    "name", "label", "status": "running|done|error"}
  {"type": "confirm", "action": {id, title, details, warning}} 寫入類工具等待確認
  {"type": "notice",  "message": "..."}                       防護提示（例如個資已遮罩）
  {"type": "done",    "usage": {...}, "budget": {...}, "waiting": bool}
  {"type": "error",   "message": "..."}
"""
import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List, Optional

import openai

from config import (client, MODEL, STREAM, MAX_STEPS, MAX_TURNS, MAX_INPUT_CHARS,
                    TEMPERATURE, MAX_TOKENS, TAIPEI, SESSION_TOKEN_BUDGET, SESSION_MAX_CALLS)
from harness_core import (CallLogger, TokenBudget, BudgetExceeded, check_input, call_llm, make_client,
                          is_retryable, backoff_delay, estimate_messages_tokens, estimate_tokens)
from tools import ToolError, tools_for

WEEKDAYS = "一二三四五六日"

# SDK 不自動重試（max_retries=0），每一次嘗試都由 call_llm 決定要不要重試，並記錄下來
LLM = make_client(client, timeout=60)
LOGGER = CallLogger(echo=True)


# =====================================================================
# ① 提示詞層：每次呼叫都重新組，日期與使用者身分由後端填入
# =====================================================================
def build_system_prompt(user: Dict[str, Any]) -> str:
    now = datetime.now(TAIPEI)
    return f"""你是公司的「員工內部助理」，協助員工查詢假勤、公司規章與預約會議室。

【目前使用者】{user['name']}（{user['dept']}／{user['title']}）
【今天】{now:%Y-%m-%d}（星期{WEEKDAYS[now.weekday()]}），時區 Asia/Taipei

【規則】
1. 假別餘額、請假紀錄、會議室空檔、規章內容，一律先呼叫工具查詢，不可自行推測或編造。
2. 使用者說「明天、下週五」等相對日期時，依今天日期換算成 YYYY-MM-DD 再呼叫工具。
3. 送出請假、預約會議室、簽核假單時，必要資訊不足就先詢問使用者；資訊齊全就直接呼叫工具，系統會跳出確認卡給使用者，你不需要再問「確定嗎」。
   工具結果中 confirmed_by_user 為 true，代表使用者已確認、操作已經完成並寫入系統，請直接用完成式回報結果，不要再提「確認」或「送出申請」。
4. 工具回傳 error 時，用一句話說明原因並建議下一步。
5. 只處理公司內部事務；其他問題請禮貌說明你的服務範圍。
6. 你能做的事就是上面提供的工具；沒有對應工具的需求，直接說明目前不支援，不要編造其他系統、網址或流程。
7. 使用繁體中文，回答簡潔，多筆資料用條列或表格呈現。"""


# =====================================================================
# ② 記憶層：每個 session 一份對話紀錄，只保留最近 MAX_TURNS 輪
# =====================================================================
def new_budget() -> TokenBudget:
    return TokenBudget(max_total_tokens=SESSION_TOKEN_BUDGET, max_calls=SESSION_MAX_CALLS)


@dataclass
class Session:
    user: Dict[str, Any]
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    messages: List[Dict[str, Any]] = field(default_factory=list)   # 不含 system
    pending: Optional[Dict[str, Any]] = None                        # 等待確認的寫入操作
    budget: TokenBudget = field(default_factory=new_budget)         # ⑤ 成本護欄

    @property
    def tag(self) -> str:
        return f"{self.user['id']}/{self.id[:6]}"


def trimmed(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """從「使用者訊息」切齊，避免把 assistant(tool_calls) 和它的 tool 結果拆散"""
    user_idx = [i for i, m in enumerate(messages) if m["role"] == "user"]
    if len(user_idx) <= MAX_TURNS:
        return messages
    return messages[user_idx[-MAX_TURNS]:]


def reset(session: Session):
    session.messages.clear()
    session.pending = None
    session.budget = new_budget()   # 示範用：清除對話就重給額度；實務上通常是「每人每天」的額度


# =====================================================================
# 呼叫模型（一輪）
#   非串流：直接用 harness_core.call_llm（重試、觀測、預算都包好了）
#   串流  ：call_llm 不支援 stream，這裡照同樣的三件事自己做一次
#           ── 送出前 check_before、失敗退避重試、完成後 LOGGER.log + budget.add
# 用 generator 的 return 值把結果交回去：content, tool_calls, usage, finish_reason
# =====================================================================
def call_plain(session: Session, kwargs, step: int):
    resp = call_llm(LLM, logger=LOGGER, budget=session.budget, tag=f"{session.tag}#{step}",
                    max_retries=2, **kwargs)
    msg = resp.choices[0].message
    calls = [{"id": tc.id or f"call_{uuid.uuid4().hex[:8]}", "name": tc.function.name,
              "arguments": tc.function.arguments or "{}"} for tc in (msg.tool_calls or [])]
    usage = resp.usage.model_dump() if resp.usage else None
    return msg.content or "", calls, usage, resp.choices[0].finish_reason


def calls_complete(calls) -> bool:
    """串流拼回來的 tool_calls 是否完整：要有函式名稱、參數要是合法 JSON"""
    for c in calls:
        if not c["name"]:
            return False
        try:
            json.loads(c["arguments"])
        except ValueError:
            return False
    return True


def open_stream(session: Session, kwargs, step: int, max_retries: int = 2):
    """建立串流連線；可重試的錯誤（429、5xx、逾時）用指數退避重試"""
    for attempt in range(max_retries + 1):
        t0 = time.perf_counter()
        try:
            try:
                return LLM.chat.completions.create(**kwargs, stream=True, stream_options={"include_usage": True})
            except openai.BadRequestError:
                return LLM.chat.completions.create(**kwargs, stream=True)   # 平台不支援 include_usage 就退回
        except Exception as e:
            retry = is_retryable(e) and attempt < max_retries
            LOGGER.log(status="error", tag=f"{session.tag}#{step}", model=MODEL, attempt=attempt,
                       latency_ms=round((time.perf_counter() - t0) * 1000), error=type(e).__name__,
                       status_code=getattr(e, "status_code", None), message=str(e)[:200], will_retry=retry)
            if not retry:
                raise
            time.sleep(backoff_delay(attempt))


def call_model(session: Session, messages, tools, step: int) -> Iterator[Dict[str, Any]]:
    kwargs = dict(model=MODEL, messages=messages, temperature=TEMPERATURE, max_tokens=MAX_TOKENS)
    if tools:
        kwargs["tools"] = tools

    if not STREAM:
        content, calls, usage, finish = call_plain(session, kwargs, step)
        if content:
            yield {"type": "delta", "text": content}
        return content, calls, usage, finish

    session.budget.check_before(messages, MAX_TOKENS)      # 送出前先擋：超過預算就不送
    t0 = time.perf_counter()
    stream = open_stream(session, kwargs, step)

    content, usage, finish = "", None, None
    acc: Dict[int, Dict[str, str]] = {}
    for chunk in stream:
        if getattr(chunk, "usage", None):
            usage = chunk.usage.model_dump()
        if not chunk.choices:
            continue
        choice = chunk.choices[0]
        finish = choice.finish_reason or finish
        delta = choice.delta
        if delta.content:
            content += delta.content
            yield {"type": "delta", "text": delta.content}
        for tc in delta.tool_calls or []:                   # tool_calls 會被切成碎片，依 index 拼回來
            slot = acc.setdefault(tc.index if tc.index is not None else len(acc),
                                  {"id": "", "name": "", "arguments": ""})
            if tc.id:
                slot["id"] = tc.id
            if tc.function and tc.function.name:
                slot["name"] = tc.function.name
            if tc.function and tc.function.arguments:
                slot["arguments"] += tc.function.arguments
    calls = [{**c, "id": c["id"] or f"call_{uuid.uuid4().hex[:8]}", "arguments": c["arguments"] or "{}"}
             for _, c in sorted(acc.items())]

    if not usage:                                           # 平台沒回用量 → 用估算值記帳，預算才不會失效
        usage = {"prompt_tokens": estimate_messages_tokens(messages),
                 "completion_tokens": estimate_tokens(content) + estimate_tokens(calls), "estimated": True}
    LOGGER.log(status="ok", tag=f"{session.tag}#{step}", model=MODEL, stream=True,
               latency_ms=round((time.perf_counter() - t0) * 1000),
               prompt_tokens=usage.get("prompt_tokens") or 0, completion_tokens=usage.get("completion_tokens") or 0,
               finish_reason=finish, tool_calls=[c["name"] for c in calls])
    session.budget.add(SimpleNamespace(**{k: usage.get(k) for k in ("prompt_tokens", "completion_tokens")}))

    # 防護：串流碎片拼不完整（例如漏了函式名稱）→ 這一輪改用一般呼叫重問一次
    if not calls_complete(calls):
        print(f"[WARN] 串流 tool_calls 不完整 {calls}，改用一般呼叫重試")
        content2, calls, usage2, finish = call_plain(session, kwargs, step)
        if content2 and not content:
            yield {"type": "delta", "text": content2}
        content = content or content2
        usage = {k: (usage.get(k) or 0) + ((usage2 or {}).get(k) or 0)
                 for k in ("prompt_tokens", "completion_tokens")}
    return content, calls, usage, finish


# =====================================================================
# ③ 工具層 + ④ 控制迴圈
# =====================================================================
def tool_message(call_id: str, result: Dict[str, Any]) -> Dict[str, Any]:
    return {"role": "tool", "tool_call_id": call_id, "content": json.dumps(result, ensure_ascii=False)}


def budget_info(session: Session) -> Dict[str, int]:
    return {"used": session.budget.used, "max": session.budget.max_total_tokens}


def run_loop(session: Session) -> Iterator[Dict[str, Any]]:
    specs = tools_for(session.user)
    schemas = [s.schema() for s in specs.values()]
    total = {"prompt_tokens": 0, "completion_tokens": 0}
    seen = set()

    for step in range(1, MAX_STEPS + 1):
        messages = [{"role": "system", "content": build_system_prompt(session.user)}] + trimmed(session.messages)
        try:
            content, calls, usage, finish = yield from call_model(session, messages, schemas, step)
        except BudgetExceeded as e:
            LOGGER.log(status="blocked", tag=f"{session.tag}#{step}", error="BudgetExceeded", message=str(e))
            yield {"type": "error", "message": f"本次對話已達用量上限（{e}），請按「清除對話」重新開始"}
            return
        except openai.APIError as e:
            yield {"type": "error", "message": f"模型服務暫時無法使用（{type(e).__name__}），請稍後再試"}
            return
        for k in total:
            total[k] += (usage or {}).get(k) or 0

        assistant = {"role": "assistant", "content": content or (None if calls else "")}
        if calls:
            assistant["tool_calls"] = [{"id": c["id"], "type": "function",
                                        "function": {"name": c["name"], "arguments": c["arguments"]}} for c in calls]
        session.messages.append(assistant)

        if not calls:                                   # 沒有要用工具 → 這就是最終回答
            yield {"type": "done", "usage": total, "budget": budget_info(session), "waiting": False}
            return

        # 同樣的工具 + 同樣的參數又叫一次 → 判定為鬼打牆，中止
        signature = tuple((c["name"], c["arguments"]) for c in calls)
        if signature in seen:
            for c in calls:
                session.messages.append(tool_message(c["id"], {"error": "重複呼叫，已中止"}))
            yield {"type": "error", "message": "助理重複執行相同步驟，已自動中止，請換個說法再試一次"}
            return
        seen.add(signature)

        for c in calls:
            spec = specs.get(c["name"])
            if spec is None:                            # 白名單外的工具（或沒有權限）
                session.messages.append(tool_message(c["id"], {"error": f"無法使用工具 {c['name']}"}))
                continue
            try:
                args = json.loads(c["arguments"])       # 模型給的是字串，不是 dict
                if not isinstance(args, dict):
                    raise ValueError
            except ValueError:
                session.messages.append(tool_message(c["id"], {"error": "參數不是合法 JSON"}))
                continue

            if spec.write:
                # 確認關卡：先驗證、產生預覽，暫停迴圈等使用者決定
                if session.pending:
                    session.messages.append(tool_message(c["id"], {"error": "一次只能確認一項操作，請稍後再處理這一項"}))
                    continue
                try:
                    preview = spec.preview(session.user, **args)
                except (ToolError, TypeError) as e:
                    yield {"type": "tool", "name": spec.name, "label": spec.label, "status": "error"}
                    session.messages.append(tool_message(c["id"], {"error": str(e)}))
                    continue
                action_id = uuid.uuid4().hex[:12]
                session.pending = {"id": action_id, "call_id": c["id"], "tool": spec.name, "args": args}
                yield {"type": "confirm", "action": {"id": action_id, "tool": spec.name, "label": spec.label,
                                                     "title": preview["title"], "details": preview["details"],
                                                     "warning": preview.get("warning")}}
                continue

            yield {"type": "tool", "name": spec.name, "label": spec.label, "status": "running"}
            try:
                result = spec.fn(session.user, **args)
                status = "done"
            except (ToolError, TypeError) as e:
                result, status = {"error": str(e)}, "error"
            except Exception as e:                      # 既有系統出錯：不要把內部細節丟給模型
                print(f"[TOOL ERROR] {spec.name}: {e!r}")
                result, status = {"error": "系統暫時無法處理，請稍後再試"}, "error"
            session.messages.append(tool_message(c["id"], result))
            yield {"type": "tool", "name": spec.name, "label": spec.label, "status": status}

        if session.pending:                             # 等使用者按確認，這一輪先停在這裡
            yield {"type": "done", "usage": total, "budget": budget_info(session), "waiting": True}
            return

    yield {"type": "error", "message": f"超過 {MAX_STEPS} 個步驟仍未完成，已中止"}


# =====================================================================
# 對外入口
# =====================================================================
def chat(session: Session, text: str) -> Iterator[Dict[str, Any]]:
    text = (text or "").strip()
    if not text:
        yield {"type": "error", "message": "請輸入訊息"}
        return

    # ⑤ 輸入防護：注入 → 擋下（不送模型、不進記憶）；個資 → 遮罩後放行
    guard = check_input(text, max_chars=MAX_INPUT_CHARS)
    if not guard.ok:
        LOGGER.log(status="blocked", tag=session.tag, error="InputGuard", message="；".join(guard.issues))
        yield {"type": "error", "message": f"訊息未送出：{'；'.join(guard.issues)}"}
        return
    if guard.issues:
        yield {"type": "notice", "message": "已自動遮罩：" + "、".join(guard.issues)}
    text = guard.text

    if session.pending:
        # 使用者沒按確認就繼續打字 → 視同取消，先把 tool 結果補上，對話紀錄才會完整
        p, session.pending = session.pending, None
        session.messages.append(tool_message(p["call_id"], {"cancelled": True, "message": "使用者未確認，操作已取消"}))
    session.messages.append({"role": "user", "content": text})
    yield from run_loop(session)


def confirm(session: Session, action_id: str, approved: bool) -> Iterator[Dict[str, Any]]:
    p = session.pending
    if not p or p["id"] != action_id:
        yield {"type": "error", "message": "這項操作已失效，請重新提出"}
        return
    session.pending = None
    spec = tools_for(session.user)[p["tool"]]
    # 回填給模型的結果要把「發生了什麼」講清楚，模型才不會用猜的（例如以為還在等確認）
    if approved:
        yield {"type": "tool", "name": spec.name, "label": spec.label, "status": "running"}
        try:
            result = {"confirmed_by_user": True, "completed": True,
                      "note": "使用者已在確認卡按下確認，操作已完成並寫入系統",
                      **spec.fn(session.user, **p["args"])}
            status = "done"
        except ToolError as e:
            result = {"confirmed_by_user": True, "completed": False, "error": str(e)}
            status = "error"
        yield {"type": "tool", "name": spec.name, "label": spec.label, "status": status}
    else:
        result = {"confirmed_by_user": False, "cancelled": True, "note": "使用者在確認卡按下取消，操作未執行"}
    session.messages.append(tool_message(p["call_id"], result))
    yield from run_loop(session)                        # 讓模型根據結果回覆使用者
