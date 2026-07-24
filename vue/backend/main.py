from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

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

AgentId = Literal[
    "fundamental",
    "technical",
    "news",
    "value",
]

ALL_AGENT_IDS: tuple[AgentId, ...] = (
    "fundamental",
    "technical",
    "news",
    "value",
)


def chunk_text(text: str, chunk_size: int = 180):
    for index in range(0, len(text), chunk_size):
        yield text[index : index + chunk_size]


class RunRequest(BaseModel):
    command: str = Field(min_length=1, max_length=500)
    timeout_seconds: int = Field(default=1800, ge=60, le=7200)
    conversation_id: str = Field(default="default", min_length=1, max_length=100)
    target_agents: list[AgentId] | None = None
    agent_mode: str | None = Field(default=None, pattern="^(all|fundamental)$")


def resolve_target_agents(request: RunRequest) -> list[AgentId]:
    if request.target_agents is not None:
        if not request.target_agents:
            raise HTTPException(
                status_code=400,
                detail="At least one target agent is required.",
            )

        selected = set(request.target_agents)
        return [
            agent_id
            for agent_id in ALL_AGENT_IDS
            if agent_id in selected
        ]

    if request.agent_mode == "fundamental":
        return ["fundamental"]

    return list(ALL_AGENT_IDS)


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
    if "Successfully loaded" in line and "tools" in line:
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


def numeric_kline_value(row: dict[str, Any], field: str) -> float:
    value = row.get(field)
    if value in (None, ""):
        raise ValueError(f"K-line row missing field: {field}")
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"K-line field {field} is not numeric: {value}") from exc


def normalize_tool_kline_rows(rows: Any) -> list[dict[str, Any]]:
    if not isinstance(rows, list) or not rows:
        raise ValueError("K-line rows must be a non-empty list.")

    normalized_rows: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("K-line row must be an object.")
        date = str(row.get("date", "")).strip()
        if not date:
            raise ValueError("K-line row missing field: date")

        normalized_rows.append(
            {
                "date": date,
                "code": str(row.get("code", "")).strip(),
                "open": numeric_kline_value(row, "open"),
                "close": numeric_kline_value(row, "close"),
                "low": numeric_kline_value(row, "low"),
                "high": numeric_kline_value(row, "high"),
                "volume": float(row.get("volume") or 0),
                "amount": float(row.get("amount") or 0),
                "pctChg": float(row.get("pctChg") or 0),
            }
        )

    return normalized_rows


def build_kline_payload_from_tool(data: dict[str, Any]) -> dict[str, Any]:
    rows = normalize_tool_kline_rows(data.get("rows"))
    code = normalize_stock_code(str(data.get("code") or rows[-1].get("code") or ""))
    latest = data.get("latest")
    if not isinstance(latest, dict):
        latest = rows[-1]

    return {
        "agent": data.get("agent", "fundamental"),
        "code": code,
        "count": len(rows),
        "latest": latest,
        "option": build_kline_option(rows, code),
        "tool_call_id": data.get("tool_call_id", ""),
    }


def clone_agent_state(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "messages": list(state.get("messages", [])),
        "data": dict(state.get("data", {})),
        "metadata": dict(state.get("metadata", {})),
    }


def write_multi_agent_report(command: str, outputs: dict[str, str]) -> Path | None:
    sections = [
        ("fundamental", "基本面 Agent"),
        ("technical", "技术分析 Agent"),
        ("news", "新闻分析 Agent"),
        ("value", "估值分析 Agent"),
    ]
    content_parts = []
    for agent_id, title in sections:
        content = outputs.get(agent_id, "").strip()
        if content:
            content_parts.append(f"## {title}\n\n{content}")

    if not content_parts:
        return None

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = REPORTS_DIR / f"multi_agent_report_{timestamp}.md"
    path.write_text(
        f"# 多 Agent 股票分析报告\n\n"
        f"**用户问题**：{command}\n\n"
        f"{chr(10).join(content_parts)}\n",
        encoding="utf-8",
    )
    return path


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

                if event_name == "kline_data":
                    try:
                        yield sse_event("kline", build_kline_payload_from_tool(data))
                    except Exception as exc:
                        yield sse_event(
                            "kline_error",
                            {
                                "agent": data.get("agent", "fundamental"),
                                "tool_call_id": data.get("tool_call_id", ""),
                                "message": str(exc) or "K-line data could not be rendered.",
                            },
                        )
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


