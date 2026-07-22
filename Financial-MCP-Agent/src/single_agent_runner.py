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
            r"^([^0-9（）()\s，。,.！？!?]{2,12})(?:的)?(?:基本面|财务|分红|现金流|估值|风险|盈利|成长|负债|行业|情况|相关|相关话题|怎么样|怎么看|能买吗|还能买吗|能不能买|值得买吗|值得投资吗)",
        ]
        for pattern in name_patterns:
            match = re.search(pattern, query)
            if match:
                company_name = match.group(1).strip()
                break

    if company_name:
        if (
            company_name.startswith(("它", "其", "该股", "这只", "这支", "这个", "这家公司", "这个公司"))
            or is_generic_market_subject(company_name)
        ):
            company_name = None
        else:
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
                if term in company_name:
                    company_name = company_name.split(term, 1)[0].strip()
        for word in ("的", "这个", "这只", "一下", "看看", "帮我", "分析"):
            if company_name:
                company_name = company_name.replace(word, "").strip()
        if not company_name or len(company_name) < 2:
            company_name = None
        elif is_generic_market_subject(company_name):
            company_name = None

    return company_name, stock_code


def is_generic_market_subject(subject: Optional[str]) -> bool:
    if not subject:
        return False

    normalized = subject.strip().lower()
    normalized = normalized.strip("，。,.！？!?：:；; ")
    for word in ("的", "这个", "这只", "这支", "一下", "看看", "帮我", "分析"):
        normalized = normalized.replace(word, "").strip()

    return normalized in GENERIC_MARKET_SUBJECTS


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


def should_reuse_history_stock(
    command: str,
    company_name: Optional[str],
    stock_code: Optional[str],
) -> bool:
    if company_name or stock_code:
        return False
    return is_follow_up_stock_question(command)


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


def build_fundamental_state(
    command: str,
    conversation_history: Optional[list[dict[str, str]]] = None,
) -> AgentState:
    history = conversation_history if conversation_history is not None else load_conversation_history()
    company_name, stock_code = extract_stock_info(command)
    reuse_history_stock = bool(
        history and should_reuse_history_stock(command, company_name, stock_code)
    )
    prompt_history = history

    if reuse_history_stock:
        history_company, history_code = extract_stock_info_from_history(history)
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
        "conversation_history": prompt_history,
        "uses_history_stock": reuse_history_stock,
    }

    if company_name:
        initial_data["company_name"] = company_name
    if stock_code:
        initial_data["stock_code"] = normalize_stock_code(stock_code)

    return AgentState(
        messages=[],
        data=initial_data,
        metadata={},
    )


async def run_fundamental_agent(command: str) -> int:
    load_dotenv(override=True)
    execution_logger = initialize_execution_logger()

    try:
        state = build_fundamental_state(command)
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
