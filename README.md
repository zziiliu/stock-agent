# stock-agent

一个用于股票分析的 Agent 项目，目前主要用于调试多 Agent、ReAct 循环、工具调用流程以及 Baostock 数据源相关问题。

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
python src/main.py --command "帮我看看茅台(600519)这只股票值得投资吗"
```
