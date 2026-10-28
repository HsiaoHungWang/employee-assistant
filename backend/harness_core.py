"""
harness.py：Harness（應用層／骨架）共用元件
對應講義第 36～42 頁「Harness 工程」「五大元件」「Agent Loop」「撐得住上線的六個要點」

模型只會「輸入文字、輸出文字」；讓它變成可用系統的，是包在外面的這一層。
這支檔案不直接執行，由 10～13 範例匯入使用：

  ① 提示詞層   build_system_prompt()         角色、規則、輸出格式、few-shot 範例
  ② 記憶層     ShortTermMemory / LongTermMemory / MemoryManager
                                             截斷（token 預算視窗）、摘要、長期事實檢索
  ③ 工具層     ToolRegistry                  註冊表 + 三層防護 + 人工確認關卡
  ④ 控制迴圈   AgentLoop + StopConfig        最大步數、token 上限、最長耗時、重複工具就中止
  ⑤ 防護與觀測 check_input / check_output    提示詞注入、個資遮罩、格式與禁用字詞檢查
               CallLogger                    每次呼叫記錄 id、model、tokens、耗時、finish_reason
               call_llm()                    逾時 + 429/5xx 指數退避重試 + 成本護欄
               TokenBudget                   每個工作階段的 token 上限

設計原則：每個元件都「把 client 當參數傳進來」，不在匯入時連線，方便單獨測試與替換。
"""
import json
import math
import os
import random
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

import openai

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(HERE, "logs")
TAIPEI = timezone(timedelta(hours=8))


def now_str() -> str:
    return datetime.now(TAIPEI).strftime("%Y-%m-%d %H:%M:%S")


# =====================================================================
# 0. token 估算（送出前用；送出後一律以 API 回傳的 usage 為準）
#    中日韓文字約 1 字 ≈ 1 token，英數約 4 字元 ≈ 1 token —— 粗估，用來「事前擋」夠用
# =====================================================================
_CJK = re.compile(r"[　-〿㐀-鿿＀-￯]")


def estimate_tokens(text: Any) -> int:
    if not text:
        return 0
    if not isinstance(text, str):
        text = json.dumps(text, ensure_ascii=False)
    cjk = len(_CJK.findall(text))
    return cjk + math.ceil((len(text) - cjk) / 4)


def estimate_messages_tokens(messages: List[Dict[str, Any]]) -> int:
    total = 0
    for m in messages:
        content = m.get("content")
        if isinstance(content, list):          # 多模態：只估文字部分
            content = " ".join(p.get("text", "") for p in content if p.get("type") == "text")
        total += 4 + estimate_tokens(content) + estimate_tokens(m.get("tool_calls"))
    return total


# =====================================================================
# ① 提示詞層：把「角色、規則、格式、範例」組成完整 system prompt
# =====================================================================
def build_system_prompt(role: str,
                        rules: Optional[List[str]] = None,
                        output_format: Optional[str] = None,
                        examples: Optional[List[Tuple[str, str]]] = None,
                        context: Optional[Dict[str, str]] = None) -> str:
    """
    role          你是誰（一句話）
    rules         限制條件，例如「只能根據工具結果回答」
    output_format 固定輸出格式（越具體，後面的 check_output 越好驗）
    examples      few-shot：[(輸入, 理想輸出), ...]，比文字描述更能穩定格式
    context       動態注入的事實，例如 {"今天日期": "2026-10-20"}
    """
    parts = [role.strip()]
    if context:
        parts.append("【背景資訊】\n" + "\n".join(f"- {k}：{v}" for k, v in context.items()))
    if rules:
        parts.append("【規則】\n" + "\n".join(f"{i}. {r}" for i, r in enumerate(rules, 1)))
    if output_format:
        parts.append("【輸出格式】\n" + output_format.strip())
    if examples:
        shots = "\n\n".join(f"輸入：{q}\n輸出：{a}" for q, a in examples)
        parts.append("【範例】\n" + shots)
    return "\n\n".join(parts)


