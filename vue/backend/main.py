from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parent
VUE_DIR = BACKEND_DIR.parent
FINANCE_DIR = Path(os.environ.get("FINANCE_DIR", VUE_DIR.parent)).resolve()
AGENT_DIR = FINANCE_DIR / "Financial-MCP-Agent"
AGENT_ENV_PATH = AGENT_DIR / ".env"
load_dotenv(
    dotenv_path=AGENT_ENV_PATH,
    override=False,
)

REPORTS_DIR = AGENT_DIR / "reports"
RUNS_DIR = BACKEND_DIR / "runs"
PENDING_RUNS: dict[str, RunRequest] = {}
SESSION_MEMORY: dict[str, list[dict[str, str]]] = {}
BAOSTOCK_LOCK = threading.Lock()
AGENT_OUTPUT_START = "::agent-output-start "
AGENT_OUTPUT_END = "::agent-output-end "
MAX_SESSION_MESSAGES = 12
MAX_SESSION_CONTENT_CHARS = 5000
GENERIC_MARKET_SUBJECTS = {
    "a股",
    "股票",
    "科创板",
    "创业板",
    "主板",
    "北交所",
    "上交所",
    "深交所",
    "沪市",
    "深市",
    "港股",
    "美股",
    "板块",
    "行业",
}


def chunk_text(text: str, chunk_size: int = 180):
    for index in range(0, len(text), chunk_size):
        yield text[index : index + chunk_size]


class RunRequest(BaseModel):
    command: str = Field(min_length=1, max_length=500)
    timeout_seconds: int = Field(default=1800, ge=60, le=7200)
    agent_mode: str = Field(default="all", pattern="^(all|fundamental)$")
    conversation_id: str = Field(default="default", min_length=1, max_length=100)


class KlineRequest(BaseModel):
    code: str = Field(default="600519", min_length=1, max_length=20)
    days: int = Field(default=240, ge=20, le=1500)
    frequency: str = Field(default="d", pattern="^(d|w|m|5|15|30|60)$")
    adjust_flag: str = Field(default="3", pattern="^[123]$")


