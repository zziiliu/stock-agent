"""
Run one analysis agent from the command line.

This is used by the web console while debugging the streaming path. It avoids
the full LangGraph workflow so one agent can be tested in isolation.
"""

import argparse
import asyncio
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

from src.agents.fundamental_agent import fundamental_agent
from src.utils.execution_logger import (
    finalize_execution_logger,
    initialize_execution_logger,
)
from src.utils.state_definition import AgentState


AGENT_DIR = Path(__file__).resolve().parents[1]
REPORTS_DIR = AGENT_DIR / "reports"
MAX_HISTORY_MESSAGES = 12
MAX_HISTORY_CONTENT_CHARS = 2500


def load_conversation_history() -> list[dict[str, str]]:
    raw_history = os.getenv("FINANCE_AGENT_HISTORY_JSON", "[]")
    try:
        items = json.loads(raw_history)
    except json.JSONDecodeError:
        return []

    if not isinstance(items, list):
        return []

    history: list[dict[str, str]] = []
    for item in items[-MAX_HISTORY_MESSAGES:]:
        if not isinstance(item, dict):
            continue

        role = str(item.get("role", "")).strip()
        content = str(item.get("content", "")).strip()
        if role not in {"user", "assistant"} or not content:
            continue

        history.append(
            {
                "role": role,
                "content": content[:MAX_HISTORY_CONTENT_CHARS],
            }
        )

    return history


def extract_stock_info(query: str) -> tuple[Optional[str], Optional[str]]:
    company_name = None
    stock_code = None

    bracket_match = re.search(r"([^（(\s]+)\s*[（(](\d{5,6})[)）]", query)
    if bracket_match:
        company_name = bracket_match.group(1).strip()
        stock_code = bracket_match.group(2)
    else:
        code_match = re.search(r"\b(\d{5,6})\b", query)
        if code_match:
            stock_code = code_match.group(1)

        name_patterns = [
            r"帮我看看\s*([^0-9（）()\s]+)",
            r"分析一下\s*([^0-9（）()\s]+)",
            r"分析\s*([^0-9（）()\s]+)",
            r"([^0-9（）()\s]+)\s*(?:这只|这个|的)?\s*股票",
        ]
        for pattern in name_patterns:
            match = re.search(pattern, query)
            if match:
                company_name = match.group(1).strip()
                break

    if company_name:
        for word in ("的", "这个", "这只", "一下", "看看", "帮我", "分析"):
            company_name = company_name.replace(word, "").strip()
        if len(company_name) < 2:
            company_name = None

    return company_name, stock_code


def normalize_stock_code(stock_code: Optional[str]) -> Optional[str]:
    if not stock_code:
        return None
    cleaned = stock_code.strip().lower()
    if cleaned.startswith(("sh.", "sz.")):
        return cleaned
    digits = "".join(char for char in cleaned if char.isdigit())
    if len(digits) != 6:
        return cleaned
    if digits.startswith("6"):
        return f"sh.{digits}"
    if digits.startswith(("0", "3")):
        return f"sz.{digits}"
    return digits


def extract_stock_code_from_text(text: str) -> Optional[str]:
    prefixed_code = re.search(r"\b(?:sh|sz)\.(\d{6})\b", text.lower())
    if prefixed_code:
        return prefixed_code.group(1)

    bracket_code = re.search(r"[（(](\d{6})[)）]", text)
    if bracket_code:
        return bracket_code.group(1)

    bare_code = re.search(r"\b(\d{6})\b", text)
    if bare_code:
        return bare_code.group(1)

    return None


def extract_stock_info_from_history(
    history: list[dict[str, str]]
) -> tuple[Optional[str], Optional[str]]:
    fallback_code = None

    for item in reversed(history):
        content = item.get("content", "")
        stock_code = extract_stock_code_from_text(content)
        if stock_code and not fallback_code:
            fallback_code = stock_code

        if item.get("role") != "user":
            continue

        company_name, user_stock_code = extract_stock_info(content)
        if company_name or user_stock_code:
            return company_name, user_stock_code or fallback_code

    return None, fallback_code


def write_single_agent_report(command: str, analysis: str) -> Optional[Path]:
    if not analysis.strip():
        return None

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = REPORTS_DIR / f"fundamental_agent_report_{timestamp}.md"
    content = (
        f"# 基本面 Agent 分析报告\n\n"
        f"**用户问题**：{command}\n\n"
        f"{analysis.strip()}\n"
    )
    path.write_text(content, encoding="utf-8")
    return path


async def run_fundamental_agent(command: str) -> int:
    load_dotenv(override=True)
    execution_logger = initialize_execution_logger()

    try:
        conversation_history = load_conversation_history()
        company_name, stock_code = extract_stock_info(command)
        if conversation_history and (not company_name or not stock_code):
            history_company, history_code = extract_stock_info_from_history(conversation_history)
            company_name = company_name or history_company
            stock_code = stock_code or history_code

        current_datetime = datetime.now()
        current_date_cn = current_datetime.strftime("%Y年%m月%d日")
        current_date_en = current_datetime.strftime("%Y-%m-%d")
        current_weekday_cn = [
            "星期一",
            "星期二",
            "星期三",
            "星期四",
            "星期五",
            "星期六",
            "星期日",
        ][current_datetime.weekday()]
        current_time = current_datetime.strftime("%H:%M:%S")
        current_time_info = (
            f"{current_date_cn} ({current_date_en}) "
            f"{current_weekday_cn} {current_time}"
        )

        initial_data = {
            "query": command,
            "current_date": current_date_en,
            "current_date_cn": current_date_cn,
            "current_time": current_time,
            "current_weekday_cn": current_weekday_cn,
            "current_time_info": current_time_info,
            "analysis_timestamp": current_datetime.isoformat(),
            "conversation_history": conversation_history,
        }

        if company_name:
            initial_data["company_name"] = company_name
        if stock_code:
            initial_data["stock_code"] = normalize_stock_code(stock_code)

        state = AgentState(
            messages=[],
            data=initial_data,
            metadata={},
        )

        result = await fundamental_agent(state)
        analysis = result.get("data", {}).get("fundamental_analysis", "")
        report_path = write_single_agent_report(command, analysis)
        if report_path:
            print(f"::agent-report fundamental {report_path}", flush=True)

        finalize_execution_logger(success=True)
        return 0

    except Exception as exc:
        print("::agent-output-start fundamental", flush=True)
        print(f"基本面 Agent 执行失败: {exc}", flush=True)
        print("::agent-output-end fundamental", flush=True)
        finalize_execution_logger(success=False, error=str(exc))
        return 1


async def main() -> int:
    parser = argparse.ArgumentParser(description="Run a single Finance Agent")
    parser.add_argument("--agent", choices=["fundamental"], required=True)
    parser.add_argument("--command", required=True)
    args = parser.parse_args()

    if args.agent == "fundamental":
        return await run_fundamental_agent(args.command)

    print(f"Unsupported agent: {args.agent}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
