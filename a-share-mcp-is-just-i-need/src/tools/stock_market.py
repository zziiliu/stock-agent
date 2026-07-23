"""
股票市场数据工具，用于MCP服务器
"""
import json
import logging
import re
from datetime import datetime, timedelta
from typing import List, Optional, Callable, Any

from mcp.server.fastmcp import FastMCP
from src.data_source_interface import FinancialDataSource, NoDataFoundError, LoginError, DataSourceError
from src.formatting.markdown_formatter import format_df_to_markdown
from src.tools.base import call_with_baostock_auth_retry

logger = logging.getLogger(__name__)


def normalize_stock_code_for_baostock(stock_code: str) -> str:
    """Normalize common A-share code formats to Baostock's sh./sz. format."""
    code = (stock_code or "").strip().lower()
    if re.fullmatch(r"(sh|sz)\.\d{6}", code):
        return code

    digits = "".join(char for char in code if char.isdigit())
    if len(digits) != 6:
        raise ValueError("stock_code must contain a 6-digit A-share code.")

    if digits.startswith("6"):
        return f"sh.{digits}"
    if digits.startswith(("0", "3")):
        return f"sz.{digits}"

    raise ValueError(
        "Only Shanghai/Shenzhen A-share codes are supported, e.g. sh.600519 or sz.300750."
    )


def parse_optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_optional_int(value: Any) -> int | None:
    numeric_value = parse_optional_float(value)
    if numeric_value is None:
        return None
    return int(numeric_value)


