"""
FundamentalAnalysis Agent: Performs fundamental analysis of a stock using ReAct Agent framework.
基本面分析 Agent：使用ReAct Agent框架对股票进行基本面分析
"""
import os
import json
from typing import AsyncIterator, Dict, Any, List, Optional
from langchain_core.prompts import PromptTemplate
from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage, AIMessageChunk, BaseMessage, ToolMessage
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.outputs import ChatResult, ChatGeneration
from langgraph.prebuilt import create_react_agent
import time

from src.utils.state_definition import AgentState
from src.tools.mcp_client import get_mcp_tools
from src.utils.logging_config import setup_logger, ERROR_ICON, SUCCESS_ICON, WAIT_ICON
from src.utils.execution_logger import get_execution_logger
from dotenv import load_dotenv

# 从.env文件加载环境变量
load_dotenv(override=True)

logger = setup_logger(__name__)
KLINE_TOOL_NAME = "get_stock_kline_data"


def format_conversation_history(history: Any) -> str:
    if not isinstance(history, list) or not history:
        return ""

    lines = []
    for item in history[-6:]:
        if not isinstance(item, dict):
            continue

        role = "用户" if item.get("role") == "user" else "基本面 Agent"
        content = str(item.get("content", "")).strip()
        if not content:
            continue

        lines.append(f"{role}: {content[:1200]}")

    return "\n\n".join(lines)


def build_fundamental_agent_input(current_data: Dict[str, Any]) -> str:
    user_query = current_data.get("query", "")

    stock_code = current_data.get(
        "stock_code",
        "Unknown",
    )

    company_name = current_data.get(
        "company_name",
        "Unknown",
    )

    current_time_info = current_data.get(
        "current_time_info",
        "未知时间",
    )

    current_date = current_data.get(
        "current_date",
        "未知日期",
    )

    history_section = ""
    if current_data.get("conversation_history"):
        history_section = """
最近对话历史已作为独立消息提供。请结合历史理解用户本轮追问；如果本轮提到新的公司或股票主题，以本轮问题为准。
"""

    has_stock_target = company_name != "Unknown" or stock_code != "Unknown"
    if not has_stock_target:
        return f"""用户本轮问题：{user_query}

当前时间：{current_time_info}
当前日期：{current_date}
{history_section}

本轮用户没有提供明确股票标的，也不是对上一只股票的明确追问。请不要调用任何股票数据工具，不要默认沿用历史股票。请用简短自然语言回应；如果用户想做基本面分析，请提醒用户补充股票名称或股票代码。"""

    return f"""用户本轮问题：{user_query}

当前时间：{current_time_info}
当前日期：{current_date}
{history_section}

当前关注标的：{company_name}（股票代码：{stock_code}）

如果股票代码为 Unknown，但公司名明确：不要从历史对话中拿旧股票代码补全。只有在非常确定该公司唯一对应的 A 股代码时才调用工具；不确定时先请用户补充股票代码。

请先判断用户本轮问题类型：
- 如果用户要求完整基本面、财务质量、投资价值或“这只股票怎么样”，再执行完整基本面分析。
- 如果用户是在上一轮基础上追问某个具体点，例如“科创板股票有什么特殊”“拿这支来说”“分红怎么样”“风险是什么”，请围绕本轮问题回答，并结合当前关注标的，不要要求用户重复股票代码。
- 如果用户只是寒暄、确认、感谢或闲聊，请自然简短回复，不要调用股票数据工具。

完整基本面分析时可按以下方向展开：
1. 获取公司基本信息和行业背景
2. 获取最新财务报表数据（资产负债表、利润表、现金流量表）
3. 分析盈利能力指标（毛利率、净利率、ROE等）
4. 分析成长能力指标（收入增长率、利润增长率等）
5. 分析运营效率指标（应收周转率、存货周转率等）
6. 分析偿债能力指标（资产负债率、流动比率等）
7. 查询历史分红情况
8. 提供基本面综合评估和投资价值分析

重要限制：请专注于财务数据和基本面指标分析，不要使用crawl_news工具获取新闻信息。基本面分析应该基于财务报表、财务指标和公司基本面数据，而不是新闻事件。

K 线工具调用规则：
- 当用户要求完整分析，或问题涉及近期股价走势、价格波动、均线、成交量、技术表现时，可以调用 get_stock_kline_data。
- 调用 K 线工具前，先用一句简短自然语言说明为什么需要查看走势图。
- 工具返回后，结合 K 线数据继续输出分析文字，不要只调用工具而不解释结果。
- 如果用户只是感谢、寒暄、确认或普通闲聊，不要调用 K 线工具。
- 如果用户询问纯制度性、概念性问题，且不需要具体股票行情，不要调用 K 线工具。
- 同一轮针对同一只股票通常最多调用一次 K 线工具，除非用户明确要求不同周期或新的股票。
- 用户没有明确指定周期时，调用 get_stock_kline_data 必须使用 days=30，不要自行扩大到60、90或更长周期。
- 如果当前股票代码不明确，不要猜代码调用工具。

需要数据支撑时请使用可用工具获取实际数据，不要基于假设。如果某些数据无法获取，最多再调用3次工具。若某个工具没有返回有效数据，不要反复更换年份、季度或参数重试，直接说明该项数据暂缺。"""


