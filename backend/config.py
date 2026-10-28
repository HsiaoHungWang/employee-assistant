"""
config.py：環境設定與 OpenAI 相容 client（金鑰只存在後端）

金鑰：讀取 backend/.env 的 MODAONE_API_KEY 或 API_KEY
⚠️ API_BASE 只保留到 /api/v1 為止，openai 套件會自己接 /chat/completions
"""
import os
from datetime import timedelta, timezone

from dotenv import load_dotenv
from openai import OpenAI

HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, ".env"))

API_BASE = os.environ.get("MODAONE_API_BASE", "https://modaone-llm-api.dginfra.gov.tw/api/v1")
MODEL = os.environ.get("MODAONE_TOOL_MODEL", "gemma-4-31B-it")
API_KEY = (os.environ.get("MODAONE_API_KEY") or os.environ.get("API_KEY") or "").strip()

# 串流開關：1 = 每一輪都串流（tool_calls 碎片自己拼回來）；0 = 一般呼叫（除錯時好用）
STREAM = os.environ.get("STREAM", "1") == "1"

# Harness 參數
MAX_STEPS = int(os.environ.get("MAX_STEPS", "6"))      # Agent Loop 最多跑幾輪
MAX_TURNS = int(os.environ.get("MAX_TURNS", "8"))      # 記憶保留最近幾輪使用者對話
MAX_INPUT_CHARS = 1000                                  # 單則訊息長度上限
TEMPERATURE = 0.2                                       # 查資料、辦事情：要穩定，不要創意
MAX_TOKENS = 1200

TAIPEI = timezone(timedelta(hours=8))
DB_PATH = os.path.join(HERE, "data", "company.db")
# 觀測紀錄由 harness_core.CallLogger 寫到 logs/llm_calls_YYYYMMDD.jsonl

# 成本護欄：每個登入工作階段的上限（一輪對話約 1,500～3,000 tokens）
SESSION_TOKEN_BUDGET = int(os.environ.get("SESSION_TOKEN_BUDGET", "60000"))
SESSION_MAX_CALLS = int(os.environ.get("SESSION_MAX_CALLS", "40"))

if not API_KEY:
    raise RuntimeError("找不到金鑰：請把 .env.example 複製成 .env 並填入 MODAONE_API_KEY")

client = OpenAI(api_key=API_KEY, base_url=API_BASE, timeout=60, max_retries=2)