# =====================================================================
# ⑤-a 可觀測性：每次呼叫都記一筆（JSON Lines，一行一筆，好 grep 也好匯入試算表）
# =====================================================================
class CallLogger:
    def __init__(self, path: Optional[str] = None, echo: bool = False):
        os.makedirs(LOG_DIR, exist_ok=True)
        self.path = path or os.path.join(LOG_DIR, f"llm_calls_{datetime.now(TAIPEI):%Y%m%d}.jsonl")
        self.echo = echo
        self.records: List[Dict[str, Any]] = []

    def log(self, **record):
        record = {"ts": now_str(), **record}
        self.records.append(record)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        if self.echo:
            if record.get("status") == "ok":
                print(f"  [log] {record.get('tag', '')} {record['model']} "
                      f"{record['latency_ms']}ms tokens={record['prompt_tokens']}+{record['completion_tokens']} "
                      f"finish={record['finish_reason']}")
            else:
                print(f"  [log] {record.get('tag', '')} ERROR {record.get('error')} "
                      f"(status={record.get('status_code')}, attempt={record.get('attempt')})")

    def summary(self) -> Dict[str, Any]:
        ok = [r for r in self.records if r.get("status") == "ok"]
        err = [r for r in self.records if r.get("status") == "error"]
        blocked = [r for r in self.records if r.get("status") == "blocked"]   # 被防護擋下，沒有送出
        lat = sorted(r["latency_ms"] for r in ok)
        p95 = lat[min(len(lat) - 1, int(len(lat) * 0.95))] if lat else 0
        return {
            "呼叫次數": len(self.records),
            "成功": len(ok),
            "失敗": len(err),
            "被防護擋下": len(blocked),
            "失敗率": f"{len(err) / (len(ok) + len(err)):.0%}" if ok or err else "-",
            "平均耗時ms": round(sum(lat) / len(lat)) if lat else 0,
            "P95耗時ms": p95,
            "輸入tokens": sum(r["prompt_tokens"] for r in ok),
            "輸出tokens": sum(r["completion_tokens"] for r in ok),
            "finish_reason": dict(Counter(r["finish_reason"] for r in ok)),
            "被截斷(length)": sum(1 for r in ok if r["finish_reason"] == "length"),
        }

    def print_summary(self):
        print("\n--- 觀測摘要 ---")
        for k, v in self.summary().items():
            print(f"{k:<14}{v}")
        print(f"（明細：{self.path}）")


LOGGER = CallLogger()


# =====================================================================
# ⑤-b 成本護欄：每個工作階段的 token 上限，超過就停，不要讓迴圈把預算燒光
# =====================================================================
class BudgetExceeded(RuntimeError):
    pass


@dataclass
class TokenBudget:
    max_total_tokens: int = 20_000
    max_calls: int = 30
    price_in_per_1k: float = 0.0     # 每 1K 輸入 token 單價（依你的平台填；0 表示不計價）
    price_out_per_1k: float = 0.0
    used_prompt: int = 0
    used_completion: int = 0
    calls: int = 0

    @property
    def used(self) -> int:
        return self.used_prompt + self.used_completion

    @property
    def remaining(self) -> int:
        return max(0, self.max_total_tokens - self.used)

    @property
    def cost(self) -> float:
        return (self.used_prompt * self.price_in_per_1k + self.used_completion * self.price_out_per_1k) / 1000

    def check_before(self, messages: List[Dict[str, Any]], max_tokens: int = 0):
        """呼叫「之前」先擋：已用 + 這次預估輸入 + 這次最多輸出 > 上限，就不送"""
        if self.calls >= self.max_calls:
            raise BudgetExceeded(f"已達呼叫次數上限 {self.max_calls}")
        need = estimate_messages_tokens(messages) + (max_tokens or 0)
        if self.used + need > self.max_total_tokens:
            raise BudgetExceeded(
                f"token 預算不足：已用 {self.used:,}，這次預估需要 {need:,}，上限 {self.max_total_tokens:,}")

    def add(self, usage):
        if usage is None:
            return
        self.calls += 1
        self.used_prompt += usage.prompt_tokens or 0
        self.used_completion += usage.completion_tokens or 0

    def report(self) -> str:
        s = (f"已用 {self.used:,} / {self.max_total_tokens:,} tokens"
             f"（輸入 {self.used_prompt:,}、輸出 {self.used_completion:,}），呼叫 {self.calls} 次")
        if self.price_in_per_1k or self.price_out_per_1k:
            s += f"，估計成本 {self.cost:.4f}"
        return s


# =====================================================================
# ⑤-c 逾時與重試：timeout=60；429 / 5xx / 逾時 / 斷線 用指數退避重試 2～3 次
#     400（參數錯）、401（金鑰錯）、404（模型名稱錯）重試也沒用 → 直接拋出
# =====================================================================
RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}


def make_client(base_client, timeout: float = 60.0):
    """
    從 common.client 衍生一個「逾時 60 秒、SDK 不自動重試」的 client。
    為什麼關掉 SDK 內建重試（max_retries=0）？教學上要讓每一次嘗試都被 call_llm 看見並記錄。
    實務上若不需要逐次記錄，直接用 OpenAI(..., timeout=60, max_retries=3) 也可以，SDK 內建就有指數退避。
    """
    return base_client.with_options(timeout=timeout, max_retries=0)


