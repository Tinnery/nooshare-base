#!/usr/bin/env python3
"""从 YAML 契约自动生成数据字典、API 文档和使用示例。"""
import os
import sys
from pathlib import Path
from datetime import datetime

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from ruamel.yaml import YAML

yaml = YAML()
yaml.preserve_quotes = True

CONTRACTS_DIR = Path(__file__).parent.parent / "contracts"
DOCS_DIR = Path(__file__).parent.parent / "docs"


def load_contract(yaml_path: Path) -> dict:
    """加载单个契约文件。"""
    with open(yaml_path, "r", encoding="utf-8") as f:
        return yaml.load(f)


def get_table_name(contract: dict) -> str:
    """获取表名（兼容 contract 和 table 两种 key）。"""
    return contract.get("contract") or contract.get("table")


def generate_data_dictionary():
    """生成数据字典 Markdown 文档。"""
    print("生成数据字典...")
    
    sections = []
    sections.append("# nooshare-base 数据字典\n")
    sections.append(f"*自动生成于 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*\n")
    sections.append("---\n")
    
    # 目录
    sections.append("## 目录\n")
    sections.append("### public.* 表（标准化产品层）\n")
    
    public_contracts = sorted(CONTRACTS_DIR.glob("public_*.yaml"))

    
    for contract_path in public_contracts:
        contract = load_contract(contract_path)
        table_name = get_table_name(contract)
        desc = contract.get("description", "")
        sections.append(f"- [{table_name}](#{table_name.replace('.', '-')}) — {desc}\n")
    

    
    sections.append("\n---\n")
    
    # public.* 表详细定义
    sections.append("## public.* 表详细定义\n")
    for contract_path in public_contracts:
        contract = load_contract(contract_path)
        sections.append(format_table_section(contract))
    

    
    # 写入文件
    output_path = DOCS_DIR / "DATA_DICTIONARY.md"
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("".join(sections))
    
    print(f"✓ 数据字典已生成: {output_path}")


def format_table_section(contract: dict) -> str:
    """格式化单个表的文档章节。"""
    lines = []
    table_name = get_table_name(contract)
    desc = contract.get("description", "")
    version = contract.get("version", 1)
    
    lines.append(f"### {table_name}\n")
    lines.append(f"**版本**: {version}  \n")
    lines.append(f"**描述**: {desc}\n\n")
    
    # 表类型
    table_type = contract.get("type", "table")
    if table_type == "materialized_view":
        lines.append("**类型**: 物化视图（Materialized View）\n\n")
    
    # 列定义
    columns = contract.get("columns", [])
    if columns:
        lines.append("#### 列定义\n\n")
        lines.append("| 列名 | 类型 | 可空 | 说明 |\n")
        lines.append("|------|------|------|------|\n")
        for col in columns:
            name = col["name"]
            dtype = col["type"]
            nullable = "✓" if col.get("nullable", True) else "✗"
            semantic = col.get("semantic", "").replace("|", "\\|")
            lines.append(f"| `{name}` | {dtype} | {nullable} | {semantic} |\n")
        lines.append("\n")
    
    # 约束
    constraints = contract.get("constraints", [])
    if constraints:
        lines.append("#### 约束\n\n")
        for c in constraints:
            lines.append(f"- {c}\n")
        lines.append("\n")
    
    # 索引
    indexes = contract.get("indexes", [])
    if indexes:
        lines.append("#### 索引\n\n")
        for idx in indexes:
            lines.append(f"- {idx}\n")
        lines.append("\n")
    
    # PIT 语义（如果有）
    pit_semantics = contract.get("pit_semantics")
    if pit_semantics:
        lines.append("#### PIT 查询语义\n\n")
        lines.append("```sql\n")
        lines.append(pit_semantics.strip())
        lines.append("\n```\n\n")
    
    # Payload Schema（如果有）
    payload_schema = contract.get("payload_schema")
    if payload_schema:
        lines.append("#### Payload Schema\n\n")
        lines.append("```json\n")
        for event_type, schema in payload_schema.items():
            lines.append(f"{event_type}: {schema}\n")
        lines.append("```\n\n")
    
    # 来源表
    source_tables = contract.get("source_tables", [])
    if source_tables:
        lines.append("#### 数据来源\n\n")
        for src in source_tables:
            lines.append(f"- `{src}`\n")
        lines.append("\n")
    
    # 消费者
    consumers = contract.get("consumers", [])
    if consumers:
        lines.append("#### 下游消费者\n\n")
        for consumer in consumers:
            lines.append(f"- `{consumer}`\n")
        lines.append("\n")
    
    lines.append("---\n\n")
    return "".join(lines)


