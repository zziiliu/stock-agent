# Finance Agent

**一个面向 A 股研究的多 Agent 投资分析工作台。**

![Finance Agent Web Console - 流式分析报告](docs/images/finance-agent-report-stream.png)

![Finance Agent Web Console - K 线与完整报告](docs/images/finance-agent-kline-view.png)

Finance Agent 试图把投研工作里最耗耐心的部分交给 Agent：从公开市场数据中抽取线索，调用 MCP 工具完成财务、行情与新闻检索，再把分析过程和最终报告以对话式界面呈现出来。

它不是一个简单的“问答 Demo”，而是一个正在演进中的智能投研控制台：后端负责调度 LangGraph/ReAct Agent 与数据工具，前端负责把工具调用、流式正文、K 线图和多轮对话体验组织成一个可观察、可迭代、可继续扩展的研究工作流。

## 更新日志

### 2026/6/21

* 初步测试项目。
* 将现有仓库推送到远程仓库。

### 2026/6/23

排查发现，每个 Agent 自身的 ReAct 循环存在问题。

LangGraph 默认递归限制为 25 步。通过打印工具调用流程日志，确认出现 `recursion limit` 错误主要有两个原因：

1. 转换内容为 Markdown 文档的工具未下载，导致工具调用报错。
2. Agent 一次会调用多个工具，而这些工具可能触发 Baostock 的并发登录限制，导致部分工具无法正常 login。

同时，提示词中要求 Agent “需要使用不同工具进行组合”，因此 Agent 会持续尝试调用多个工具，最终更容易触发递归步数限制。

### 2026/6/30

开始使用 `uv` 管理项目依赖。

### 2026/7/21

新增 `vue` 前后端调试控制台，用于在网页端调试 Finance Agent 的单 Agent 流式输出流程。

* 前端使用 Vue、Vite、DaisyUI 构建对话式页面，包含左侧会话边栏、底部输入框和基本面 Agent 输出区。
* 后端使用 FastAPI 提供接口，通过 SSE 将基本面 Agent 的工具调用进度和最终 Markdown 分析内容流式推送到前端。
* 新增基本面 Agent 单独运行入口，便于前端优先调试单 Agent 链路。
* 新增近一个月 K 线展示模块，前端使用 ECharts 渲染后端返回的 K 线数据。
* 新增 Agent 正文回复框和复制按钮，方便复制单次分析结果。
* 新增基本面 Agent 的轻量会话记忆，当前使用 Python 进程内数组保存最近对话，用于支持初步多轮追问。
* 完善 README 首页展示，新增前端截图和项目介绍。

### 2026/7/22

完善 `vue` 控制台的真实流式输出和多轮对话体验，当前重点打磨基本面 Agent 链路。

* 基本面 Agent 改为通过 LangGraph/ReAct 的真实消息流输出 token，前端使用 `@microsoft/fetch-event-source` 增量接收并渲染 Markdown。
* FastAPI 后端新增直接 SSE 流式接口，统一返回 `status`、`agent_progress`、`token`、`kline`、`done`、`error` 等事件。
* 前端新增请求取消、重复发送保护、生成中按钮禁用/取消、自动滚动到最新输出等交互。
* 改造基本面 Agent 的会话输入方式：参考多轮聊天项目，将最近历史转换为 `HumanMessage` / `AIMessage` 后与当前问题一起传入模型，而不是只做单轮问答。
* 优化股票标的识别逻辑，避免把“科创板”“创业板”“主板”等市场板块误判为公司名；支持“拿这支来说”“该股”“这家公司”等追问表达沿用上一轮股票。
* K 线图改为后端事件驱动渲染：后端根据当前标的或明确追问返回近一个月 K 线，普通闲聊不会误触发示例股票代码绘图。
* 前端归档单轮回复时同步保存 K 线快照，追问或感谢类回复不会清空上一轮图表。
* 优化聊天式布局，移除多余报告卡片，调整回复框、复制按钮、时间戳、侧边栏和底部输入框的显示效果。

### 2026/7/23

继续重构基本面 Agent 的 K 线渲染链路，将图表从“后端结束后自动补图”升级为 Agent 主动调用的结构化工具输出。

* 新增 MCP 工具 `get_stock_kline_data`，复用现有 Baostock DataSource、登录锁和 MCP Server，返回股票代码、近 30 个交易日 OHLCV、最新交易日和区间摘要等结构化数据。
* 基本面 Agent 将 K 线工具加入白名单，并在提示词中约束调用边界：完整分析或近期走势问题可调用，感谢、寒暄、纯概念问题不调用，默认周期固定为 30 个交易日。
* 流式链路新增 `ToolMessage` 监听：识别 `get_stock_kline_data` 工具结果后发送 `kline_data`，FastAPI 再转换为前端已有的 `kline` SSE 事件。
* 删除 `/api/run/fundamental/stream` 中根据报告正文或历史标的自动补 K 线的逻辑，避免 Agent 未调用工具时页面仍出现旧图。
* 前端将回复内容保存为按顺序排列的 `blocks`，K 线图作为当前轮自己的 `kline` block 插入；历史轮次和当前轮不再共用全局 K 线状态。
* 新增独立 `KlineChart.vue` 组件管理 ECharts 生命周期，并通过 `tool_call_id` 去重，防止同一次工具调用重复渲染图表。
* 补充走势类历史追问识别，支持“它最近走势怎么样”这类多轮问题复用上一轮股票标的并触发 K 线工具。

## 使用 uv 安装项目依赖

克隆项目后，进入项目根目录：

```bash
cd stock-agent
```

创建虚拟环境：

```bash
uv venv
```

激活虚拟环境：

```bash
source .venv/bin/activate
```

使用 `uv` 安装依赖：

```bash
uv pip install -r requirements.txt
```
## 运行项目
复制 `.env` 文件
```bash
cd stock-agent/Financial-MCP-Agent
cp .env.example .env
```

将 `.env`文件中的 key 和 url 换成个人的 key 和 url,以及使用的模型。

修改 mcp-tools的路径：
在 ` stock-agent/Financial-MCP-Agent/src/tools/mcp_config.py`下:
 ` r"/Users/mac/Finance/stock-agent/a-share-mcp-is-just-i-need"`,  

模块化运行基本面agent测试：
```bash
cd Financial-MCP-Agent
python -m src.agents.fundamental_agent
```
windows下运行所有agent测试:
```bash
cd Financial-MCP-Agent
python -m src.main --command "帮我看看茅台(600519)这只股票值得投资吗"
```