app = FastAPI(title="Finance Agent API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def sse_event(event: str, data: dict[str, Any]) -> str:
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


def classify_output_line(line: str) -> str:
    text = line.lower()
    if "[tool call" in text:
        return "tool_call"
    if "[tool result" in text:
        return "tool_result"
    if "fundamentalagent" in text or "fundamentalagent:" in text:
        return "fundamental_agent"
    if "technicalagent" in text or "technicalagent:" in text:
        return "technical_agent"
    if "valueagent" in text or "valueagent:" in text:
        return "value_agent"
    if "newsagent" in text or "newsagent:" in text:
        return "news_agent"
    if "summaryagent" in text or "summaryagent:" in text:
        return "summary_agent"
    if "error" in text or "traceback" in text:
        return "error"
    return "log"


def summarize_fundamental_progress(line: str) -> str:
    """Turn noisy backend logs into concise user-facing agent progress."""
    if "FundamentalAgent: Fetching MCP tools" in line:
        return "正在连接 A 股数据工具..."
    if "Successfully loaded 11 tools" in line:
        return "已加载基本面分析工具。"
    if "FundamentalAgent: Calling ReAct agent" in line:
        return "正在让基本面 Agent 规划并调用数据。"
    if "Tool 'get_stock_basic_info' called" in line:
        return "正在获取公司基本信息。"
    if "Tool 'get_stock_industry' called" in line:
        return "正在获取行业分类信息。"
    if "Tool 'get_profit_data' called" in line:
        return "正在获取盈利能力数据。"
    if "Tool 'get_growth_data' called" in line:
        return "正在获取成长能力数据。"
    if "Tool 'get_balance_data' called" in line:
        return "正在获取资产负债数据。"
    if "Tool 'get_cash_flow_data' called" in line:
        return "正在获取现金流数据。"
    if "Tool 'get_operation_data' called" in line:
        return "正在获取运营效率数据。"
    if "Tool 'get_dupont_data' called" in line:
        return "正在获取杜邦分析数据。"
    if "Tool 'get_dividend_data' called" in line:
        return "正在获取历史分红数据。"
    if "ReAct agent execution completed" in line:
        return "基本面 Agent 已完成推理，正在整理输出。"
    if "Successfully completed fundamental analysis" in line:
        return "基本面分析完成。"
    return ""


def report_items() -> list[dict[str, Any]]:
    if not REPORTS_DIR.exists():
        return []

    reports = []
    for path in REPORTS_DIR.glob("*.md"):
        stat = path.stat()
        reports.append(
            {
                "filename": path.name,
                "path": str(path),
                "size": stat.st_size,
                "modified_at": datetime.fromtimestamp(stat.st_mtime).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
                "mtime": stat.st_mtime,
            }
        )

    reports.sort(key=lambda item: item["mtime"], reverse=True)
    return reports


def choose_agent_python() -> str:
    configured = os.environ.get("FINANCE_AGENT_PYTHON")
    if configured:
        return configured

    for candidate in (
        Path("D:/miniconda3/envs/jianji/python.exe"),
        Path(os.environ.get("CONDA_PREFIX", "")) / "python.exe",
        FINANCE_DIR / ".venv" / "Scripts" / "python.exe",
        AGENT_DIR / ".venv" / "Scripts" / "python.exe",
    ):
        if candidate.exists():
            return str(candidate)

    return sys.executable


def ensure_agent_import_path() -> None:
    agent_path = str(AGENT_DIR)
    if agent_path not in sys.path:
        sys.path.insert(0, agent_path)


def new_report_since(before_mtimes: dict[str, float]) -> Path | None:
    for item in report_items():
        filename = item["filename"]
        if filename not in before_mtimes or item["mtime"] > before_mtimes[filename]:
            return REPORTS_DIR / filename
    return None


def session_history_for(conversation_id: str) -> list[dict[str, str]]:
    return SESSION_MEMORY.setdefault(conversation_id, [])


def clamp_session_content(value: str) -> str:
    text = value.strip()
    if len(text) <= MAX_SESSION_CONTENT_CHARS:
        return text
    return text[:MAX_SESSION_CONTENT_CHARS].rstrip() + "\n\n[内容已截断]"


def append_session_memory(conversation_id: str, command: str, response: str) -> None:
    history = session_history_for(conversation_id)
    history.append({"role": "user", "content": clamp_session_content(command)})
    if response.strip():
        history.append({"role": "assistant", "content": clamp_session_content(response)})

    if len(history) > MAX_SESSION_MESSAGES:
        del history[: len(history) - MAX_SESSION_MESSAGES]


def extract_stock_code_from_command(command: str) -> str | None:
    prefixed_match = re.search(r"\b(?:sh|sz)\.(\d{6})\b", command.lower())
    if prefixed_match:
        return prefixed_match.group(1)

    bracket_match = re.search(r"[（(](\d{6})[)）]", command)
    if bracket_match:
        return bracket_match.group(1)

    code_match = re.search(r"\b(\d{6})\b", command)
    if code_match:
        return code_match.group(1)

    return None


def extract_stock_code_from_generated_text(text: str) -> str | None:
    def is_example_match(match: re.Match[str]) -> bool:
        prefix = text[max(0, match.start() - 18) : match.start()]
        return any(marker in prefix for marker in ("例如", "比如", "示例", "格式", "如：", "如:"))

    for prefixed_match in re.finditer(r"\b(?:sh|sz)\.(\d{6})\b", text.lower()):
        if not is_example_match(prefixed_match):
            return prefixed_match.group(1)

    for bracket_match in re.finditer(r"[（(](\d{6})[)）]", text):
        if not is_example_match(bracket_match):
            return bracket_match.group(1)

    return None


def extract_stock_code_from_history(history: list[dict[str, str]]) -> str | None:
    for item in reversed(history):
        code = extract_stock_code_from_command(item.get("content", ""))
        if code:
            return code

        prefixed_match = re.search(r"\b(?:sh|sz)\.(\d{6})\b", item.get("content", "").lower())
        if prefixed_match:
            return prefixed_match.group(1)

    return None


def is_generic_market_subject(subject: str | None) -> bool:
    if not subject:
        return False

    normalized = subject.strip().lower()
    normalized = normalized.strip("，。,.！？!?：:；; ")
    for word in ("的", "这个", "这只", "这支", "一下", "看看", "帮我", "分析"):
        normalized = normalized.replace(word, "").strip()

    return normalized in GENERIC_MARKET_SUBJECTS


def extract_stock_subject_from_command(command: str) -> str | None:
    patterns = (
        r"(?:帮我看看|给我看看|看看|分析一下|分析|研究一下|评估一下|聊聊)\s*([^0-9（）()\s，。,.！？!?]+)",
        r"([^0-9（）()\s，。,.！？!?]+)\s*(?:这只|这个|这支|的)?\s*股票",
        r"^([^0-9（）()\s，。,.！？!?]{2,12})(?:的)?(?:基本面|财务|分红|现金流|估值|风险|盈利|成长|负债|行业|情况|相关|相关话题|怎么样|怎么看|能买吗|还能买吗|能不能买|值得买吗|值得投资吗)",
    )
    follow_up_pronouns = (
        "它",
        "其",
        "该股",
        "这只",
        "这支",
        "这个",
        "这家公司",
        "这个公司",
    )

    for pattern in patterns:
        match = re.search(pattern, command)
        if not match:
            continue

        subject = match.group(1).strip()
        if subject.startswith(follow_up_pronouns) or is_generic_market_subject(subject):
            return None

        for term in (
            "基本面",
            "财务",
            "分红",
            "现金流",
            "估值",
            "风险",
            "盈利",
            "成长",
            "负债",
            "行业",
            "情况",
            "相关话题",
            "相关",
            "怎么样",
            "怎么看",
            "能买吗",
            "还能买吗",
            "能不能买",
            "值得买吗",
            "值得投资吗",
        ):
            if term in subject:
                subject = subject.split(term, 1)[0].strip()

        for word in ("的", "这个", "这只", "这支", "一下", "看看", "帮我", "分析"):
            subject = subject.replace(word, "").strip()

        if len(subject) >= 2:
            if is_generic_market_subject(subject):
                return None
            return subject

    return None


def is_follow_up_stock_question(command: str) -> bool:
    stripped = command.strip().lower()
    if not stripped:
        return False

    follow_up_markers = (
        "它",
        "其",
        "该股",
        "这只",
        "这支",
        "这只股票",
        "这支股票",
        "这家公司",
        "这个公司",
        "该公司",
        "拿这支",
        "拿这只",
        "拿它",
        "拿该股",
        "这支来说",
        "这只来说",
        "继续",
        "接着",
        "再看",
        "再看看",
        "上面",
        "刚才",
        "前面",
        "之前",
        "上一只",
    )
    analysis_terms = (
        "分红",
        "现金流",
        "估值",
        "风险",
        "财务",
        "基本面",
        "技术面",
        "盈利",
        "成长",
        "负债",
        "行业",
        "毛利率",
        "净利率",
        "roe",
        "pe",
        "pb",
        "怎么看",
        "能买吗",
        "值得",
    )
    return any(marker in stripped for marker in follow_up_markers + analysis_terms)


def should_reuse_history_stock_for_command(command: str) -> bool:
    if extract_stock_code_from_command(command):
        return False
    if extract_stock_subject_from_command(command):
        return False
    return is_follow_up_stock_question(command)


def should_preserve_kline_after_run(command: str, state_data: dict[str, Any]) -> bool:
    if state_data.get("stock_code") or state_data.get("uses_history_stock"):
        return False
    if extract_stock_code_from_command(command) or extract_stock_subject_from_command(command):
        return False
    return True


def normalize_stock_code(code: str) -> str:
    cleaned = code.strip().lower()
    if cleaned.startswith(("sh.", "sz.")):
        return cleaned
    digits = "".join(char for char in cleaned if char.isdigit())
    if len(digits) != 6:
        raise HTTPException(status_code=400, detail="Stock code must be 6 digits.")
    if digits.startswith("6"):
        return f"sh.{digits}"
    if digits.startswith(("0", "3")):
        return f"sz.{digits}"
    return digits


def resolve_kline_code(
    command: str,
    history: list[dict[str, str]] | None = None,
    stock_code: str | None = None,
    analysis_text: str | None = None,
    allow_analysis_text_code: bool = False,
) -> str | None:
    if stock_code:
        return stock_code

    code = extract_stock_code_from_command(command)
    if code:
        return code

    if analysis_text and allow_analysis_text_code:
        code = extract_stock_code_from_generated_text(analysis_text)
        if code:
            return code

    if history and should_reuse_history_stock_for_command(command):
        return extract_stock_code_from_history(history)

    return None


def moving_average(values: list[float], window: int) -> list[float | None]:
    result: list[float | None] = []
    for index in range(len(values)):
        if index + 1 < window:
            result.append(None)
            continue
        window_values = values[index + 1 - window : index + 1]
        result.append(round(sum(window_values) / window, 3))
    return result


def fetch_kline_rows_once(request: KlineRequest) -> list[dict[str, Any]]:
    try:
        import baostock as bs
    except ImportError as exc:
        raise HTTPException(status_code=500, detail="baostock is not installed.") from exc

    code = normalize_stock_code(request.code)
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=request.days * 2)).strftime("%Y-%m-%d")
    fields = "date,code,open,high,low,close,volume,amount,pctChg"

    login_result = bs.login()
    if login_result.error_code != "0":
        raise HTTPException(
            status_code=502,
            detail=f"Baostock login failed: {login_result.error_msg}",
        )

    try:
        result_set = bs.query_history_k_data_plus(
            code,
            fields,
            start_date=start_date,
            end_date=end_date,
            frequency=request.frequency,
            adjustflag=request.adjust_flag,
        )
        if result_set.error_code != "0":
            raise HTTPException(
                status_code=502,
                detail=f"Baostock query failed: {result_set.error_msg}",
            )

        rows: list[dict[str, Any]] = []
        while result_set.next():
            row = dict(zip(result_set.fields, result_set.get_row_data()))
            if not row.get("open") or not row.get("close"):
                continue
            rows.append(row)

        if not rows:
            raise HTTPException(status_code=404, detail="No K-line data found.")

        return rows[-request.days :]
    finally:
        bs.logout()