def dataframe_to_kline_payload(
    df,
    code: str,
    days: int,
    frequency: str,
    adjust_flag: str,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []

    for raw_row in df.tail(days).to_dict(orient="records"):
        row = {
            "date": str(raw_row.get("date", "")).strip(),
            "code": str(raw_row.get("code") or code).strip(),
            "open": parse_optional_float(raw_row.get("open")),
            "close": parse_optional_float(raw_row.get("close")),
            "low": parse_optional_float(raw_row.get("low")),
            "high": parse_optional_float(raw_row.get("high")),
            "volume": parse_optional_int(raw_row.get("volume")),
            "amount": parse_optional_float(raw_row.get("amount")),
            "pctChg": parse_optional_float(raw_row.get("pctChg")),
        }

        if not row["date"] or any(row[field] is None for field in ("open", "close", "low", "high")):
            continue

        rows.append(row)

    if not rows:
        raise NoDataFoundError(f"No valid K-line rows found for {code}.")

    closes = [float(row["close"]) for row in rows]
    highs = [float(row["high"]) for row in rows]
    lows = [float(row["low"]) for row in rows]
    first_close = closes[0]
    latest_close = closes[-1]
    period_change_pct = None
    if first_close:
        period_change_pct = round((latest_close - first_close) / first_close * 100, 2)

    return {
        "type": "kline",
        "code": code,
        "frequency": frequency,
        "adjust_flag": adjust_flag,
        "count": len(rows),
        "rows": rows,
        "latest": rows[-1],
        "summary": {
            "first_close": first_close,
            "latest_close": latest_close,
            "period_change_pct": period_change_pct,
            "period_high": max(highs),
            "period_low": min(lows),
            "total_volume": sum(row.get("volume") or 0 for row in rows),
        },
    }


def safe_data_fetch(
    func_name: str,
    data_source_func: Callable,
    *args,
    **kwargs
) -> str:
    """
    安全的数据获取函数，统一处理所有异常和错误情况
    
    参数:
        func_name: 函数名称，用于日志记录
        data_source_func: 数据源函数
        *args: 传递给数据源函数的参数
        **kwargs: 传递给数据源函数的关键字参数
        
    返回:
        Markdown格式的数据表格或错误消息
    """
    try:
        # 调用数据源函数
        df = call_with_baostock_auth_retry(func_name, lambda: data_source_func(*args, **kwargs))
        
        # 格式化结果
        logger.info(f"Successfully retrieved data for {func_name}, formatting to Markdown.")
        return format_df_to_markdown(df)
        
    except NoDataFoundError as e:
        logger.warning(f"NoDataFoundError for {func_name}: {e}")
        return f"Error: {e}"
    except LoginError as e:
        logger.error(f"LoginError for {func_name}: {e}")
        return f"Error: Could not connect to data source. {e}"
    except DataSourceError as e:
        logger.error(f"DataSourceError for {func_name}: {e}")
        return f"Error: An error occurred while fetching data. {e}"
    except ValueError as e:
        logger.warning(f"ValueError processing request for {func_name}: {e}")
        return f"Error: Invalid input parameter. {e}"
    except Exception as e:
        logger.exception(f"Unexpected Exception processing {func_name}: {e}")
        return f"Error: An unexpected error occurred: {e}"


def register_stock_market_tools(app: FastMCP, active_data_source: FinancialDataSource):
    """
    向MCP应用注册股票市场数据工具

    参数:
        app: FastMCP应用实例
        active_data_source: 活跃的金融数据源
    """

    @app.tool()
    def get_historical_k_data(
        code: str,
        start_date: str,
        end_date: str,
        frequency: str = "d",
        adjust_flag: str = "3",
        fields: Optional[List[str]] = None,
    ) -> str:
        """
        获取中国A股股票的历史K线（OHLCV）数据

        参数:
            code: Baostock格式的股票代码（例如：'sh.600000', 'sz.000001'）
            start_date: 开始日期，格式为'YYYY-MM-DD'
            end_date: 结束日期，格式为'YYYY-MM-DD'
            frequency: 数据频率。有效选项（来自Baostock）：
                         'd': 日线
                         'w': 周线
                         'm': 月线
                         '5': 5分钟
                         '15': 15分钟
                         '30': 30分钟
                         '60': 60分钟
                       默认为'd'
            adjust_flag: 价格/成交量调整标志。有效选项（来自Baostock）：
                           '1': 前复权
                           '2': 后复权
                           '3': 不复权
                         默认为'3'
            fields: 可选的具体数据字段列表（必须是有效的Baostock字段）
                    如果为None或空，将使用默认字段（例如：date, code, open, high, low, close, volume, amount, pctChg）

        返回:
            包含K线数据表的Markdown格式字符串，或错误消息
            如果结果集太大，表格可能会被截断
        """
        logger.info(
            f"Tool 'get_historical_k_data' called for {code} ({start_date}-{end_date}, freq={frequency}, adj={adjust_flag}, fields={fields})")
        
        # 验证频率和调整标志
        valid_freqs = ['d', 'w', 'm', '5', '15', '30', '60']
        valid_adjusts = ['1', '2', '3']
        if frequency not in valid_freqs:
            logger.warning(f"Invalid frequency requested: {frequency}")
            return f"Error: Invalid frequency '{frequency}'. Valid options are: {valid_freqs}"
        if adjust_flag not in valid_adjusts:
            logger.warning(f"Invalid adjust_flag requested: {adjust_flag}")
            return f"Error: Invalid adjust_flag '{adjust_flag}'. Valid options are: {valid_adjusts}"

        # 使用通用函数处理数据获取
        return safe_data_fetch(
            "get_historical_k_data",
            active_data_source.get_historical_k_data,
            code=code,
            start_date=start_date,
            end_date=end_date,
            frequency=frequency,
            adjust_flag=adjust_flag,
            fields=fields,
        )

    @app.tool()
    def get_stock_kline_data(
        stock_code: str,
        days: int = 30,
        frequency: str = "d",
        adjust_flag: str = "3",
    ) -> str:
        """
        获取中国A股股票最近若干个有效交易日的结构化K线数据，用于走势图渲染和走势解释。

        参数:
            stock_code: 股票代码，支持 '600519'、'sh.600519'、'300750'、'sz.300750' 等格式。
            days: 返回的有效交易日数量，默认30。除非用户明确要求更长或更短周期，否则必须保持30。
            frequency: K线频率，默认'd'日线；支持'd'、'w'、'm'、'5'、'15'、'30'、'60'。
            adjust_flag: 复权标志，默认'3'不复权；'1'前复权，'2'后复权，'3'不复权。

        返回:
            JSON字符串，包含code、rows、latest、summary等字段；失败时返回项目通用的Error字符串。
        """
        logger.info(
            "Tool 'get_stock_kline_data' called for %s, days=%s, freq=%s, adj=%s",
            stock_code,
            days,
            frequency,
            adjust_flag,
        )

        try:
            normalized_code = normalize_stock_code_for_baostock(stock_code)
            days = int(days)
            if days < 1 or days > 240:
                return "Error: Invalid days. Please request between 1 and 240 trading days."

            valid_freqs = ["d", "w", "m", "5", "15", "30", "60"]
            valid_adjusts = ["1", "2", "3"]
            if frequency not in valid_freqs:
                return f"Error: Invalid frequency '{frequency}'. Valid options are: {valid_freqs}"
            if adjust_flag not in valid_adjusts:
                return f"Error: Invalid adjust_flag '{adjust_flag}'. Valid options are: {valid_adjusts}"

            end_date = datetime.now().strftime("%Y-%m-%d")
            lookback_days = max(days * 3, 90)
            start_date = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
            fields = [
                "date",
                "code",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "amount",
                "pctChg",
            ]

            df = call_with_baostock_auth_retry(
                "get_stock_kline_data",
                lambda: active_data_source.get_historical_k_data(
                    code=normalized_code,
                    start_date=start_date,
                    end_date=end_date,
                    frequency=frequency,
                    adjust_flag=adjust_flag,
                    fields=fields,
                ),
            )

            payload = dataframe_to_kline_payload(
                df=df,
                code=normalized_code,
                days=days,
                frequency=frequency,
                adjust_flag=adjust_flag,
            )
            return json.dumps(payload, ensure_ascii=False)

        except NoDataFoundError as e:
            logger.warning("NoDataFoundError for get_stock_kline_data: %s", e)
            return f"Error: {e}"
        except LoginError as e:
            logger.error("LoginError for get_stock_kline_data: %s", e)
            return f"Error: Could not connect to data source. {e}"
        except DataSourceError as e:
            logger.error("DataSourceError for get_stock_kline_data: %s", e)
            return f"Error: An error occurred while fetching data. {e}"
        except ValueError as e:
            logger.warning("ValueError processing get_stock_kline_data: %s", e)
            return f"Error: Invalid input parameter. {e}"
        except Exception as e:
            logger.exception("Unexpected Exception processing get_stock_kline_data: %s", e)
            return f"Error: An unexpected error occurred: {e}"

    @app.tool()
    def get_stock_basic_info(code: str, fields: Optional[List[str]] = None) -> str:
        """
        获取给定中国A股股票的基本信息

        参数:
            code: Baostock格式的股票代码（例如：'sh.600000', 'sz.000001'）
            fields: 可选列表，用于从可用的基本信息中选择特定列
                    （例如：['code', 'code_name', 'industry', 'listingDate']）
                    如果为None或空，返回Baostock中所有可用的基本信息列

        返回:
            包含基本股票信息表的Markdown格式字符串，或错误消息
        """
        logger.info(
            f"Tool 'get_stock_basic_info' called for {code} (fields={fields})")
        
        # 使用通用函数处理数据获取
        return safe_data_fetch(
            "get_stock_basic_info",
            active_data_source.get_stock_basic_info,
            code=code,
            fields=fields,
        )

    @app.tool()
    def get_dividend_data(code: str, year: str, year_type: str = "report") -> str:
        """
        获取给定股票代码和年份的分红信息

        参数:
            code: Baostock格式的股票代码（例如：'sh.600000', 'sz.000001'）
            year: 查询年份（例如：'2023'）
            year_type: 年份类型。有效选项（来自Baostock）：
                         'report': 预案公告年份
                         'operate': 除权除息年份
                       默认为'report'

        返回:
            包含分红数据表的Markdown格式字符串，或错误消息
        """
        logger.info(
            f"Tool 'get_dividend_data' called for {code}, year={year}, year_type={year_type}")
        
        # 基本验证
        if year_type not in ['report', 'operate']:
            logger.warning(f"Invalid year_type requested: {year_type}")
            return f"Error: Invalid year_type '{year_type}'. Valid options are: 'report', 'operate'"
        if not year.isdigit() or len(year) != 4:
            logger.warning(f"Invalid year format requested: {year}")
            return f"Error: Invalid year '{year}'. Please provide a 4-digit year."

        # 使用通用函数处理数据获取
        return safe_data_fetch(
            "get_dividend_data",
            active_data_source.get_dividend_data,
            code=code,
            year=year,
            year_type=year_type,
        )

    @app.tool()
    def get_adjust_factor_data(code: str, start_date: str, end_date: str) -> str:
        """
        获取给定股票代码和日期范围的复权因子数据
        使用Baostock的"涨跌幅复权算法"因子。用于计算复权价格

        参数:
            code: Baostock格式的股票代码（例如：'sh.600000', 'sz.000001'）
            start_date: 开始日期，格式为'YYYY-MM-DD'
            end_date: 结束日期，格式为'YYYY-MM-DD'

        返回:
            包含复权因子数据表的Markdown格式字符串，或错误消息
        """
        logger.info(
            f"Tool 'get_adjust_factor_data' called for {code} ({start_date} to {end_date})")
        
        # 使用通用函数处理数据获取
        return safe_data_fetch(
            "get_adjust_factor_data",
            active_data_source.get_adjust_factor_data,
            code=code,
            start_date=start_date,
            end_date=end_date,
        )