def is_retryable(e: Exception) -> bool:
    if isinstance(e, (openai.APITimeoutError, openai.APIConnectionError)):
        return True
    return getattr(e, "status_code", None) in RETRYABLE_STATUS


def retry_after_seconds(e: Exception) -> Optional[float]:
    """伺服器若在 429 回應帶 Retry-After，就照它說的等"""
    resp = getattr(e, "response", None)
    try:
        return float(resp.headers.get("retry-after"))
    except Exception:
        return None


def backoff_delay(attempt: int, base: float = 1.0, cap: float = 20.0) -> float:
    """指數退避 + 隨機抖動：1s、2s、4s…（再加 0～0.5s，避免大家同一瞬間一起重試）"""
    return min(cap, base * (2 ** attempt)) + random.uniform(0, 0.5)


def call_llm(client, *, logger: Optional[CallLogger] = None, budget: Optional[TokenBudget] = None,
             max_retries: int = 3, base_delay: float = 1.0, tag: str = "", verbose: bool = True,
             **kwargs):
    """
    包一層 client.chat.completions.create：
      1. 送出前 → 成本護欄檢查（budget）
      2. 失敗時 → 可重試的錯誤用指數退避重試；不可重試的直接拋出
      3. 每一次嘗試 → 寫一筆觀測紀錄（成功或失敗都記）
      4. 成功後 → 把 usage 記進 budget
    不支援 stream=True（串流的 usage 要另外處理，見 12_production.py stream）
    """
    logger = logger or LOGGER
    if budget:
        budget.check_before(kwargs.get("messages", []), kwargs.get("max_tokens", 0))

    for attempt in range(max_retries + 1):
        t0 = time.perf_counter()
        try:
            resp = client.chat.completions.create(**kwargs)
        except Exception as e:
            retry = is_retryable(e) and attempt < max_retries
            logger.log(status="error", tag=tag, model=kwargs.get("model"), attempt=attempt,
                       latency_ms=round((time.perf_counter() - t0) * 1000),
                       error=type(e).__name__, status_code=getattr(e, "status_code", None),
                       message=str(e)[:200], will_retry=retry)
            if not retry:
                raise
            delay = retry_after_seconds(e) or backoff_delay(attempt, base_delay)
            if verbose:
                print(f"  ⟳ {type(e).__name__}（status={getattr(e, 'status_code', '-')}），"
                      f"{delay:.1f} 秒後第 {attempt + 1} 次重試…")
            time.sleep(delay)
            continue

        latency_ms = round((time.perf_counter() - t0) * 1000)
        u = resp.usage
        logger.log(status="ok", tag=tag, id=resp.id, model=resp.model, attempt=attempt,
                   latency_ms=latency_ms,
                   prompt_tokens=getattr(u, "prompt_tokens", 0) or 0,
                   completion_tokens=getattr(u, "completion_tokens", 0) or 0,
                   finish_reason=resp.choices[0].finish_reason)
        if budget:
            budget.add(u)
        return resp


# =====================================================================
# ⑤-d 輸入輸出防護
#   輸入：長度上限、提示詞注入（擋下）、個資（遮罩後放行）
#   輸出：個資遮罩、禁用字詞、必要段落／JSON 格式檢查
#   ⚠️ 關鍵字規則只能擋最常見的寫法，是「第一道網」不是保證；
#      真正的防線是「工具權限最小化 + 敏感動作人工確認」（見 ToolRegistry）
# =====================================================================
INJECTION_PATTERNS = [
    r"(忽略|無視|忘記|忘掉).{0,8}(之前|以上|前面|先前|所有).{0,8}(指示|指令|規則|設定|提示)",
    r"ignore\s+(all\s+)?(the\s+)?(previous|prior|above|earlier)\s+(instructions|rules|prompts?)",
    r"(顯示|印出|告訴我|輸出|洩漏).{0,8}(系統提示|系統指令|system\s*prompt)",
    r"reveal.{0,20}(system\s*prompt|instructions)",
    r"(你現在是|扮演|假裝你是).{0,12}(沒有|不受).{0,4}(限制|規範)",
    r"(developer|開發者|DAN)\s*模式|developer\s+mode|jailbreak|越獄",
]