def fetch_kline_rows(request: KlineRequest) -> list[dict[str, Any]]:
    last_error: HTTPException | None = None
    with BAOSTOCK_LOCK:
        for attempt in range(2):
            try:
                return fetch_kline_rows_once(request)
            except HTTPException as exc:
                last_error = exc
                if exc.status_code != 502 or attempt == 1:
                    raise
                time.sleep(0.8)

    if last_error:
        raise last_error
    raise HTTPException(status_code=502, detail="Baostock query failed.")


def build_kline_option(rows: list[dict[str, Any]], code: str) -> dict[str, Any]:
    dates = [row["date"] for row in rows]
    ohlc = [
        [
            float(row["open"]),
            float(row["close"]),
            float(row["low"]),
            float(row["high"]),
        ]
        for row in rows
    ]
    closes = [item[1] for item in ohlc]
    volumes = [float(row.get("volume") or 0) for row in rows]

    return {
        "animation": False,
        "backgroundColor": "#ffffff",
        "title": {
            "text": f"{normalize_stock_code(code)} K-line",
            "left": 12,
            "top": 8,
            "textStyle": {"fontSize": 15, "fontWeight": 700},
        },
        "legend": {
            "top": 8,
            "right": 16,
            "data": ["K", "MA5", "MA10", "MA20"],
        },
        "tooltip": {
            "trigger": "axis",
            "axisPointer": {"type": "cross"},
            "borderWidth": 1,
        },
        "axisPointer": {
            "link": [{"xAxisIndex": "all"}],
            "label": {"backgroundColor": "#5b665d"},
        },
        "grid": [
            {"left": 52, "right": 24, "top": 54, "height": "58%"},
            {"left": 52, "right": 24, "top": "76%", "height": "13%"},
        ],
        "xAxis": [
            {
                "type": "category",
                "data": dates,
                "boundaryGap": False,
                "axisLine": {"onZero": False},
                "min": "dataMin",
                "max": "dataMax",
            },
            {
                "type": "category",
                "gridIndex": 1,
                "data": dates,
                "boundaryGap": False,
                "axisLine": {"onZero": False},
                "axisTick": {"show": False},
                "axisLabel": {"show": False},
                "min": "dataMin",
                "max": "dataMax",
            },
        ],
        "yAxis": [
            {"scale": True, "splitArea": {"show": True}},
            {"scale": True, "gridIndex": 1, "splitNumber": 2, "axisLabel": {"show": False}},
        ],
        "dataZoom": [
            {"type": "inside", "xAxisIndex": [0, 1], "start": 55, "end": 100},
            {
                "show": True,
                "type": "slider",
                "xAxisIndex": [0, 1],
                "top": "92%",
                "start": 55,
                "end": 100,
            },
        ],
        "series": [
            {
                "name": "K",
                "type": "candlestick",
                "data": ohlc,
                "itemStyle": {
                    "color": "#d84b43",
                    "color0": "#1f9d69",
                    "borderColor": "#d84b43",
                    "borderColor0": "#1f9d69",
                },
            },
            {"name": "MA5", "type": "line", "data": moving_average(closes, 5), "smooth": True, "showSymbol": False},
            {"name": "MA10", "type": "line", "data": moving_average(closes, 10), "smooth": True, "showSymbol": False},
            {"name": "MA20", "type": "line", "data": moving_average(closes, 20), "smooth": True, "showSymbol": False},
            {
                "name": "Volume",
                "type": "bar",
                "xAxisIndex": 1,
                "yAxisIndex": 1,
                "data": volumes,
                "itemStyle": {"color": "#9aa9a0"},
            },
        ],
    }