def tool_progress_message(tool_name: str) -> str:
    messages = {
        "get_stock_basic_info": "正在获取公司基本信息。",
        "get_stock_industry": "正在获取行业分类信息。",
        "get_profit_data": "正在获取盈利能力数据。",
        "get_operation_data": "正在获取运营效率数据。",
        "get_growth_data": "正在获取成长能力数据。",
        "get_balance_data": "正在获取资产负债数据。",
        "get_cash_flow_data": "正在获取现金流数据。",
        "get_dupont_data": "正在获取杜邦分析数据。",
        "get_dividend_data": "正在获取历史分红数据。",
        "get_performance_express_report": "正在获取业绩快报数据。",
        "get_forecast_report": "正在获取业绩预告数据。",
        "get_stock_kline_data": "正在获取近一个月 K 线数据。",
    }
    return messages.get(tool_name, f"正在调用工具：{tool_name}")


def chunk_text_content(message: BaseMessage) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        if isinstance(content.get("text"), str):
            return content["text"]
        return json.dumps(content, ensure_ascii=False)
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts)
    return ""


def tool_call_chunk_value(call_chunk: Any, key: str) -> Any:
    if isinstance(call_chunk, dict):
        return call_chunk.get(key)
    return getattr(call_chunk, key, None)


def parse_kline_tool_payload(content: str) -> tuple[dict[str, Any] | None, str | None]:
    text = (content or "").strip()
    if not text:
        return None, "K 线工具没有返回内容。"
    if text.lower().startswith("error:"):
        return None, text

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"K 线工具返回 JSON 解析失败：{exc}"

    if not isinstance(payload, dict):
        return None, "K 线工具返回格式错误：顶层不是对象。"
    if payload.get("type") != "kline":
        return None, "K 线工具返回格式错误：type 不是 kline。"

    rows = payload.get("rows")
    if not isinstance(rows, list) or not rows:
        return None, "K 线工具返回格式错误：rows 为空。"

    code = payload.get("code")
    if not isinstance(code, str) or not code:
        return None, "K 线工具返回格式错误：缺少股票代码。"

    return payload, None


def kline_tool_result_event(
    message: ToolMessage,
    tool_name_by_call_id: dict[str, str],
    emitted_keys: set[str],
) -> dict[str, Any] | None:
    tool_call_id = getattr(message, "tool_call_id", "") or ""
    tool_name = getattr(message, "name", "") or tool_name_by_call_id.get(tool_call_id, "")
    if tool_name != KLINE_TOOL_NAME:
        return None

    content = chunk_text_content(message)
    payload, error = parse_kline_tool_payload(content)
    fallback_key = f"{tool_name}:{tool_call_id or content[:80]}"

    if payload:
        rows = payload.get("rows") or []
        latest = payload.get("latest") or (rows[-1] if rows else {})
        dedupe_key = tool_call_id or f"{tool_name}:{payload.get('code')}:{latest.get('date')}"
        if dedupe_key in emitted_keys:
            return None

        emitted_keys.add(dedupe_key)
        return {
            "event": "kline_data",
            "data": {
                "agent": "fundamental",
                "tool_call_id": tool_call_id,
                "code": payload.get("code"),
                "count": payload.get("count") or len(rows),
                "rows": rows,
                "latest": latest,
                "summary": payload.get("summary") or {},
            },
        }

    if fallback_key in emitted_keys:
        return None

    emitted_keys.add(fallback_key)
    return {
        "event": "kline_error",
        "data": {
            "agent": "fundamental",
            "tool_call_id": tool_call_id,
            "message": error or "K 线数据加载失败。",
        },
    }