# (名稱, 規則, 遮罩函式)；用前後環視取代 \b，因為中文字也算 \w，\b 會失效
PII_RULES: List[Tuple[str, str, Callable[[re.Match], str]]] = [
    ("身分證字號", r"(?<![A-Za-z0-9])[A-Z][12]\d{8}(?![0-9])",
     lambda m: m.group()[:2] + "******" + m.group()[-2:]),
    ("信用卡號", r"(?<!\d)(?:\d{4}[- ]?){3}\d{4}(?!\d)",
     lambda m: "****-****-****-" + re.sub(r"\D", "", m.group())[-4:]),
    ("手機號碼", r"(?<!\d)09\d{2}[- ]?\d{3}[- ]?\d{3}(?!\d)",
     lambda m: m.group()[:4] + "-***-" + re.sub(r"\D", "", m.group())[-3:]),
    ("Email", r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
     lambda m: m.group()[0] + "***@" + m.group().split("@")[1]),
]


@dataclass
class GuardResult:
    ok: bool                      # False = 擋下，不要送出／不要給使用者
    text: str                     # 處理後（遮罩後）的文字
    issues: List[str] = field(default_factory=list)


def mask_pii(text: str) -> Tuple[str, List[str]]:
    found = []
    for name, pattern, masker in PII_RULES:
        text, n = re.subn(pattern, masker, text)
        if n:
            found.append(f"{name} ×{n} 已遮罩")
    return text, found


def check_input(text: str, max_chars: int = 2000) -> GuardResult:
    issues = []
    if len(text) > max_chars:
        return GuardResult(False, text, [f"輸入過長（{len(text)} 字 > {max_chars}）"])
    for p in INJECTION_PATTERNS:
        if re.search(p, text, flags=re.IGNORECASE):
            return GuardResult(False, text, [f"疑似提示詞注入：符合規則 /{p[:30]}…/"])
    masked, found = mask_pii(text)
    issues += found
    return GuardResult(True, masked, issues)


def extract_json(text: str) -> Any:
    """模型常把 JSON 包在 ```json ... ``` 裡，先剝掉再解析；失敗拋 ValueError"""
    t = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, flags=re.S)
    if m:
        t = m.group(1).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        m = re.search(r"(\{.*\}|\[.*\])", t, flags=re.S)   # 前後多了說明文字時，抓第一段 JSON
        if m:
            return json.loads(m.group(1))
        raise ValueError(f"不是合法 JSON：{t[:80]}")


def check_output(text: str,
                 required_sections: Tuple[str, ...] = (),
                 banned_words: Tuple[str, ...] = (),
                 expect_json: bool = False,
                 json_keys: Tuple[str, ...] = ()) -> GuardResult:
    issues = []
    if not text or not text.strip():
        return GuardResult(False, "", ["空白回覆"])
    text, found = mask_pii(text)
    issues += found
    for w in banned_words:
        if w in text:
            text = text.replace(w, "＊" * len(w))
            issues.append(f"禁用字詞「{w}」已替換")
    missing = [s for s in required_sections if s not in text]
    if missing:
        return GuardResult(False, text, issues + [f"缺少必要段落：{'、'.join(missing)}"])
    if expect_json:
        try:
            data = extract_json(text)
        except ValueError as e:
            return GuardResult(False, text, issues + [str(e)])
        lack = [k for k in json_keys if not isinstance(data, dict) or k not in data]
        if lack:
            return GuardResult(False, text, issues + [f"JSON 缺少欄位：{'、'.join(lack)}"])
    return GuardResult(True, text, issues)


# =====================================================================
# ② 記憶層
#   短期記憶 ShortTermMemory：最近幾輪原文，超過 token 預算就把最舊的擠出去並「摘要」
#   長期記憶 LongTermMemory ：跨對話保存使用者事實／偏好（JSON 檔），每次只「檢索」相關的放進提示詞
#   MemoryManager           ：每一輪決定「哪些歷史要送、哪些要壓縮」，組出這次要送的 messages
# =====================================================================
SUMMARY_PROMPT = (
    "你是對話摘要器。請把「先前摘要」與「新增的舊對話」合併成一段新的繁體中文摘要，"
    "保留：使用者的身分與偏好、已確定的決定、尚未完成的事項、重要數字與日期；"
    "刪除：寒暄、重複內容、已被推翻的資訊。150 字以內，只輸出摘要本身。"
)