async def build_kline_payload(
    command: str,
    history: list[dict[str, str]] | None = None,
    stock_code: str | None = None,
    analysis_text: str | None = None,
    allow_analysis_text_code: bool = False,
) -> dict[str, Any] | None:
    code = resolve_kline_code(
        command,
        history,
        stock_code,
        analysis_text,
        allow_analysis_text_code=allow_analysis_text_code,
    )
    if not code:
        return None

    request = KlineRequest(code=code, days=30, frequency="d", adjust_flag="3")
    rows = await asyncio.to_thread(fetch_kline_rows, request)
    return {
        "agent": "fundamental",
        "code": normalize_stock_code(code),
        "count": len(rows),
        "latest": rows[-1],
        "option": build_kline_option(rows, code),
    }


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {
        "finance_dir": str(FINANCE_DIR),
        "agent_dir": str(AGENT_DIR),
        "reports_dir": str(REPORTS_DIR),
        "agent_python": choose_agent_python(),
        "reports_count": len(report_items()),
    }


@app.get("/api/reports")
async def reports() -> dict[str, Any]:
    return {"reports": report_items()}


@app.get("/api/reports/latest")
async def latest_report() -> dict[str, Any]:
    reports = report_items()
    if not reports:
        raise HTTPException(status_code=404, detail="没有找到报告文件")

    path = REPORTS_DIR / reports[0]["filename"]
    return {"path": str(path), "content": read_text(path)}


