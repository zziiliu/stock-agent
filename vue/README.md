# Finance Agent Web Console

这个目录是 `Finance` 项目的前后端外壳，不修改现有 Agent 代码。

## 后端

```powershell
cd D:\juliye\toy\gupiao\Finance\vue
conda activate jianji
pip install -r backend\requirements.txt
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 --reload
```

后端会在 `Financial-MCP-Agent` 目录下执行：

```powershell
python -m src.main --command "帮我看看茅台(600519)这只股票值得投资吗"
```

如果需要指定运行 Finance Agent 的 Python，可以设置：

```powershell
$env:FINANCE_AGENT_PYTHON="D:\path\to\python.exe"
```

如果这个目录暂时不在 `D:\juliye\toy\gupiao\Finance\vue` 下运行，需要额外指定 Finance 根目录：

```powershell
$env:FINANCE_DIR="D:\juliye\toy\gupiao\Finance"
```

## 前端

```powershell
cd D:\juliye\toy\gupiao\Finance\vue
npm install --cache .\.npm-cache
npm run dev
```