class ShortTermMemory:
    def __init__(self, client, model: str, token_budget: int = 600, summarize: bool = True,
                 logger: Optional[CallLogger] = None, keep_min_turns: int = 1):
        self.client, self.model = client, model
        self.token_budget = token_budget          # 「原文」最多佔多少 token
        self.summarize = summarize                # False = 純截斷（擠出去就忘了）
        self.keep_min_turns = keep_min_turns      # 至少保留最近幾輪原文
        self.logger = logger
        self.turns: List[Tuple[Dict, Dict]] = []  # [(user_msg, assistant_msg), ...]
        self.summary = ""
        self.dropped_total = 0

    def add_turn(self, user_text: str, assistant_text: str):
        self.turns.append(({"role": "user", "content": user_text},
                           {"role": "assistant", "content": assistant_text}))

    def messages(self) -> List[Dict[str, str]]:
        return [m for pair in self.turns for m in pair]

    def tokens(self) -> int:
        return estimate_messages_tokens(self.messages())

    def fit(self) -> int:
        """超過預算就從最舊的開始擠出去；回傳這次擠掉幾輪"""
        evicted = []
        while self.tokens() > self.token_budget and len(self.turns) > self.keep_min_turns:
            evicted.append(self.turns.pop(0))
        if evicted:
            self.dropped_total += len(evicted)
            if self.summarize:
                self._summarize([m for pair in evicted for m in pair])
        return len(evicted)

    def _summarize(self, old_messages: List[Dict[str, str]]):
        transcript = "\n".join(f"{m['role']}：{m['content']}" for m in old_messages)
        resp = call_llm(self.client, logger=self.logger, tag="memory-summary",
                        model=self.model, temperature=0.1, max_tokens=300,
                        messages=[{"role": "system", "content": SUMMARY_PROMPT},
                                  {"role": "user", "content":
                                      f"【先前摘要】\n{self.summary or '（無）'}\n\n【新增的舊對話】\n{transcript}"}])
        self.summary = (resp.choices[0].message.content or "").strip()


EXTRACT_PROMPT = (
    "從使用者這句話中，抽出「值得跨對話長期記住」的個人事實或偏好，"
    "例如：稱呼、過敏、飲食限制、語言或回答風格偏好、所在城市、職業。"
    "不要抽出一次性的問題、當下行程細節或情緒。"
    "只輸出一個 JSON 物件，key 用 2～6 字的中文名詞，value 用簡短描述；沒有就輸出 {}。"
)

# 這些 key 一旦存在就「每次都帶」，不靠檢索命中 —— 安全相關的事不能賭檢索
PINNED_KEYS = ("稱呼", "姓名", "過敏", "飲食限制", "回答風格", "回答偏好", "語言偏好")


class LongTermMemory:
    def __init__(self, path: str, user_id: str = "default"):
        self.path, self.user_id = path, user_id
        self.store: Dict[str, Dict[str, Dict[str, str]]] = {}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                self.store = json.load(f)

    @property
    def facts(self) -> Dict[str, Dict[str, str]]:
        return self.store.setdefault(self.user_id, {})

    def save(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.store, f, ensure_ascii=False, indent=2)

    def upsert(self, key: str, value: str) -> str:
        """新增或更新；更新時保留舊值，方便除錯「模型為什麼記錯」"""
        old = self.facts.get(key)
        if old and old["value"] == value:
            return "unchanged"
        self.facts[key] = {"value": value, "updated": now_str(),
                           **({"previous": old["value"]} if old else {})}
        self.save()
        return "updated" if old else "added"

    def forget(self, key: str):
        self.facts.pop(key, None)
        self.save()

    def extract_and_save(self, client, model: str, user_text: str,
                         logger: Optional[CallLogger] = None) -> Dict[str, str]:
        """多一次小呼叫：讓模型判斷這句話裡有沒有值得長期記住的事"""
        system = EXTRACT_PROMPT
        if self.facts:   # 給模型看既有的 key，同一件事才會沿用同一個 key（才會「更新」而不是重複新增）
            system += ("\n既有記憶：" + json.dumps({k: v["value"] for k, v in self.facts.items()},
                                                ensure_ascii=False) +
                       "\n若是同一件事的新資訊，請沿用既有的 key；沒有變化的不要重複輸出。")
        resp = call_llm(client, logger=logger, tag="memory-extract", verbose=False,
                        model=model, temperature=0.0, max_tokens=200,
                        messages=[{"role": "system", "content": system},
                                  {"role": "user", "content": user_text}])
        try:
            data = extract_json(resp.choices[0].message.content)
        except ValueError:
            return {}                  # 抽不出來就算了，記憶是 best-effort，不能讓主流程失敗
        changes = {}
        if isinstance(data, dict):
            for k, v in data.items():
                if isinstance(k, str) and isinstance(v, (str, int, float)) and str(v).strip():
                    status = self.upsert(k.strip(), str(v).strip())
                    if status != "unchanged":
                        changes[k] = f"{v}（{status}）"
        return changes

    @staticmethod
    def _bigrams(s: str) -> set:
        s = re.sub(r"\s+", "", s.lower())
        return {s[i:i + 2] for i in range(len(s) - 1)}

    def retrieve(self, query: str, k: int = 3) -> Dict[str, str]:
        """
        檢索：釘選的事實一定帶 + 其餘依「字元雙字組重疊」挑最相關的 k 條。
        這是最陽春的關鍵字檢索；事實多了以後改用向量檢索（見 09_context_window.py rag）
        """
        pinned = {key: f["value"] for key, f in self.facts.items() if key in PINNED_KEYS}
        q = self._bigrams(query)
        scored = sorted(((len(q & self._bigrams(key + v["value"])), key, v["value"])
                         for key, v in self.facts.items() if key not in pinned), reverse=True)
        picked = {key: val for score, key, val in scored[:k] if score > 0}
        return {**pinned, **picked}


