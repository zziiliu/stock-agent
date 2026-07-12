'''
from langchain_mcp_adapters.client import MultiServerMCPClient
from src.utils.logging_config import setup_logger, SUCCESS_ICON, ERROR_ICON, WAIT_ICON
from src.tools.mcp_config import SERVER_CONFIGS
import asyncio  # 异步操作所需，如get_tools
import json

logger = setup_logger(__name__)

_mcp_client_instance = None
_mcp_tools = None


def print_tool_details(tools):
    """打印工具的详细信息，用于调试"""
    logger.info(f"{SUCCESS_ICON} 工具详细信息:")
    for i, tool in enumerate(tools, 1):
        logger.info(f"  {i}. 工具名称: {tool.name}")
        logger.info(f"     描述: {tool.description}")

        # 打印其他可能的属性
        for attr in ['input_schema', 'parameters', 'schema']:
            if hasattr(tool, attr):
                attr_value = getattr(tool, attr)
                if attr_value:
                    logger.info(f"     {attr}: {attr_value}")

        logger.info(f"     工具类型: {type(tool)}")
        # logger.info(f"     所有属性: {dir(tool)}")
        logger.info("     " + "-" * 50)


async def get_mcp_tools():
    """
    使用定义的服务器配置初始化MultiServerMCPClient，
    并从a-share-mcp-v2服务器获取可用工具。

    返回:
        list: 从MCP服务器加载的LangChain兼容工具列表。
              如果初始化或工具加载失败，则返回空列表。
    """
    global _mcp_client_instance, _mcp_tools

    if _mcp_tools is not None:
        logger.info(f"{SUCCESS_ICON} Returning cached MCP tools.")
        return _mcp_tools

    logger.info(
        f"{WAIT_ICON} Initializing MultiServerMCPClient with config: {SERVER_CONFIGS}")
    try:
        _mcp_client_instance = MultiServerMCPClient(SERVER_CONFIGS)

        logger.info(
            f"{WAIT_ICON} Fetching tools from MCP server 'a_share_mcp_v2'...")
        # The get_tools() method is asynchronous.
        loaded_tools = await _mcp_client_instance.get_tools()

        if not loaded_tools:
            logger.warning(
                f"{ERROR_ICON} No tools loaded from MCP server 'a_share_mcp_v2'. Check server logs and configuration.")
            _mcp_tools = []  # Cache empty list on failure to load
            return []

        _mcp_tools = loaded_tools
        logger.info(
            f"{SUCCESS_ICON} Successfully loaded {len(_mcp_tools)} tools from 'a_share_mcp_v2'.")

        # # 打印工具名称列表
        # tool_names = [tool.name for tool in _mcp_tools]
        # logger.info(f"工具名称列表: {tool_names}")

        # 打印详细的工具信息
        # print_tool_details(_mcp_tools)

        return _mcp_tools

    except Exception as e:
        logger.error(
            f"{ERROR_ICON} Failed to initialize MCP client or load tools: {e}", exc_info=True)
        _mcp_tools = []  # Cache empty list on failure
        return []


async def close_mcp_client_sessions():
    """
    关闭MultiServerMCPClient管理的任何开放会话。
    如果必要，应在应用程序关闭时调用此函数。
    """
    global _mcp_client_instance
    if _mcp_client_instance:
        logger.info(f"{WAIT_ICON} Closing MCP client sessions...")
        try:
            logger.info(
                f"{SUCCESS_ICON} MCP client sessions (if any were persistently open) assumed closed or managed by library.")
            _mcp_client_instance = None   # 允许重新初始化
            global _mcp_tools
            _mcp_tools = None
        except Exception as e:
            logger.error(
                f"{ERROR_ICON} Error during MCP client session cleanup: {e}", exc_info=True)
    else:
        logger.info("MCP client was not initialized, no sessions to close.")


# 测试此模块的示例（可选，用于直接执行）
async def _main_test_mcp_client():
    logger.info("--- Testing MCP Client Tool Loading ---")
    tools = await get_mcp_tools()
    if tools:
        print(f"Successfully loaded {len(tools)} tools:")
        for tool in tools:
            print(
                f"- Name: {tool.name}")

        # 测试一个简单的工具调用（如果有合适的工具）
        if tools:
            logger.info("--- Testing Tool Call ---")
            # 尝试调用第一个工具（需要根据实际工具调整参数）
            first_tool = tools[0]
            logger.info(f"尝试调用工具: {first_tool.name}")

            # 这里需要根据实际的工具参数schema来构造测试参数
            # 暂时跳过实际调用，只是展示结构
            logger.info("工具调用测试跳过（需要实际参数）")
    else:
        print("Failed to load tools or no tools found.")

    # 测试关闭（如果适用）
    await close_mcp_client_sessions()
    logger.info("--- MCP Client Test Complete ---")

if __name__ == '__main__':
    # 这允许直接运行测试，例如：python -m src.tools.mcp_client
    # 确保您的环境已设置（例如，'uv'命令可用）。
    # E:\github\a_share_mcp的a_share_mcp服务器应该准备好运行。

    # 如果尚未配置，为测试运行设置基本日志记录
    if not logger.hasHandlers():
        import logging
        logging.basicConfig(level=logging.INFO)
        logger.info("Basic logging configured for test run.")

    asyncio.run(_main_test_mcp_client())
'''