def build_recent_conversation_messages(history: Any) -> List[BaseMessage]:
    if not isinstance(history, list):
        return []

    messages: List[BaseMessage] = []
    for item in history[-8:]:
        if not isinstance(item, dict):
            continue

        role = item.get("role")
        content = str(item.get("content", "")).strip()
        if not content:
            continue

        if role == "user":
            messages.append(HumanMessage(content=content[:900]))
        elif role == "assistant":
            messages.append(AIMessage(content=content[:1800]))

    return messages


def build_fundamental_system_message(current_data: Dict[str, Any]) -> SystemMessage:
    company_name = current_data.get("company_name") or "未明确"
    stock_code = current_data.get("stock_code") or "未明确"
    current_time_info = current_data.get("current_time_info", "未知时间")

    return SystemMessage(
        content=f"""你是一个 A 股基本面 Agent，正在进行多轮投资咨询。

当前时间：{current_time_info}
当前关注标的：{company_name}（股票代码：{stock_code}）

多轮对话规则：
1. 最近几轮用户和助手消息会作为独立 message 提供，请把它们当作真实对话历史，而不是普通文本资料。
2. 当用户说“它”“该股”“这只”“这支”“这家公司”“拿这支来说”等指代表达时，默认指向当前关注标的。
3. “科创板”“创业板”“主板”“A股”等是市场或板块，不是具体公司名。用户问这些规则时，如果同时出现“这支/该股/拿这支来说”，请结合当前关注标的回答，不要要求用户重新提供代码。
4. 如果用户明确提出新的股票名称或代码，以本轮新标的为准，不要用历史股票覆盖。
5. 如果用户只是感谢、确认或普通闲聊，请自然回复，不要调用股票数据工具。
6. 对需要事实数据支撑的问题，优先调用可用工具；对制度、板块规则、解释性问题，可以结合公开常识和当前标的直接回答。
7. 只有当问题涉及完整分析、近期走势、价格波动、均线、成交量或技术表现时，才调用 get_stock_kline_data；调用前先用一句话说明要查看走势，工具返回后继续解释图中的走势；用户没有明确指定周期时，days 必须使用30。
8. “谢谢你”“明白了”“好的”等普通回复绝对不要调用 get_stock_kline_data 或其他行情工具。"""
    )


def serialize_messages_for_log(messages: List[BaseMessage]) -> List[Dict[str, str]]:
    serialized = []
    for message in messages:
        if isinstance(message, SystemMessage):
            role = "system"
        elif isinstance(message, HumanMessage):
            role = "user"
        elif isinstance(message, AIMessage):
            role = "assistant"
        else:
            role = message.__class__.__name__
        serialized.append({"role": role, "content": chunk_text_content(message)})
    return serialized


def build_fundamental_messages(current_data: Dict[str, Any]) -> List[BaseMessage]:
    messages: List[BaseMessage] = [build_fundamental_system_message(current_data)]
    messages.extend(
        build_recent_conversation_messages(
            current_data.get("conversation_history", [])
        )
    )
    messages.append(HumanMessage(content=build_fundamental_agent_input(current_data)))
    return messages


