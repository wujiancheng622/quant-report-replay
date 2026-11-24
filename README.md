# Quant Report Replay

量化研报复现流水线。它把 A 股与境内 ETF 的策略研报变成可复算的含费回测，经过 V1 数值筛选和独立审计后，给出可供人工复核的模拟观察建议。

## 工作流

1. Windows 后台从本机收件目录读取已下载完成的 PDF，按内容指纹去重并入队。
2. 每篇在独立 WSL 会话内判读策略、准备数据和复现。原文未披露的细节可在有依据时补齐，并标为本地重构。规则和回放输入在回测前冻结。
3. 程序复核交易日历及扣费账本。核验完备的主账本进入正式 V1，有缺口的长回放可以保留参考分和具体待补项。
4. 正式 V1 PASS 后开独立会话做 22 项检查。已确认的严重问题拦截准入，关键缺口留待核，一般观察项随报告保留。
5. 程序把结果写入本机汇总和通知回执。邮箱已配置时可发通知。模拟账户的安排由用户决定。

## 目录

| 目录 | 内容 |
| --- | --- |
| [strategy_research_pipeline](strategy_research_pipeline/) | 收件与调度、规则冻结与回放交接、账务核验、独审材料包、准入和通知。 |
| [strategy_benchmark_v1](strategy_benchmark_v1/) | 净值评分器、V1 规则和测试。 |

## 本机配置

日常流水线以 **Windows + WSL** 为运行环境。Python 使用 3.11 或以上，首次发布在 Windows 的 Python 3.13 环境验收。

```powershell
python -m pip install -r requirements.txt
python -m pytest -q strategy_benchmark_v1
```

评分器和规则可独立使用。完整流水线还依赖本机的 Codex CLI、研究与审计技能、策略台账、授权的 Tushare 只读客户端，以及用于提取 PDF 的 Python 环境。当前代码按原部署的目录布局查找这些依赖。换机时先核对 `runner.py` 的技能、台账、WSL 和 PDF 解释器入口，以及 `market_calendar.py` 的只读取数客户端路径。

首次启动前应准备共用策略台账的 SQLite 结构，并配置研究所需的只读数据源。通知可选。已有 Windows 部署从当前用户的 `LOCALAPPDATA/QuantResearchPipeline/mail.json` 读取配置。

```powershell
python strategy_research_pipeline/smoke_workflow.py
python strategy_research_pipeline/runner.py watch
```

流程回归在已配备本地审计技能文件的 Windows 环境运行。默认测试使用合成账本和模拟 SMTP。真实运行的 PDF、数据、账本、日志和通知配置保留在本机。

## 评分与恢复

- 正式 V1 使用唯一冻结的正常成本主账本，按扣费净年化和最大回撤计算。门槛见 [benchmark_v1.json](strategy_benchmark_v1/benchmark_v1.json)。
- 取数暂未恢复、历史不足、经济事实待核和已确认回放错误分别登记。历史成绩保留其运行身份和输入指纹。
- 同一正式账本可通过 `runner.py reaudit` 获准只补独审。结果与证据追加到新的审计目录。
- 停止后台时，在运行目录创建 `STOP` 文件，等在手任务完成；恢复前移走该标记。