"""
MCP Client 管理模块。

为 LangGraph Agent 提供绑定到持久 MCP ClientSession 的工具。

重要：
    get_mcp_tools() 是异步上下文管理器，必须这样使用：

        async with get_mcp_tools() as tools:
            agent = create_react_agent(llm, tools)
            response = await agent.ainvoke(...)

    Agent 的完整执行过程必须位于 async with 内部。
"""

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools

from src.tools.mcp_config import SERVER_CONFIGS
from src.utils.logging_config import (
    setup_logger,
    SUCCESS_ICON,
    ERROR_ICON,
    WAIT_ICON,
)


logger = setup_logger(__name__)


# SERVER_CONFIGS 中对应的 MCP Server 名称
DEFAULT_MCP_SERVER_NAME = "a_share_mcp_v2"


# MultiServerMCPClient 本身只保存服务器配置。
# 真正的持久连接由 client.session(...) 的上下文控制。
_mcp_client_instance: MultiServerMCPClient | None = None


def print_tool_details(tools: list[BaseTool]) -> None:
    """打印 MCP 工具详细信息，用于调试。"""
    logger.info(f"{SUCCESS_ICON} MCP 工具详细信息:")

    for index, tool in enumerate(tools, start=1):
        logger.info(f"  {index}. 工具名称: {tool.name}")
        logger.info(f"     描述: {tool.description}")
        logger.info(f"     工具类型: {type(tool)}")

        # 不同 LangChain 版本中的 schema 属性可能略有区别
        for attr_name in (
            "args_schema",
            "input_schema",
            "parameters",
            "schema",
        ):
            if not hasattr(tool, attr_name):
                continue

            try:
                attr_value = getattr(tool, attr_name)
            except Exception:
                continue

            if attr_value:
                logger.info(
                    f"     {attr_name}: {attr_value}"
                )

        logger.info("     " + "-" * 50)


def get_mcp_client() -> MultiServerMCPClient:
    """
    获取全局 MultiServerMCPClient 实例。

    注意：
        缓存 MultiServerMCPClient 不等于缓存 ClientSession。
        持久 ClientSession 由 get_mcp_tools() 上下文管理器创建。
    """
    global _mcp_client_instance

    if _mcp_client_instance is None:
        logger.info(
            f"{WAIT_ICON} Initializing MultiServerMCPClient."
        )

        _mcp_client_instance = MultiServerMCPClient(
            SERVER_CONFIGS
        )

        logger.info(
            f"{SUCCESS_ICON} MultiServerMCPClient initialized."
        )

    return _mcp_client_instance