def generate_api_docs():
    """生成 CLI API 文档。"""
    print("生成 API 文档...")
    
    lines = []
    lines.append("# nooshare-base CLI API 文档\n")
    lines.append(f"*自动生成于 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*\n")
    lines.append("---\n\n")
    
    lines.append("## 概述\n\n")
    lines.append("nooshare-base 提供统一的 CLI 入口，所有数据操作通过命令行完成。\n\n")
    lines.append("```bash\n")
    lines.append("base --help\n")
    lines.append("```\n\n")
    
    lines.append("---\n\n")
    
    # 核心命令
    lines.append("## 核心命令\n\n")
    
    commands = [
        {
            "name": "init-db",
            "desc": "初始化 public schema 和表结构",
            "usage": "base init-db",
            "details": "创建 public.canonical_symbol、public.corporate_action_event 等基础表，以及 6 个物化 PIT 视图。",
        },
        {
            "name": "build",
            "desc": "运行完整管线（P1-P7）",
            "usage": "base build [--start-date YYYYMMDD]",
            "details": """执行完整的数据管线：
- P1: 构建 canonical_symbol（符号主数据）
- P2: 事件溯源（公司行为事件提取）
- P3: 刷新物化 PIT 视图
- P4: 跨源对账
- P5: 数据新鲜度监控
- P7: 记录 pipeline_run

支持增量重算：`--start-date 20200101` 只重算指定日期之后的数据。""",
        },
        {
            "name": "reconcile",
            "desc": "只运行跨源对账（P4）",
            "usage": "base reconcile",
            "details": "对比 ju.stocks 与 tong.symbol_meta，检查交易日历完整性，检测结果写入 public.reconcile_status。",
        },
        {
            "name": "freshness",
            "desc": "只刷新数据新鲜度看板（P5）",
            "usage": "base freshness",
            "details": "扫描所有 namespace 和 public.* 表，更新 public.data_freshness 表的 max_date、row_count、refreshed_at。",
        },
        {
            "name": "validate",
            "desc": "运行验证门禁（V9-V12 必跑，V1-V8 按数据源执行）",
            "usage": "base validate",
            "details": """验证门禁包括：
- V9: 内部自检（PIT 视图行数 > 0）
- V10: 物化视图完整性
- V11: 幂等性验证（相同输入产出相同）
- V12: canonical_id 映射无歧义""",
        },
        {
            "name": "monitor",
            "desc": "监控数据新鲜度，超过阈值时发送告警",
            "usage": "base monitor [--max-age DAYS] [--alert-type log|email|webhook]",
            "details": "检查 public.data_freshness 表，如果 refreshed_at 超过 max-age 天，触发告警。",
        },
    ]
    
    for cmd in commands:
        lines.append(f"### {cmd['name']}\n\n")
        lines.append(f"**描述**: {cmd['desc']}\n\n")
        lines.append("**用法**:\n")
        lines.append(f"```bash\n{cmd['usage']}\n```\n\n")
        lines.append("**详情**:\n\n")
        lines.append(cmd["details"])
        lines.append("\n\n---\n\n")
    
    # 深度演进命令
    lines.append("## 深度演进命令\n\n")
    
    advanced_commands = [
        {
            "name": "backtest",
            "desc": "前视偏差回测验证（金融数据质量黄金标准）",
            "usage": "base backtest [--sample-size N] [--seed SEED] [--output PATH]",
            "details": """随机抽取 N 个历史日期，对比 PIT 视图查询结果与 namespace 源头数据。

**验收标准**: 前视偏差率 = 0%

**输出**: JSON 格式回测报告，包含每个日期的比对结果。""",
        },
        {
            "name": "contract-test",
            "desc": "契约测试自动化：验证数据是否符合 YAML 契约",
            "usage": "base contract-test [--table TABLE] [--sample-size N] [--output PATH]",
            "details": """从 YAML 契约生成 JSON Schema，自动验证数据是否符合契约（类型/非空/范围）。

**验收标准**: 契约测试覆盖率 = 100%

**示例**:
```bash
# 验证所有 public.* 表
base contract-test

# 只验证单个表
base contract-test --table public.st_status_pit
```""",
        },
        {
            "name": "benchmark",
            "desc": "查询性能基准测试：测量 P50/P95/P99 延迟",
            "usage": "base benchmark [--iterations N] [--output PATH]",
            "details": """定义 10 个典型查询，实测每个查询的 P50/P95/P99 延迟，并生成优化建议报告。

**验收标准**: 单股票1年日线 P95 < 100ms

**输出**:
- `benchmarks/report.json`: 详细基准测试结果
- `benchmarks/OPTIMIZATION_REPORT.md`: 优化建议报告""",
        },
        {
            "name": "lineage",
            "desc": "数据血缘追踪：追溯数据来源",
            "usage": "base lineage {build|trace|stats} [--table TABLE] [--row-id ID]",
            "details": """追踪 public.* 数据的来源，支持正向/反向/全链路追踪。

**操作**:
- `build`: 构建血缘关系（写入 public.data_lineage）
- `trace`: 追溯指定行的来源
- `stats`: 查看血缘统计信息

**验收标准**: 血缘覆盖率 = 100%

**示例**:
```bash
# 构建血缘关系
base lineage build

# 追溯某行数据的来源
base lineage trace --table public.valuation_pit --row-id "sz000001:2020-01-01"

# 查看统计信息
base lineage stats
```""",
        },
        {
            "name": "sla",
            "desc": "数据质量 SLA 监控：检查数据是否按时就绪",
            "usage": "base sla {check|report|list|define} [OPTIONS]",
            "details": """定义每个表的 SLA（如"估值数据每天 9:30 前就绪"），违约时自动告警。

**操作**:
- `check`: 检查当前 SLA 违约情况
- `report`: 生成 SLA 达成率报告
- `list`: 列出所有已定义的 SLA
- `define`: 定义新的 SLA

**验收标准**: SLA 达成率 > 99%

**示例**:
```bash
# 检查 SLA 违约
base sla check

# 生成过去 30 天的达成率报告
base sla report --days 30

# 定义新的 SLA
base sla define --table ju.valuation --time 09:30
```""",
        },
    ]
    
    for cmd in advanced_commands:
        lines.append(f"### {cmd['name']}\n\n")
        lines.append(f"**描述**: {cmd['desc']}\n\n")
        lines.append("**用法**:\n")
        lines.append(f"```bash\n{cmd['usage']}\n```\n\n")
        lines.append("**详情**:\n\n")
        lines.append(cmd["details"])
        lines.append("\n\n---\n\n")
    
    # 写入文件
    output_path = DOCS_DIR / "API_REFERENCE.md"
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("".join(lines))
    
    print(f"✓ API 文档已生成: {output_path}")