async def stream_fundamental_agent(state: AgentState) -> AsyncIterator[dict[str, Any]]:
    """Stream real LLM chunks from the fundamental ReAct agent."""
    logger.info(
        f"{WAIT_ICON} FundamentalAgent: Starting streaming fundamental analysis."
    )
    load_dotenv(override=True)

    execution_logger = get_execution_logger()
    agent_name = "fundamental_agent"

    current_data = state.get("data", {})
    current_messages = state.get("messages", [])
    current_metadata = state.get("metadata", {})
    user_query = current_data.get("query")

    execution_logger.log_agent_start(agent_name, {
        "user_query": user_query,
        "stock_code": current_data.get("stock_code"),
        "company_name": current_data.get("company_name"),
        "input_data_keys": list(current_data.keys()),
        "streaming": True,
    })

    if not user_query:
        current_data["fundamental_analysis_error"] = "User query is missing."
        execution_logger.log_agent_complete(
            agent_name, current_data, 0, False, "User query is missing"
        )
        raise ValueError("User query is missing.")

    agent_start_time = time.time()

    try:
        api_key = os.getenv("OPENAI_COMPATIBLE_API_KEY")
        base_url = os.getenv("OPENAI_COMPATIBLE_BASE_URL")
        model_name = os.getenv("OPENAI_COMPATIBLE_MODEL")

        if not all([api_key, base_url, model_name]):
            current_data["fundamental_analysis_error"] = "Missing OpenAI environment variables."
            execution_logger.log_agent_complete(
                agent_name,
                current_data,
                time.time() - agent_start_time,
                False,
                "Missing OpenAI environment variables",
            )
            raise RuntimeError("Missing OpenAI environment variables.")

        yield {
            "event": "status",
            "data": {"message": "正在创建基本面分析模型。"},
        }

        llm = ChatOpenAI(
            model=model_name,
            api_key=api_key,
            base_url=base_url,
            temperature=0.3,
            max_tokens=6000,
            streaming=True,
        )

        input_messages = build_fundamental_messages(current_data)
        logged_input_messages = serialize_messages_for_log(input_messages)
        agent_input = input_messages[-1].content
        has_stock_target = bool(
            current_data.get("stock_code") or current_data.get("company_name")
        )
        if not has_stock_target:
            yield {
                "event": "agent_status",
                "data": {"agent": "fundamental", "status": "streaming"},
            }
            yield {
                "event": "status",
                "data": {"message": "正在生成回复。"},
            }

            start_time = time.time()
            streamed_parts: list[str] = []
            async for message_chunk in llm.astream(input_messages):
                content = chunk_text_content(message_chunk)
                if content:
                    streamed_parts.append(content)
                    yield {
                        "event": "token",
                        "data": {
                            "agent": "fundamental",
                            "content": content,
                        },
                    }

            execution_time = time.time() - start_time
            final_output = "".join(streamed_parts).strip()
            if not final_output:
                raise RuntimeError("No streamed response generated.")

            execution_logger.log_llm_interaction(
                agent_name=agent_name,
                interaction_type="direct_chat_stream",
                input_messages=logged_input_messages,
                output_content=final_output,
                model_config={
                    "model": model_name,
                    "temperature": 0.3,
                    "max_tokens": 6000,
                    "api_base": base_url,
                    "streaming": True,
                },
                execution_time=execution_time,
            )

            current_data["fundamental_analysis"] = final_output
            current_metadata["fundamental_agent_executed"] = True
            current_metadata["fundamental_agent_timestamp"] = str(time.time())
            current_metadata["fundamental_agent_execution_time"] = f"{execution_time:.2f} seconds"

            total_execution_time = time.time() - agent_start_time
            execution_logger.log_agent_complete(
                agent_name,
                {
                    "fundamental_analysis_length": len(final_output),
                    "analysis_preview": final_output[:500],
                    "llm_execution_time": execution_time,
                    "total_execution_time": total_execution_time,
                    "streamed": True,
                    "direct_chat": True,
                },
                total_execution_time,
                True,
            )

            yield {
                "event": "final",
                "data": {"agent": "fundamental", "content": final_output},
            }
            yield {
                "event": "agent_status",
                "data": {"agent": "fundamental", "status": "done"},
            }
            return

        yield {
            "event": "agent_progress",
            "data": {"agent": "fundamental", "message": "正在连接 A 股数据工具..."},
        }

        allowed_tool_names = {
            "get_stock_basic_info",
            "get_stock_industry",
            "get_profit_data",
            "get_operation_data",
            "get_growth_data",
            "get_balance_data",
            "get_cash_flow_data",
            "get_dupont_data",
            "get_dividend_data",
            "get_performance_express_report",
            "get_forecast_report",
            KLINE_TOOL_NAME,
        }

        async with get_mcp_tools() as all_mcp_tools:
            mcp_tools = [
                tool
                for tool in all_mcp_tools
                if tool.name in allowed_tool_names
            ]

            if not mcp_tools:
                current_data["fundamental_analysis_error"] = "No MCP tools available."
                execution_logger.log_agent_complete(
                    agent_name,
                    current_data,
                    time.time() - agent_start_time,
                    False,
                    "No MCP tools available",
                )
                raise RuntimeError("No MCP tools available.")

            yield {
                "event": "agent_progress",
                "data": {"agent": "fundamental", "message": "已加载基本面分析工具。"},
            }

            agent = create_react_agent(
                llm,
                mcp_tools,
            )

            logger.info(f"Agent input: {agent_input}")

            input_data = {
                "messages": input_messages
            }

            yield {
                "event": "agent_status",
                "data": {"agent": "fundamental", "status": "streaming"},
            }
            yield {
                "event": "status",
                "data": {"message": "正在执行基本面分析。"},
            }

            start_time = time.time()
            streamed_parts: list[str] = []
            sent_tool_names: set[str] = set()
            tool_name_by_call_id: dict[str, str] = {}
            emitted_kline_tool_call_ids: set[str] = set()
            tool_call_count = 0

            async for message_chunk, metadata in agent.astream(
                input_data,
                config={"recursion_limit": 50},
                stream_mode="messages",
            ):
                if isinstance(message_chunk, AIMessageChunk):
                    content = chunk_text_content(message_chunk)
                    if content:
                        streamed_parts.append(content)
                        yield {
                            "event": "token",
                            "data": {
                                "agent": "fundamental",
                                "content": content,
                            },
                        }

                    tool_call_chunks = getattr(message_chunk, "tool_call_chunks", None) or []
                    if tool_call_chunks:
                        for call_chunk in tool_call_chunks:
                            tool_name = tool_call_chunk_value(call_chunk, "name") or ""
                            tool_call_id = tool_call_chunk_value(call_chunk, "id") or ""

                            if tool_call_id and tool_name:
                                tool_name_by_call_id[tool_call_id] = tool_name

                            if tool_name and tool_name not in sent_tool_names:
                                sent_tool_names.add(tool_name)
                                tool_call_count += 1
                                progress = tool_progress_message(tool_name)
                                print(f"\n[TOOL CALL #{tool_call_count}] {tool_name}", flush=True)
                                yield {
                                    "event": "agent_progress",
                                    "data": {"agent": "fundamental", "message": progress},
                                }
                    continue

                if isinstance(message_chunk, ToolMessage):
                    kline_event = kline_tool_result_event(
                        message_chunk,
                        tool_name_by_call_id,
                        emitted_kline_tool_call_ids,
                    )
                    if kline_event:
                        yield kline_event

            execution_time = time.time() - start_time
            final_output = "".join(streamed_parts).strip()
            if not final_output:
                raise RuntimeError("No streamed analysis generated.")

            logger.info(
                f"Streaming ReAct agent execution completed in {execution_time:.2f} seconds"
            )
            logger.info(
                f"Final streamed analysis length: {len(final_output)} characters"
            )

            model_config = {
                "model": model_name,
                "temperature": 0.3,
                "max_tokens": 6000,
                "api_base": base_url,
                "streaming": True,
            }

            execution_logger.log_llm_interaction(
                agent_name=agent_name,
                interaction_type="react_agent_stream",
                input_messages=logged_input_messages,
                output_content=final_output,
                model_config=model_config,
                execution_time=execution_time,
            )

            current_data["fundamental_analysis"] = final_output
            current_metadata["fundamental_agent_executed"] = True
            current_metadata["fundamental_agent_timestamp"] = str(time.time())
            current_metadata["fundamental_agent_execution_time"] = f"{execution_time:.2f} seconds"

            total_execution_time = time.time() - agent_start_time
            execution_logger.log_agent_complete(
                agent_name,
                {
                    "fundamental_analysis_length": len(final_output),
                    "analysis_preview": final_output[:500],
                    "llm_execution_time": execution_time,
                    "total_execution_time": total_execution_time,
                    "streamed": True,
                },
                total_execution_time,
                True,
            )

            yield {
                "event": "final",
                "data": {"agent": "fundamental", "content": final_output},
            }
            yield {
                "event": "agent_status",
                "data": {"agent": "fundamental", "status": "done"},
            }

    except Exception as e:
        logger.error(
            f"{ERROR_ICON} FundamentalAgent: Streaming execution failed: {e}",
            exc_info=True,
        )
        current_data["fundamental_analysis_error"] = str(e)
        current_metadata["fundamental_agent_error"] = str(e)
        execution_logger.log_agent_complete(
            agent_name,
            current_data,
            time.time() - agent_start_time,
            False,
            str(e),
        )
        raise