@app.get("/api/reports/{filename}")
async def report(filename: str) -> dict[str, Any]:
    path = (REPORTS_DIR / filename).resolve()
    reports_root = REPORTS_DIR.resolve()

    if reports_root not in path.parents or path.suffix.lower() != ".md":
        raise HTTPException(status_code=400, detail="报告路径不合法")
    if not path.exists():
        raise HTTPException(status_code=404, detail="报告不存在")

    return {"path": str(path), "content": read_text(path)}


@app.post("/api/kline")
async def kline(request: KlineRequest) -> dict[str, Any]:
    rows = await asyncio.to_thread(fetch_kline_rows, request)
    option = build_kline_option(rows, request.code)
    return {
        "code": normalize_stock_code(request.code),
        "count": len(rows),
        "option": option,
        "latest": rows[-1],
    }


@app.post("/api/run/fundamental/stream")
async def stream_fundamental_direct(
    request: RunRequest,
    http_request: Request,
) -> StreamingResponse:
    async def event_generator():
        if not AGENT_DIR.exists():
            yield sse_event("error", {"message": f"Agent dir does not exist: {AGENT_DIR}"})
            yield sse_event("done", {"message": "failed", "ok": False})
            return

        ensure_agent_import_path()

        try:
            from src.agents.fundamental_agent import stream_fundamental_agent
            from src.single_agent_runner import (
                build_fundamental_state,
                write_single_agent_report,
            )
            from src.utils.execution_logger import (
                finalize_execution_logger,
                initialize_execution_logger,
            )
        except Exception as exc:
            yield sse_event("error", {"message": f"Unable to import Agent modules: {exc}"})
            yield sse_event("done", {"message": "failed", "ok": False})
            return

        history = session_history_for(request.conversation_id)[-MAX_SESSION_MESSAGES:]
        state = build_fundamental_state(request.command.strip(), history)
        analysis_parts: list[str] = []
        final_output = ""
        report_path: Path | None = None
        logger_initialized = False

        try:
            RUNS_DIR.mkdir(parents=True, exist_ok=True)
            initialize_execution_logger(str(AGENT_DIR / "logs"))
            logger_initialized = True

            yield sse_event(
                "status",
                {
                    "message": "starting",
                    "command": request.command,
                    "agent": "fundamental",
                },
            )
            yield sse_event(
                "agent_status",
                {
                    "agent": "fundamental",
                    "status": "streaming",
                    "time": datetime.now().isoformat(),
                },
            )

            async for item in stream_fundamental_agent(state):
                if await http_request.is_disconnected():
                    raise asyncio.CancelledError

                event_name = item.get("event", "status")
                data = item.get("data", {})

                if event_name == "token":
                    content = str(data.get("content", ""))
                    if content:
                        analysis_parts.append(content)
                        yield sse_event(
                            "token",
                            {
                                "agent": data.get("agent", "fundamental"),
                                "content": content,
                                "time": datetime.now().isoformat(),
                            },
                        )
                    continue

                if event_name == "final":
                    final_output = str(data.get("content", "")).strip()
                    continue

                yield sse_event(event_name, data)

            final_output = final_output or "".join(analysis_parts).strip()
            if final_output:
                report_path = write_single_agent_report(request.command, final_output)
                append_session_memory(
                    request.conversation_id,
                    request.command,
                    final_output,
                )

            if report_path:
                report_content = read_text(report_path)
                yield sse_event(
                    "report",
                    {
                        "path": str(report_path),
                        "content": report_content,
                    },
                )

            try:
                state_data = state.get("data", {})
                history_for_kline = session_history_for(request.conversation_id)
                allow_analysis_text_code = bool(
                    state_data.get("stock_code")
                    or state_data.get("company_name")
                    or state_data.get("uses_history_stock")
                )
                kline_code = resolve_kline_code(
                    request.command,
                    history_for_kline,
                    stock_code=state_data.get("stock_code"),
                    analysis_text=final_output,
                    allow_analysis_text_code=allow_analysis_text_code,
                )
                if kline_code:
                    yield sse_event(
                        "agent_progress",
                        {
                            "agent": "fundamental",
                            "message": "正在渲染近一个月 K 线。",
                        },
                    )
                kline_payload = await build_kline_payload(
                    request.command,
                    history_for_kline,
                    stock_code=kline_code,
                    analysis_text=final_output,
                    allow_analysis_text_code=allow_analysis_text_code,
                )
                if kline_payload:
                    yield sse_event("kline", kline_payload)
                else:
                    yield sse_event(
                        "kline_state",
                        {
                            "agent": "fundamental",
                            "action": "preserve"
                            if should_preserve_kline_after_run(request.command, state_data)
                            else "clear",
                        },
                    )
            except Exception as exc:
                yield sse_event(
                    "kline_error",
                    {
                        "agent": "fundamental",
                        "message": str(exc) or "K-line data could not be loaded.",
                    },
                )

            if logger_initialized:
                finalize_execution_logger(success=True)
                logger_initialized = False

            yield sse_event(
                "done",
                {
                    "message": "completed",
                    "ok": True,
                    "report_path": str(report_path) if report_path else "",
                },
            )

        except asyncio.CancelledError:
            if logger_initialized:
                finalize_execution_logger(success=False, error="Client disconnected.")
            raise
        except Exception as exc:
            if logger_initialized:
                finalize_execution_logger(success=False, error=str(exc))
            yield sse_event(
                "agent_status",
                {
                    "agent": "fundamental",
                    "status": "error",
                    "time": datetime.now().isoformat(),
                },
            )
            yield sse_event("error", {"message": str(exc) or "Agent run failed."})
            yield sse_event("done", {"message": "failed", "ok": False})

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/run/start")
async def start_stream_run(request: RunRequest) -> dict[str, str]:
    run_id = uuid.uuid4().hex
    PENDING_RUNS[run_id] = request
    return {"run_id": run_id}