def generate_usage_examples():
    """生成使用示例文档。"""
    print("生成使用示例...")
    
    lines = []
    lines.append("# nooshare-base 使用示例\n")
    lines.append(f"*自动生成于 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*\n")
    lines.append("---\n\n")
    
    lines.append("## 快速开始\n\n")
    lines.append("### 1. 初始化数据库\n\n")
    lines.append("```bash\n")
    lines.append("# 创建 public schema 和所有表\n")
    lines.append("base init-db\n")
    lines.append("```\n\n")
    
    lines.append("### 2. 运行完整管线\n\n")
    lines.append("```bash\n")
    lines.append("# 完整管线：P1-P7\n")
    lines.append("base build\n")
    lines.append("```\n\n")
    
    lines.append("### 3. 验证数据质量\n\n")
    lines.append("```bash\n")
    lines.append("# 运行验证门禁\n")
    lines.append("base validate\n")
    lines.append("```\n\n")
    
    lines.append("---\n\n")
    
    lines.append("## 常见查询场景\n\n")
    
    scenarios = [
        {
            "title": "查询某股票的 ST 历史",
            "desc": "查询某只股票在指定日期的 ST 状态（Point-in-Time）",
            "sql": """SELECT symbol, st_type, effective_from, effective_to
FROM public.st_status_pit
WHERE symbol = 'sz000001'
  AND effective_from <= '2020-01-01'
  AND (effective_to IS NULL OR effective_to > '2020-01-01');""",
        },
        {
            "title": "查询全市场某日的估值数据",
            "desc": "查询 2020-01-01 当天所有股票的市值（避免前视偏差）",
            "sql": """SELECT symbol, capitalization, circulating_cap
FROM public.valuation_pit
WHERE effective_from <= '2020-01-01'
  AND effective_to >= '2020-01-01';""",
        },
        {
            "title": "查询某股票的复权因子历史",
            "desc": "查询某只股票的所有分红送股事件",
            "sql": """SELECT effective_date, event_type, payload
FROM public.adjustment_factor_pit
WHERE symbol = 'sz000001'
  AND effective_date <= '2020-12-31'
ORDER BY effective_date;""",
        },
        {
            "title": "查询某股票的财务报表（利润表）",
            "desc": "查询某只股票的历史财务数据（按公告日期，避免前视偏差）",
            "sql": """SELECT report_date, pub_date, revenue, net_profit, eps
FROM public.income_statement_pit
WHERE symbol = 'sz000001'
  AND pub_date <= '2020-12-31'
ORDER BY report_date DESC
LIMIT 10;""",
        },
        {
            "title": "统计当前 ST 股票数量",
            "desc": "查询当前（最新日期）所有 ST 股票",
            "sql": """SELECT COUNT(DISTINCT symbol) AS st_count
FROM public.st_status_pit
WHERE effective_to IS NULL;""",
        },
        {
            "title": "查询某日的全市场交易日线",
            "desc": "查询 2020-01-02 所有股票的日线数据",
            "sql": """SELECT symbol, open, high, low, close, volume, amount
FROM tong.stock_daily
WHERE trade_date = '2020-01-02';""",
        },
        {
            "title": "查询某股票的股本变动历史",
            "desc": "查询某只股票的总股本和流通股本变化",
            "sql": """SELECT effective_date, payload->>'total_shares' AS total_shares,
                      payload->>'circulating_shares' AS circulating_shares
FROM public.corporate_action_event
WHERE canonical_id = '000001'
  AND event_type = 'shares_change'
ORDER BY effective_date;""",
        },
        {
            "title": "检查数据新鲜度",
            "desc": "查看所有表的最新数据日期",
            "sql": """SELECT namespace, table_name, max_date, row_count, refreshed_at
FROM public.data_freshness
ORDER BY namespace, table_name;""",
        },
        {
            "title": "查看管线运行历史",
            "desc": "查看最近的管线运行记录",
            "sql": """SELECT run_id, started_at, completed_at, status, products
FROM public.pipeline_run
ORDER BY started_at DESC
LIMIT 10;""",
        },
        {
            "title": "检查跨源对账状态",
            "desc": "查看最近的对账结果",
            "sql": """SELECT namespace, table_name, check_type, status, checked_at
FROM public.reconcile_status
ORDER BY checked_at DESC;""",
        },
    ]
    
    for i, scenario in enumerate(scenarios, 1):
        lines.append(f"### 场景 {i}: {scenario['title']}\n\n")
        lines.append(f"**描述**: {scenario['desc']}\n\n")
        lines.append("**SQL 查询**:\n\n")
        lines.append("```sql\n")
        lines.append(scenario["sql"])
        lines.append("\n```\n\n")
        lines.append("---\n\n")
    
    # 高级用法
    lines.append("## 高级用法\n\n")
    
    lines.append("### 前视偏差回测\n\n")
    lines.append("验证 PIT 视图是否零前视偏差（金融数据质量黄金标准）。\n\n")
    lines.append("```bash\n")
    lines.append("# 随机抽取 100 个历史日期进行回测\n")
    lines.append("base backtest --sample-size 100 --output backtest_report.json\n")
    lines.append("```\n\n")
    
    lines.append("### 契约测试\n\n")
    lines.append("验证数据是否符合 YAML 契约定义。\n\n")
    lines.append("```bash\n")
    lines.append("# 验证所有 public.* 表\n")
    lines.append("base contract-test --output contract_test_report.json\n")
    lines.append("```\n\n")
    
    lines.append("### 性能基准测试\n\n")
    lines.append("测量典型查询的 P50/P95/P99 延迟。\n\n")
    lines.append("```bash\n")
    lines.append("# 运行基准测试\n")
    lines.append("base benchmark --iterations 100 --output benchmarks/report.json\n")
    lines.append("```\n\n")
    
    lines.append("### 数据血缘追踪\n\n")
    lines.append("追溯 public.* 数据的来源。\n\n")
    lines.append("```bash\n")
    lines.append("# 构建血缘关系\n")
    lines.append("base lineage build\n")
    lines.append("\n")
    lines.append("# 追溯某行数据的来源\n")
    lines.append("base lineage trace --table public.valuation_pit --row-id 'sz000001:2020-01-01'\n")
    lines.append("```\n\n")
    
    lines.append("### SLA 监控\n\n")
    lines.append("定义和检查数据质量 SLA。\n\n")
    lines.append("```bash\n")
    lines.append("# 定义 SLA：估值数据每天 9:30 前就绪\n")
    lines.append("base sla define --table ju.valuation --time 09:30\n")
    lines.append("\n")
    lines.append("# 检查 SLA 违约\n")
    lines.append("base sla check\n")
    lines.append("\n")
    lines.append("# 生成达成率报告\n")
    lines.append("base sla report --days 30\n")
    lines.append("```\n\n")
    
    # 写入文件
    output_path = DOCS_DIR / "USAGE_EXAMPLES.md"
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("".join(lines))
    
    print(f"✓ 使用示例已生成: {output_path}")