class MemoryManager:
    """每一輪：檢索長期記憶 + 摘要 + 最近幾輪原文 → 組出這次要送的 messages"""

    def __init__(self, client, model: str, system_prompt: str,
                 short_term: ShortTermMemory, long_term: Optional[LongTermMemory] = None,
                 logger: Optional[CallLogger] = None, budget: Optional[TokenBudget] = None):
        self.client, self.model, self.system_prompt = client, model, system_prompt
        self.short, self.long = short_term, long_term
        self.logger, self.budget = logger, budget

    def build_messages(self, user_text: str) -> Tuple[List[Dict[str, str]], Dict[str, str]]:
        system = self.system_prompt
        facts = self.long.retrieve(user_text) if self.long else {}
        if facts:
            system += "\n\n【關於這位使用者（長期記憶）】\n" + "\n".join(f"- {k}：{v}" for k, v in facts.items())
        if self.short.summary:
            system += "\n\n【先前對話摘要】\n" + self.short.summary
        msgs = [{"role": "system", "content": system}] + self.short.messages() + \
               [{"role": "user", "content": user_text}]
        return msgs, facts

    def chat(self, user_text: str, **kwargs) -> Dict[str, Any]:
        messages, facts = self.build_messages(user_text)
        resp = call_llm(self.client, logger=self.logger, budget=self.budget, tag="memory-chat",
                        model=self.model, messages=messages,
                        **{"temperature": 0.3, "max_tokens": 400, **kwargs})
        answer = resp.choices[0].message.content or ""
        # 呼叫成功之後才寫入記憶（同 06_multi_turn 的 ask_and_append 原則）
        self.short.add_turn(user_text, answer)
        dropped = self.short.fit()
        learned = self.long.extract_and_save(self.client, self.model, user_text, self.logger) if self.long else {}
        return {"answer": answer, "usage": resp.usage, "sent_messages": len(messages),
                "retrieved": facts, "dropped_turns": dropped, "learned": learned}


# =====================================================================
# ③ 工具層：註冊表 + 三層防護 + 人工確認關卡
#    「工具能做的事就是模型能做的事」→ 刪除、付款、寄信這類動作一律要人核准
# =====================================================================
@dataclass
class Tool:
    name: str
    fn: Callable[..., Dict[str, Any]]
    description: str
    parameters: Dict[str, Any]
    requires_approval: bool = False

    def schema(self) -> Dict[str, Any]:
        return {"type": "function",
                "function": {"name": self.name, "description": self.description, "parameters": self.parameters}}


def console_approver(name: str, args: Dict[str, Any]) -> bool:
    """預設的人工確認：在終端機問 y/N；非互動環境（沒有 stdin）一律拒絕"""
    print(f"\n  ⚠️  模型要求執行敏感動作：{name}")
    print("     參數：" + json.dumps(args, ensure_ascii=False))
    try:
        return input("     是否核准？[y/N] ").strip().lower() == "y"
    except EOFError:
        print("     （非互動環境，預設拒絕）")
        return False


class ToolRegistry:
    def __init__(self, approver: Callable[[str, Dict[str, Any]], bool] = console_approver):
        self.tools: Dict[str, Tool] = {}
        self.approver = approver

    def register(self, tool: Tool):
        self.tools[tool.name] = tool
        return self

    def schemas(self) -> List[Dict[str, Any]]:
        return [t.schema() for t in self.tools.values()]

    def execute(self, tool_call) -> Dict[str, Any]:
        name, raw = tool_call.function.name, tool_call.function.arguments
        try:                                                    # 防護 1：參數 JSON
            args = json.loads(raw or "{}")
        except json.JSONDecodeError:
            return {"error": f"invalid JSON arguments for {name}", "raw_arguments": raw}
        tool = self.tools.get(name)                             # 防護 2：白名單
        if tool is None:
            return {"error": f"unknown function: {name}"}
        if tool.requires_approval and not self.approver(name, args):   # 人工確認關卡
            return {"error": "rejected_by_human",
                    "message": "使用者未核准此動作。請不要重試，改在回覆中說明需要人工處理。"}
        try:                                                    # 防護 3：執行例外
            return tool.fn(**args)
        except TypeError as e:
            return {"error": f"bad arguments for {name}: {e}"}
        except Exception as e:
            return {"error": f"tool execution failed for {name}: {e}"}