@app.get("/api/run/stream/{run_id}")
async def stream_run(run_id: str) -> StreamingResponse:
    request = PENDING_RUNS.pop(run_id, None)
    if request is None:
        raise HTTPException(status_code=404, detail="Run id not found or already consumed.")

    async def event_generator():
        if not AGENT_DIR.exists():
            yield sse_event("server_error", {"message": f"Agent dir does not exist: {AGENT_DIR}"})
            yield sse_event("done", {"ok": False})
            return

        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        started_at = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_file = RUNS_DIR / f"finance_agent_stream_{started_at}_{run_id}.log"
        before_mtimes = {item["filename"]: item["mtime"] for item in report_items()}

        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONPATH"] = str(AGENT_DIR)
        env["FINANCE_AGENT_HISTORY_JSON"] = json.dumps(
            session_history_for(request.conversation_id)[-MAX_SESSION_MESSAGES:],
            ensure_ascii=False,
        )

        if request.agent_mode == "fundamental":
            command = [
                choose_agent_python(),
                "-m",
                "src.single_agent_runner",
                "--agent",
                "fundamental",
                "--command",
                request.command.strip(),
            ]
        else:
            command = [
                choose_agent_python(),
                "-m",
                "src.main",
                "--command",
                request.command.strip(),
                "--output-file",
                str(output_file),
            ]

        yield sse_event(
            "status",
            {
                "message": "starting",
                "command": request.command,
                "python": command[0],
                "output_file": str(output_file),
            },
        )

        process: subprocess.Popen[str] | None = None
        active_agent_output: str | None = None
        captured_stdout_lines: list[str] = []
        analysis_text_parts: list[str] = []
        agent_analysis_sent = False
        progress_messages_sent: set[str] = set()
        try:
            process = subprocess.Popen(
                command,
                cwd=str(AGENT_DIR),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )

            yield sse_event("status", {"message": "process_started", "pid": process.pid})
            deadline = asyncio.get_running_loop().time() + request.timeout_seconds

            assert process.stdout is not None
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    process.kill()
                    await asyncio.to_thread(process.wait)
                    yield sse_event("server_error", {"message": "Finance Agent run timed out."})
                    yield sse_event("done", {"ok": False, "return_code": process.returncode})
                    return

                try:
                    line = await asyncio.wait_for(
                        asyncio.to_thread(process.stdout.readline),
                        timeout=min(1.0, remaining),
                    )
                except asyncio.TimeoutError:
                    if process.poll() is not None:
                        break
                    yield sse_event("heartbeat", {"time": datetime.now().isoformat()})
                    continue

                if line:
                    line = line.rstrip()
                    if line:
                        captured_stdout_lines.append(line)
                        normalized_line = line.lstrip("\ufeff")
                        if normalized_line.startswith(AGENT_OUTPUT_START):
                            active_agent_output = normalized_line[len(AGENT_OUTPUT_START) :].strip()
                            yield sse_event(
                                "agent_status",
                                {
                                    "agent": active_agent_output,
                                    "status": "streaming",
                                    "time": datetime.now().isoformat(),
                                },
                            )
                            continue

                        if normalized_line.startswith(AGENT_OUTPUT_END):
                            finished_agent = normalized_line[len(AGENT_OUTPUT_END) :].strip()
                            yield sse_event(
                                "agent_status",
                                {
                                    "agent": finished_agent or active_agent_output,
                                    "status": "done",
                                    "time": datetime.now().isoformat(),
                                },
                            )
                            active_agent_output = None
                            continue

                        if active_agent_output:
                            agent_analysis_sent = True
                            analysis_text_parts.append(f"{line}\n")
                            for chunk in chunk_text(f"{line}\n"):
                                yield sse_event(
                                    "agent_delta",
                                    {
                                        "agent": active_agent_output,
                                        "content": chunk,
                                        "time": datetime.now().isoformat(),
                                    },
                                )
                                await asyncio.sleep(0.01)
                            continue

                        if request.agent_mode == "fundamental":
                            progress_message = summarize_fundamental_progress(line)
                            if progress_message and progress_message not in progress_messages_sent:
                                progress_messages_sent.add(progress_message)
                                yield sse_event(
                                    "agent_status",
                                    {
                                        "agent": "fundamental",
                                        "status": "streaming",
                                        "time": datetime.now().isoformat(),
                                    },
                                )
                                yield sse_event(
                                    "agent_progress",
                                    {
                                        "agent": "fundamental",
                                        "message": progress_message,
                                        "time": datetime.now().isoformat(),
                                    },
                                )

                        yield sse_event(
                            "log",
                            {
                                "kind": classify_output_line(line),
                                "line": line,
                                "time": datetime.now().isoformat(),
                            },
                        )
                    continue

                if process.poll() is not None:
                    break
                await asyncio.sleep(0.05)

            return_code = await asyncio.to_thread(process.wait)
            report_path = new_report_since(before_mtimes)
            report_content = read_text(report_path) if report_path else ""

            if request.agent_mode == "fundamental" and not agent_analysis_sent:
                captured_stdout = "\n".join(captured_stdout_lines)
                start_marker = f"{AGENT_OUTPUT_START}fundamental"
                end_marker = f"{AGENT_OUTPUT_END}fundamental"
                start_index = captured_stdout.find(start_marker)
                end_index = captured_stdout.find(end_marker, start_index + len(start_marker))

                if start_index != -1 and end_index != -1:
                    content = captured_stdout[
                        start_index + len(start_marker) : end_index
                    ].strip()

                    if content:
                        agent_analysis_sent = True
                        analysis_text_parts.append(f"{content}\n")
                        yield sse_event(
                            "agent_status",
                            {
                                "agent": "fundamental",
                                "status": "streaming",
                                "time": datetime.now().isoformat(),
                            },
                        )
                        for chunk in chunk_text(f"{content}\n"):
                            yield sse_event(
                                "agent_delta",
                                {
                                    "agent": "fundamental",
                                    "content": chunk,
                                    "time": datetime.now().isoformat(),
                                },
                            )
                            await asyncio.sleep(0.01)
                        yield sse_event(
                            "agent_status",
                            {
                                "agent": "fundamental",
                                "status": "done",
                                "time": datetime.now().isoformat(),
                            },
                        )

            if (
                request.agent_mode == "fundamental"
                and not agent_analysis_sent
                and report_content.strip()
            ):
                analysis_text_parts.append(report_content)
                yield sse_event(
                    "agent_status",
                    {
                        "agent": "fundamental",
                        "status": "streaming",
                        "time": datetime.now().isoformat(),
                    },
                )
                for chunk in chunk_text(report_content):
                    yield sse_event(
                        "agent_delta",
                        {
                            "agent": "fundamental",
                            "content": chunk,
                            "time": datetime.now().isoformat(),
                        },
                    )
                    await asyncio.sleep(0.01)
                yield sse_event(
                    "agent_status",
                    {
                        "agent": "fundamental",
                        "status": "done",
                        "time": datetime.now().isoformat(),
                    },
                )

            if report_path:
                yield sse_event(
                    "report",
                    {
                        "path": str(report_path),
                        "content": report_content,
                    },
                )

            if request.agent_mode == "fundamental":
                try:
                    state_data = {}
                    analysis_for_kline = report_content.strip() or "".join(analysis_text_parts).strip()
                    allow_analysis_text_code = bool(
                        extract_stock_code_from_command(request.command)
                        or extract_stock_subject_from_command(request.command)
                        or should_reuse_history_stock_for_command(request.command)
                    )
                    kline_code = resolve_kline_code(
                        request.command,
                        session_history_for(request.conversation_id),
                        analysis_text=analysis_for_kline,
                        allow_analysis_text_code=allow_analysis_text_code,
                    )
                    if kline_code:
                        yield sse_event(
                            "agent_progress",
                            {
                                "agent": "fundamental",
                                "message": "正在渲染近一个月 K 线。",
                            },
                        )
                    kline_payload = await build_kline_payload(
                        request.command,
                        session_history_for(request.conversation_id),
                        stock_code=kline_code,
                        analysis_text=analysis_for_kline,
                        allow_analysis_text_code=allow_analysis_text_code,
                    )
                    if kline_payload:
                        yield sse_event("kline", kline_payload)
                    else:
                        yield sse_event(
                            "kline_state",
                            {
                                "agent": "fundamental",
                                "action": "preserve"
                                if should_preserve_kline_after_run(request.command, state_data)
                                else "clear",
                            },
                        )
                except Exception as exc:
                    yield sse_event(
                        "kline_error",
                        {
                            "agent": "fundamental",
                            "message": str(exc) or "K-line data could not be loaded.",
                        },
                    )

            if request.agent_mode == "fundamental" and return_code == 0:
                memory_response = report_content.strip() or "".join(analysis_text_parts).strip()
                append_session_memory(
                    request.conversation_id,
                    request.command,
                    memory_response,
                )

            if return_code != 0:
                yield sse_event(
                    "server_error",
                    {"message": f"Finance Agent exited with code {return_code}."},
                )

            yield sse_event(
                "done",
                {
                    "ok": return_code == 0,
                    "return_code": return_code,
                    "report_path": str(report_path) if report_path else "",
                },
            )

        except asyncio.CancelledError:
            if process and process.poll() is None:
                process.kill()
                await asyncio.to_thread(process.wait)
            raise
        except FileNotFoundError as exc:
            yield sse_event("server_error", {"message": f"Unable to start Python: {command[0]}"})
            yield sse_event("done", {"ok": False})
        except Exception as exc:
            if process and process.poll() is None:
                process.kill()
                await asyncio.to_thread(process.wait)
            yield sse_event(
                "server_error",
                {"message": str(exc) or f"{type(exc).__name__}: {exc!r}"},
            )
            yield sse_event("done", {"ok": False})

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/run")
async def run_agent(request: RunRequest) -> dict[str, Any]:
    if not AGENT_DIR.exists():
        raise HTTPException(status_code=500, detail=f"Agent 目录不存在: {AGENT_DIR}")

    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    started_at = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_file = RUNS_DIR / f"finance_agent_{started_at}.log"

    before_mtimes = {item["filename"]: item["mtime"] for item in report_items()}
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = str(AGENT_DIR)

    command = [
        choose_agent_python(),
        "-m",
        "src.main",
        "--command",
        request.command.strip(),
        "--output-file",
        str(output_file),
    ]

    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(AGENT_DIR),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        try:
            stdout_bytes, _ = await asyncio.wait_for(
                process.communicate(),
                timeout=request.timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.wait()
            raise HTTPException(status_code=504, detail="Finance Agent 运行超时") from exc

    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail=f"无法启动 Python: {command[0]}") from exc

    output = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
    report_path = new_report_since(before_mtimes)
    report_content = read_text(report_path) if report_path else ""

    if process.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=f"Finance Agent 退出码 {process.returncode}\n{output[-4000:]}",
        )

    return {
        "command": request.command,
        "return_code": process.returncode,
        "output": output,
        "output_file": str(output_file),
        "report_path": str(report_path) if report_path else "",
        "report_content": report_content,
    }