@asynccontextmanager
async def get_mcp_tools(
    server_name: str = DEFAULT_MCP_SERVER_NAME,
) -> AsyncIterator[list[BaseTool]]:
    """
    打开一个持久 MCP ClientSession，并加载绑定到该会话的工具。

    在 async with 生命周期内：

        1. stdio MCP Server 子进程保持运行；
        2. 所有 MCP tool 复用同一个 ClientSession；
        3. tool 调用不会每次重新启动 MCP Server；
        4. 退出上下文后，session 和子进程会自动关闭。

    Args:
        server_name:
            SERVER_CONFIGS 中的 MCP Server 名称。

    Yields:
        绑定到当前持久 ClientSession 的 LangChain 工具列表。

    Raises:
        KeyError:
            server_name 不存在于 SERVER_CONFIGS。

        RuntimeError:
            MCP Server 没有返回任何工具。

        Exception:
            MCP 连接、工具加载或 session 执行期间发生异常。
    """
    if server_name not in SERVER_CONFIGS:
        available_servers = list(SERVER_CONFIGS.keys())

        raise KeyError(
            f"MCP server '{server_name}' does not exist. "
            f"Available servers: {available_servers}"
        )

    client = get_mcp_client()

    logger.info(
        f"{WAIT_ICON} Opening persistent MCP session "
        f"for server '{server_name}'..."
    )

    try:
        # 对于 stdio transport：
        # 进入这里时启动 MCP Server 子进程；
        # 退出这里时关闭 ClientSession 和子进程。
        async with client.session(server_name) as session:
            logger.info(
                f"{SUCCESS_ICON} Persistent MCP session opened "
                f"for '{server_name}'."
            )

            tools = await load_mcp_tools(session)

            if not tools:
                raise RuntimeError(
                    f"No tools were loaded from MCP server "
                    f"'{server_name}'."
                )

            logger.info(
                f"{SUCCESS_ICON} Successfully loaded "
                f"{len(tools)} tools from '{server_name}'."
            )

            tool_names = [tool.name for tool in tools]
            logger.info(
                f"MCP tools from '{server_name}': {tool_names}"
            )

            # 需要时再打开，避免日志太长
            # print_tool_details(tools)

            # Agent 的 ainvoke/astream 必须发生在 yield 尚未结束时
            yield tools

    except asyncio.CancelledError:
        logger.warning(
            f"MCP session for '{server_name}' was cancelled."
        )
        raise

    except Exception as exc:
        logger.error(
            f"{ERROR_ICON} MCP session or tool loading failed "
            f"for '{server_name}': {exc}",
            exc_info=True,
        )
        raise

    finally:
        logger.info(
            f"{WAIT_ICON} MCP session context finished "
            f"for '{server_name}'."
        )


async def close_mcp_client_sessions() -> None:
    """
    兼容旧代码的清理函数。

    持久 ClientSession 已经由 get_mcp_tools() 的 async with
    自动关闭。这里主要清除缓存的 MultiServerMCPClient 配置实例，
    方便测试或后续重新初始化。
    """
    global _mcp_client_instance

    if _mcp_client_instance is None:
        logger.info(
            "MCP client was not initialized; nothing to reset."
        )
        return

    logger.info(
        f"{WAIT_ICON} Resetting MultiServerMCPClient instance..."
    )

    _mcp_client_instance = None

    logger.info(
        f"{SUCCESS_ICON} MultiServerMCPClient instance reset."
    )


async def _main_test_mcp_client() -> None:
    """
    直接运行本文件时的测试入口。

    运行方式：
        python -m src.tools.mcp_client
    """
    logger.info("--- Testing persistent MCP session ---")

    try:
        async with get_mcp_tools() as tools:
            print(
                f"Successfully loaded {len(tools)} tools:"
            )

            for tool in tools:
                print(f"- {tool.name}")

            logger.info(
                "Persistent session is active. "
                "Tool invocation test is skipped because "
                "each tool requires different arguments."
            )

    except Exception as exc:
        logger.error(
            f"{ERROR_ICON} MCP client test failed: {exc}",
            exc_info=True,
        )

    finally:
        await close_mcp_client_sessions()
        logger.info("--- MCP Client test complete ---")


if __name__ == "__main__":
    asyncio.run(_main_test_mcp_client())