# =====================================================================
# ④ 控制迴圈：Agent Loop（Think → Act → Observe）+ 停止條件 + 每一步留紀錄
# =====================================================================
@dataclass
class StopConfig:
    max_steps: int = 8              # 最多問模型幾次
    max_total_tokens: int = 12_000  # 這次任務最多燒多少 token
    max_seconds: float = 90.0       # 這次任務最長耗時
    max_repeat: int = 1             # 同一工具 + 同一組參數最多執行幾次（超過就中止）
    max_tool_errors: int = 3        # 工具累計失敗幾次就中止
    finalize_on_stop: bool = True   # 被停下時，是否再問一次（不帶 tools）請模型就現有資料收尾


STOP_REASON_TEXT = {
    "answered": "模型給出最終答案",
    "max_steps": "達到最大步數",
    "token_budget": "達到 token 上限",
    "timeout": "超過最長耗時",
    "repeated_tool": "偵測到重複呼叫相同工具與參數",
    "tool_errors": "工具失敗次數過多",
    "api_error": "API 呼叫失敗",
}


class AgentLoop:
    def __init__(self, client, model: str, registry: ToolRegistry,
                 stop: Optional[StopConfig] = None, logger: Optional[CallLogger] = None,
                 verbose: bool = True, temperature: float = 0.1, max_tokens: int = 800):
        self.client, self.model, self.registry = client, model, registry
        self.stop = stop or StopConfig()
        self.logger = logger or LOGGER
        self.verbose = verbose
        self.temperature, self.max_tokens = temperature, max_tokens

    def _say(self, *a):
        if self.verbose:
            print(*a)

    def run(self, messages: List[Dict[str, Any]]) -> Dict[str, Any]:
        messages = list(messages)
        cfg = self.stop
        budget = TokenBudget(max_total_tokens=cfg.max_total_tokens, max_calls=cfg.max_steps + 1)
        t_start = time.perf_counter()
        seen: Counter = Counter()
        tool_errors = 0
        trace: List[Dict[str, Any]] = []
        final_answer, stop_reason = "", "max_steps"

        for step in range(1, cfg.max_steps + 1):
            elapsed = time.perf_counter() - t_start
            if elapsed > cfg.max_seconds:                         # 停止條件：最長耗時
                stop_reason = "timeout"
                break
            try:
                # ---- Think：讓模型讀目前狀態，決定下一步 ----
                resp = call_llm(self.client, logger=self.logger, budget=budget, tag=f"agent-step{step}",
                                verbose=self.verbose, model=self.model, messages=messages,
                                tools=self.registry.schemas(),
                                temperature=self.temperature, max_tokens=self.max_tokens)
            except BudgetExceeded as e:                           # 停止條件：token 上限
                stop_reason = "token_budget"
                self._say(f"  ⛔ {e}")
                break
            except Exception as e:
                stop_reason = "api_error"
                self._say(f"  ⛔ API 錯誤：{type(e).__name__}: {e}")
                break

            msg = resp.choices[0].message
            record = {"step": step, "elapsed_s": round(time.perf_counter() - t_start, 2),
                      "tokens_used": budget.used, "finish_reason": resp.choices[0].finish_reason}

            if not msg.tool_calls:                                # 模型不再叫工具 → 完成
                final_answer = msg.content or ""
                messages.append({"role": "assistant", "content": final_answer})
                trace.append({**record, "think": "final_answer", "content": final_answer[:200]})
                self._say(f"[步驟 {step}] Think → 直接回答（不再呼叫工具）")
                stop_reason = "answered"
                break

            messages.append(msg.model_dump(exclude_none=True))
            repeated = False
            for tc in msg.tool_calls:
                # ---- Act：harness 執行模型指定的工具 ----
                sig = (tc.function.name, _normalize_args(tc.function.arguments))
                seen[sig] += 1
                if seen[sig] > cfg.max_repeat:                    # 停止條件：重複相同工具
                    result = {"error": "repeated_call_blocked",
                              "message": f"{tc.function.name} 以相同參數已呼叫 {seen[sig] - 1} 次"}
                    repeated = True
                else:
                    result = self.registry.execute(tc)
                if isinstance(result, dict) and "error" in result:
                    tool_errors += 1
                # ---- Observe：結果以 role="tool" 寫回 messages ----
                messages.append({"role": "tool", "tool_call_id": tc.id,
                                 "content": json.dumps(result, ensure_ascii=False)})
                ok = "✗" if isinstance(result, dict) and "error" in result else "✓"
                self._say(f"[步驟 {step}] Think → {tc.function.name}({tc.function.arguments})  "
                          f"Act/Observe {ok} {json.dumps(result, ensure_ascii=False)[:90]}")
                trace.append({**record, "think": "tool_call", "tool": tc.function.name,
                              "arguments": tc.function.arguments, "result": result})

            if repeated:
                stop_reason = "repeated_tool"
                break
            if tool_errors >= cfg.max_tool_errors:                # 停止條件：工具一直失敗
                stop_reason = "tool_errors"
                break
        # for 迴圈跑完沒有 break → stop_reason 維持 "max_steps"

        if stop_reason != "answered":
            self._say(f"  ⛔ 停止：{STOP_REASON_TEXT[stop_reason]}")
            final_answer = self._finalize(messages, stop_reason, budget)

        elapsed = round(time.perf_counter() - t_start, 2)
        trace_path = self._save_trace(trace, stop_reason, elapsed, budget)
        return {"final_answer": final_answer, "stop_reason": stop_reason,
                "stop_reason_text": STOP_REASON_TEXT[stop_reason], "steps": len({t["step"] for t in trace}),
                "elapsed_s": elapsed, "tokens_used": budget.used, "messages": messages,
                "trace": trace, "trace_path": trace_path}

    def _finalize(self, messages, stop_reason, budget) -> str:
        """被停下時的「優雅收尾」：不帶 tools 再問一次，請模型用手上資料回答並說明哪裡沒完成"""
        fallback = f"（任務未完成：{STOP_REASON_TEXT[stop_reason]}，請人工確認）"
        if not self.stop.finalize_on_stop or stop_reason in ("token_budget", "api_error"):
            return fallback
        note = {"role": "user", "content":
                f"系統通知：任務因「{STOP_REASON_TEXT[stop_reason]}」被中止，不能再使用任何工具。"
                "請只根據目前已取得的資料回答，並清楚說明哪些部分沒有完成。"}
        try:
            resp = call_llm(self.client, logger=self.logger, tag="agent-finalize", verbose=self.verbose,
                            model=self.model, messages=_strip_tool_messages(messages + [note]),
                            temperature=self.temperature, max_tokens=self.max_tokens, max_retries=1)
            budget.add(resp.usage)
            return (resp.choices[0].message.content or fallback) + f"\n{fallback}"
        except Exception:
            return fallback

    def _save_trace(self, trace, stop_reason, elapsed, budget) -> str:
        os.makedirs(LOG_DIR, exist_ok=True)
        path = os.path.join(LOG_DIR, f"agent_trace_{datetime.now(TAIPEI):%Y%m%d_%H%M%S_%f}.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            for t in trace:
                f.write(json.dumps(t, ensure_ascii=False) + "\n")
            f.write(json.dumps({"summary": True, "stop_reason": stop_reason, "elapsed_s": elapsed,
                                "tokens_used": budget.used, "calls": budget.calls}, ensure_ascii=False) + "\n")
        return path


def _normalize_args(raw: str) -> str:
    """參數順序或空白不同也算「相同參數」"""
    try:
        return json.dumps(json.loads(raw or "{}"), sort_keys=True, ensure_ascii=False)
    except json.JSONDecodeError:
        return (raw or "").strip()


def _strip_tool_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    收尾呼叫不帶 tools；有些伺服器看到 tool_calls / tool 訊息卻沒有 tools 定義會報錯，
    所以把工具往返改寫成一般文字，資料內容不變。
    """
    out: List[Dict[str, Any]] = []
    for m in messages:
        if m["role"] == "tool":
            m = {"role": "user", "content": f"[工具結果] {m['content']}"}
        elif m["role"] == "assistant" and m.get("tool_calls"):
            calls = ", ".join(f"{c['function']['name']}({c['function']['arguments']})" for c in m["tool_calls"])
            m = {"role": "assistant", "content": f"[我呼叫了工具] {calls}"}
        # 相鄰同角色合併：部分模型（如 Gemma）的對話模板要求 user / assistant 交替出現
        if out and out[-1]["role"] == m["role"] and m["role"] in ("user", "assistant"):
            out[-1] = {"role": m["role"], "content": f"{out[-1]['content']}\n{m['content']}"}
        else:
            out.append(m)
    return out