async def fundamental_agent(state: AgentState) -> AgentState:
    """
    使用ReAct框架进行基本面分析，直接集成MCP工具
    
    Args:
        state: 包含用户查询的当前 Agent状态

    Returns:
        更新后的AgentState，包含基本面分析结果
    """
    logger.info(
        f"{WAIT_ICON} FundamentalAgent: Starting fundamental analysis using ReAct framework.")

    # 获取执行日志记录器，用于记录 Agent的执行过程
    execution_logger = get_execution_logger()
    agent_name = "fundamental_agent"

    # 从状态中提取当前数据、消息和元数据
    current_data = state.get("data", {})
    current_messages = state.get("messages", [])
    current_metadata = state.get("metadata", {})
    user_query = current_data.get("query")

    # 记录 Agent开始执行，包含关键信息
    execution_logger.log_agent_start(agent_name, {
        "user_query": user_query,
        "stock_code": current_data.get("stock_code"),
        "company_name": current_data.get("company_name"),
        "input_data_keys": list(current_data.keys())
    })

    # 验证用户查询是否存在
    if not user_query:
        logger.error(
            f"{ERROR_ICON} FundamentalAgent: User query is missing in state data.")
        current_data["fundamental_analysis_error"] = "User query is missing."

        # 记录 Agent执行失败
        execution_logger.log_agent_complete(
            agent_name, current_data, 0, False, "User query is missing")

        return {"data": current_data, "messages": current_messages, "metadata": current_metadata}

    # 记录 Agent开始时间，用于计算执行时长
    agent_start_time = time.time()

    try:
        # 使用API调用
        api_key = os.getenv("OPENAI_COMPATIBLE_API_KEY")
        base_url = os.getenv("OPENAI_COMPATIBLE_BASE_URL")
        model_name = os.getenv("OPENAI_COMPATIBLE_MODEL")

        # 验证必要的环境变量是否存在
        if not all([api_key, base_url, model_name]):
            logger.error(
                f"{ERROR_ICON} FundamentalAgent: Missing OpenAI environment variables.")
            current_data["fundamental_analysis_error"] = "Missing OpenAI environment variables."

            # 记录 Agent执行失败
            execution_logger.log_agent_complete(agent_name, current_data, time.time(
            ) - agent_start_time, False, "Missing OpenAI environment variables")

            return {"data": current_data, "messages": current_messages, "metadata": current_metadata}

        logger.info(
            f"{WAIT_ICON} FundamentalAgent: Creating ChatOpenAI with model {model_name}")
        # 创建LLM实例，设置合适的参数
        llm = ChatOpenAI(
            model=model_name,
            api_key=api_key,
            base_url=base_url,
            temperature=0.3,  # 较低的温度确保分析的一致性
            max_tokens=6000   # 增加token数量用于详细分析
        )

         # 2. 获取 MCP 工具集
        logger.info(
            f"{WAIT_ICON} FundamentalAgent: Fetching MCP tools..."
        )

        try:
            allowed_tool_names = {
                "get_stock_basic_info",
                "get_stock_industry",
                "get_profit_data",
                "get_operation_data",
                "get_growth_data",
                "get_balance_data",
                "get_cash_flow_data",
                "get_dupont_data",
                "get_dividend_data",
                "get_performance_express_report",
                "get_forecast_report",
                KLINE_TOOL_NAME,
            }

            # get_mcp_tools() 现在是异步上下文管理器。
            # 整个 ReAct Agent 执行期间都会复用同一个 MCP session
            # 和同一个 stdio MCP Server 子进程。
            async with get_mcp_tools() as all_mcp_tools:

                # 只保留基本面 Agent 所需的工具
                mcp_tools = [
                    tool
                    for tool in all_mcp_tools
                    if tool.name in allowed_tool_names
                ]

                if not mcp_tools:
                    logger.error(
                        f"{ERROR_ICON} "
                        f"FundamentalAgent: No MCP tools available."
                    )

                    current_data[
                        "fundamental_analysis_error"
                    ] = "No MCP tools available."

                    execution_logger.log_agent_complete(
                        agent_name,
                        current_data,
                        time.time() - agent_start_time,
                        False,
                        "No MCP tools available",
                    )

                    return {
                        "data": current_data,
                        "messages": current_messages,
                        "metadata": current_metadata,
                    }

                logger.info(
                    f"{SUCCESS_ICON} "
                    f"FundamentalAgent: Successfully loaded "
                    f"{len(mcp_tools)} tools."
                )

                # 打印可用工具名称，方便调试
                tool_names = [
                    tool.name for tool in mcp_tools
                ]

                logger.info(
                    f"Available tools: {tool_names}"
                )

                # 3. 创建 ReAct Agent
                logger.info(
                    f"{WAIT_ICON} "
                    f"FundamentalAgent: Creating ReAct agent..."
                )

                agent = create_react_agent(
                    llm,
                    mcp_tools,
                )

                # 4. 准备输入数据
                input_messages = build_fundamental_messages(current_data)
                logged_input_messages = serialize_messages_for_log(input_messages)
                agent_input = input_messages[-1].content

                logger.info(f"Agent input: {agent_input}")

                # 5. 调用ReAct Agent - 使用正确的messages格式
                logger.info(
                    f"{WAIT_ICON} FundamentalAgent: Calling ReAct agent...")
                start_time = time.time()

                # LangGraph ReAct Agent需要messages格式的输入
                input_data = {
                    "messages": input_messages
                }

                # 调用 Agent执行分析
                # response = await agent.ainvoke(input_data)


                # 调用 Agent执行分析：调试版，边执行边打印工具调用
                response = None
                tool_call_count = 0
                seen_message_ids = set()

                async for chunk in agent.astream(
                    input_data,
                    config={"recursion_limit": 50},
                    stream_mode="values"
                ):
                    response = chunk

                    messages = chunk.get("messages", [])

                    for msg in messages:
                        msg_id = getattr(msg, "id", None) or id(msg)
                        if msg_id in seen_message_ids:
                            continue
                        seen_message_ids.add(msg_id)

                        # 打印 AI 发起的工具调用
                        if isinstance(msg, AIMessage):
                            tool_calls = getattr(msg, "tool_calls", None)
                            if tool_calls:
                                for call in tool_calls:
                                    tool_call_count += 1
                                    tool_name = call.get("name", "UNKNOWN")
                                    tool_args = call.get("args", {})

                                    print(f"\n[TOOL CALL #{tool_call_count}] {tool_name}")
                                    print(f"[ARGS] {tool_args}")

                    # 打印工具返回
                        if type(msg).__name__ == "ToolMessage":
                            tool_name = getattr(msg, "name", "UNKNOWN")
                            content = getattr(msg, "content", "")
                            content_text = str(content)
                            preview = content_text[:500].replace("\n", " ")
                            print(f"[TOOL RESULT] {tool_name}, len={len(content_text)}, preview={preview}")
                print(f"\nTotal tool calls: {tool_call_count}")





            end_time = time.time()
            execution_time = end_time - start_time

            logger.info(
                f"ReAct agent execution completed in {execution_time:.2f} seconds")

            # 6. 提取分析结果
            final_output = "No analysis generated."

            if "messages" in response and isinstance(response["messages"], list):
                messages = response["messages"]
                # 查找最后一条AI消息，这通常包含最终的分析结果
                ai_messages = [
                    msg for msg in messages if isinstance(msg, AIMessage)]
                if ai_messages:
                    last_ai_message = ai_messages[-1]
                    final_output = last_ai_message.content
                    logger.info(
                        f"Successfully extracted analysis from AI message.")
                else:
                    logger.warning("No AI messages found in response")
                    # 如果没有AI消息，尝试获取所有消息的内容
                    all_content = []
                    for msg in messages:
                        if hasattr(msg, 'content') and msg.content:
                            all_content.append(str(msg.content))
                    if all_content:
                        final_output = "\n".join(all_content)
            else:
                logger.error(f"Unexpected response format: {type(response)}")
                logger.error(
                    f"Response keys: {response.keys() if isinstance(response, dict) else 'Not a dict'}")

            logger.info(
                f"Final extracted analysis length: {len(final_output)} characters")
            print("::agent-output-start fundamental", flush=True)
            print(final_output, flush=True)
            print("::agent-output-end fundamental", flush=True)
            # 7. 记录LLM交互，用于后续分析和优化
            model_config = {
                "model": model_name,
                "temperature": 0.3,
                "max_tokens": 6000,
                "api_base": base_url
            }
            
            execution_logger.log_llm_interaction(
                agent_name=agent_name,
                interaction_type="react_agent",
                input_messages=logged_input_messages,
                output_content=final_output,
                model_config=model_config,
                execution_time=execution_time
            )

            logger.info(
                f"{SUCCESS_ICON} FundamentalAgent: Successfully completed fundamental analysis.")
            
            # 8. 更新状态，保存分析结果和元数据
            current_data["fundamental_analysis"] = final_output
            current_metadata["fundamental_agent_executed"] = True
            current_metadata["fundamental_agent_timestamp"] = str(time.time())
            current_metadata["fundamental_agent_execution_time"] = f"{execution_time:.2f} seconds"

            # 9. 添加消息记录，保持对话历史
            new_message = {"role": "assistant", "content": "基本面分析已完成"}
            updated_messages = current_messages + [new_message]

            # 记录 Agent执行成功
            total_execution_time = time.time() - agent_start_time
            execution_logger.log_agent_complete(agent_name, {
                "fundamental_analysis_length": len(final_output),
                "analysis_preview": final_output[:500] if len(final_output) > 500 else final_output,
                "llm_execution_time": execution_time,
                "total_execution_time": total_execution_time
            }, total_execution_time, True)

            return {
                "data": current_data,
                "messages": updated_messages,
                "metadata": current_metadata
            }

        except Exception as e:
            logger.error(
                f"{ERROR_ICON} FundamentalAgent: Error in MCP or agent execution: {e}", exc_info=True)
            current_data[
                "fundamental_analysis_error"] = f"Error in MCP or agent execution: {e}"
            current_data["fundamental_analysis"] = f"基本面分析过程中出现错误: {str(e)}"
            current_metadata["fundamental_agent_error"] = str(e)

            # 记录 Agent执行失败
            execution_logger.log_agent_complete(
                agent_name, current_data, time.time() - agent_start_time, False, str(e))

            return {
                "data": current_data,
                "messages": current_messages,
                "metadata": current_metadata
            }

    except Exception as e:
        logger.error(
            f"{ERROR_ICON} FundamentalAgent: Error during execution: {e}", exc_info=True)
        current_data["fundamental_analysis_error"] = f"Error during execution: {e}"
        current_metadata["fundamental_agent_error"] = str(e)

        # 记录 Agent执行失败
        execution_logger.log_agent_complete(
            agent_name, current_data, time.time() - agent_start_time, False, str(e))

        return {
            "data": current_data,
            "messages": current_messages,
            "metadata": current_metadata
        }