@app.post("/api/run/agents/stream")
async def stream_agents_direct(
    request: RunRequest,
    http_request: Request,
) -> StreamingResponse:
    target_agents = resolve_target_agents(request)

    async def event_generator():
        if not AGENT_DIR.exists():
            yield sse_event("error", {"message": f"Agent dir does not exist: {AGENT_DIR}"})
            yield sse_event("done", {"message": "failed", "ok": False})
            return

        ensure_agent_import_path()

        try:
            from src.agents.fundamental_agent import stream_fundamental_agent
            from src.agents.news_agent import stream_news_agent
            from src.agents.technical_agent import stream_technical_agent
            from src.agents.value_agent import stream_value_agent
            from src.single_agent_runner import build_fundamental_state
            from src.utils.execution_logger import (
                finalize_execution_logger,
                initialize_execution_logger,
            )
        except Exception as exc:
            yield sse_event("error", {"message": f"Unable to import Agent modules: {exc}"})
            yield sse_event("done", {"message": "failed", "ok": False})
            return

        all_agent_streams = {
            "fundamental": stream_fundamental_agent,
            "technical": stream_technical_agent,
            "news": stream_news_agent,
            "value": stream_value_agent,
        }
        agent_streams = {
            agent_id: all_agent_streams[agent_id]
            for agent_id in target_agents
        }

        history = session_history_for(request.conversation_id)[-MAX_SESSION_MESSAGES:]
        base_state = build_fundamental_state(request.command.strip(), history)
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        analysis_parts: dict[str, list[str]] = {agent_id: [] for agent_id in agent_streams}
        final_outputs: dict[str, str] = {}
        errors: dict[str, str] = {}
        logger_initialized = False

        async def forward_agent(agent_id: str, stream_func):
            try:
                async for item in stream_func(clone_agent_state(base_state)):
                    data = item.get("data", {})
                    if isinstance(data, dict):
                        data.setdefault("agent", agent_id)
                    await queue.put(
                        {
                            "agent": agent_id,
                            "event": item.get("event", "status"),
                            "data": data,
                        }
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                errors[agent_id] = str(exc) or f"{agent_id} Agent run failed."
                await queue.put(
                    {
                        "agent": agent_id,
                        "event": "agent_status",
                        "data": {
                            "agent": agent_id,
                            "status": "error",
                            "time": datetime.now().isoformat(),
                        },
                    }
                )
                await queue.put(
                    {
                        "agent": agent_id,
                        "event": "agent_error",
                        "data": {
                            "agent": agent_id,
                            "message": errors[agent_id],
                            "time": datetime.now().isoformat(),
                        },
                    }
                )
            finally:
                await queue.put(
                    {
                        "agent": agent_id,
                        "event": "_agent_done",
                        "data": {"agent": agent_id},
                    }
                )

        tasks: list[asyncio.Task] = []

        try:
            RUNS_DIR.mkdir(parents=True, exist_ok=True)
            initialize_execution_logger(str(AGENT_DIR / "logs"))
            logger_initialized = True

            yield sse_event(
                "status",
                {
                    "message": "starting",
                    "command": request.command,
                    "agent": "all",
                    "agents": target_agents,
                },
            )

            for agent_id in agent_streams:
                yield sse_event(
                    "agent_status",
                    {
                        "agent": agent_id,
                        "status": "waiting",
                        "time": datetime.now().isoformat(),
                    },
                )

            tasks = [
                asyncio.create_task(forward_agent(agent_id, stream_func))
                for agent_id, stream_func in agent_streams.items()
            ]

            completed_agents = 0
            while completed_agents < len(tasks):
                if await http_request.is_disconnected():
                    raise asyncio.CancelledError

                try:
                    item = await asyncio.wait_for(queue.get(), timeout=0.2)
                except asyncio.TimeoutError:
                    continue

                event_name = item.get("event", "status")
                data = item.get("data", {})
                agent_id = item.get("agent") or data.get("agent") or "fundamental"

                if event_name == "_agent_done":
                    completed_agents += 1
                    continue

                if event_name == "token":
                    content = str(data.get("content", ""))
                    if content:
                        analysis_parts.setdefault(agent_id, []).append(content)
                        yield sse_event(
                            "token",
                            {
                                "agent": agent_id,
                                "content": content,
                                "time": datetime.now().isoformat(),
                            },
                        )
                    continue

                if event_name == "final":
                    final_outputs[agent_id] = str(data.get("content", "")).strip()
                    continue

                if event_name == "kline_data":
                    try:
                        yield sse_event("kline", build_kline_payload_from_tool(data))
                    except Exception as exc:
                        yield sse_event(
                            "kline_error",
                            {
                                "agent": agent_id,
                                "tool_call_id": data.get("tool_call_id", ""),
                                "message": str(exc) or "K-line data could not be rendered.",
                            },
                        )
                    continue

                yield sse_event(event_name, data)

            for agent_id, parts in analysis_parts.items():
                final_outputs.setdefault(agent_id, "".join(parts).strip())

            report_path = write_multi_agent_report(request.command, final_outputs)
            combined_response = "\n\n".join(
                f"{agent_id}: {content}"
                for agent_id, content in final_outputs.items()
                if content.strip()
            )
            if combined_response:
                append_session_memory(
                    request.conversation_id,
                    request.command,
                    combined_response,
                )

            if report_path:
                yield sse_event(
                    "report",
                    {
                        "path": str(report_path),
                        "content": read_text(report_path),
                    },
                )

            if logger_initialized:
                finalize_execution_logger(success=not errors)
                logger_initialized = False

            yield sse_event(
                "done",
                {
                    "message": "completed" if not errors else "completed_with_errors",
                    "ok": not errors,
                    "target_agents": target_agents,
                    "errors": errors,
                    "report_path": str(report_path) if report_path else "",
                },
            )

        except asyncio.CancelledError:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if logger_initialized:
                finalize_execution_logger(success=False, error="Client disconnected.")
            raise
        except Exception as exc:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if logger_initialized:
                finalize_execution_logger(success=False, error=str(exc))
            message = str(exc) or "Multi-agent run failed."
            yield sse_event("error", {"message": message})
            yield sse_event(
                "done",
                {
                    "message": "failed",
                    "ok": False,
                    "target_agents": target_agents,
                    "errors": {"all": message},
                },
            )

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