def generate_index():
    """生成文档索引。"""
    print("生成文档索引...")
    
    lines = []
    lines.append("# nooshare-base 文档\n")
    lines.append(f"*自动生成于 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*\n")
    lines.append("---\n\n")
    
    lines.append("## 文档列表\n\n")
    lines.append("1. **[数据字典](DATA_DICTIONARY.md)** — 所有表的列定义、约束、索引、数据来源\n")
    lines.append("2. **[API 文档](API_REFERENCE.md)** — CLI 命令参考，包括核心命令和深度演进命令\n")
    lines.append("3. **[使用示例](USAGE_EXAMPLES.md)** — 10 个常见查询场景 + 高级用法\n\n")
    
    lines.append("---\n\n")
    
    lines.append("## 快速链接\n\n")
    lines.append("- [public.canonical_symbol](DATA_DICTIONARY.md#public-canonical_symbol) — 符号主数据\n")
    lines.append("- [public.st_status_pit](DATA_DICTIONARY.md#public-st_status_pit) — ST 状态 PIT 视图\n")
    lines.append("- [public.valuation_pit](DATA_DICTIONARY.md#public-valuation_pit) — 估值数据 PIT 视图\n")
    lines.append("- [CLI build 命令](API_REFERENCE.md#build) — 运行完整管线\n")
    lines.append("- [查询示例](USAGE_EXAMPLES.md#场景-1-查询某股票的-st-历史) — ST 历史查询\n\n")
    
    lines.append("---\n\n")
    
    lines.append("## 核心概念\n\n")
    lines.append("- **PIT (Point-in-Time)**: 时间点查询，避免前视偏差\n")
    lines.append("- **双时态模型**: effective_date（生效日期）+ pub_date（公告日期）+ observed_at（观测时间）\n")
    lines.append("- **Event Sourcing**: 公司行为事件溯源，保留完整变更历史\n")
    lines.append("- **Canonical Symbol**: 统一证券标识，消除跨源符号歧义\n\n")
    
    # 写入文件
    output_path = DOCS_DIR / "README.md"
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("".join(lines))
    
    print(f"✓ 文档索引已生成: {output_path}")


def main():
    """主函数。"""
    print("=" * 60)
    print("nooshare-base 文档生成器")
    print("=" * 60)
    print()
    
    # 确保 docs 目录存在
    DOCS_DIR.mkdir(exist_ok=True)
    
    # 生成文档
    generate_data_dictionary()
    generate_api_docs()
    generate_usage_examples()
    generate_index()
    
    print()
    print("=" * 60)
    print("✓ 所有文档生成完成！")
    print(f"输出目录: {DOCS_DIR}")
    print("=" * 60)


if __name__ == "__main__":
    main()