# 本地测试函数
async def test_fundamental_agent():
    """基本面分析 Agent的测试函数"""
    from src.utils.state_definition import AgentState
    from datetime import datetime

    # 准备测试数据，包含当前时间信息
    current_datetime = datetime.now()
    current_date_cn = current_datetime.strftime("%Y年%m月%d日")
    current_date_en = current_datetime.strftime("%Y-%m-%d")
    current_weekday_cn = ["星期一", "星期二", "星期三", "星期四",
                          "星期五", "星期六", "星期日"][current_datetime.weekday()]
    current_time = current_datetime.strftime("%H:%M:%S")
    current_time_info = f"{current_date_cn} ({current_date_en}) {current_weekday_cn} {current_time}"

    # 创建测试状态，模拟真实的用户查询
    test_state = AgentState(
        messages=[],
        data={
            "query": "分析嘉友国际的财务状况",
            "stock_code": "sh.603871",
            "company_name": "嘉友国际",
            "current_date": current_date_en,
            "current_date_cn": current_date_cn,
            "current_time": current_time,
            "current_weekday_cn": current_weekday_cn,
            "current_time_info": current_time_info,
            "analysis_timestamp": current_datetime.isoformat()
        },
        metadata={}
    )

    # 运行 Agent并输出结果
    result = await fundamental_agent(test_state)
    print("Fundamental Analysis Result:")
    print(result)
    print(result.get("data", {}).get("fundamental_analysis", "No analysis found"))

    return result

if __name__ == "__main__":
    import asyncio
    asyncio.run(test_fundamental_agent())
