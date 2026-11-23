"""查询性能基准测试（Query Performance Benchmarks）。

目标：
  - 单股票1年日线数据查询延迟 < 100ms
  - 全市场估值查询延迟 < 200ms
  - ST 历史查询延迟 < 150ms

测试方法：
  1. 定义 10 个典型查询
  2. 每个查询运行 100 次
  3. 计算 P50/P95/P99 延迟
  4. 用 EXPLAIN ANALYZE 分析执行计划

CLI:
  python benchmarks/query_performance.py
  pytest benchmarks/query_performance.py -v
"""
import logging
import time
from typing import Dict, List, Any
from datetime import datetime, timedelta

import pandas as pd
import typer

from base.db import read_sql, execute_sql

log = logging.getLogger(__name__)

app = typer.Typer(help="查询性能基准测试")


# ─── 典型查询定义 ─────────────────────────────────────────────────────────────

TYPICAL_QUERIES = [
    {
        "name": "Q1: 单股票1年日线数据",
        "description": "查询单只股票过去1年的日线数据（OHLCV）",
        "sql": """
            SELECT date, open, high, low, close, volume
            FROM tong.stock_daily
            WHERE symbol = 'sh600000'
              AND date >= CURRENT_DATE - INTERVAL '1 year'
            ORDER BY date
        """,
        "target_ms": 100,
    },
    {
        "name": "Q2: 全市场最新估值",
        "description": "查询全市场所有股票的最新估值数据（市值、流通市值）",
        "sql": """
            SELECT canonical_id, symbol, capitalization, circulating_cap
            FROM public.valuation_pit
            WHERE effective_to >= CURRENT_DATE - INTERVAL '7 days'
        """,
        "target_ms": 200,
    },
    {
        "name": "Q3: 单股票ST历史",
        "description": "查询单只股票的ST状态历史（所有ST区间）",
        "sql": """
            SELECT effective_from, effective_to, st_type
            FROM public.st_status_pit
            WHERE symbol = 'sh600000'
            ORDER BY effective_from
        """,
        "target_ms": 150,
    },
    {
        "name": "Q4: 某日全市场ST状态",
        "description": "查询某个历史日期的全市场ST状态快照",
        "sql": """
            SELECT canonical_id, symbol, st_type
            FROM public.st_status_pit
            WHERE effective_from <= '2023-12-31'
              AND (effective_to IS NULL OR effective_to >= '2023-12-31')
        """,
        "target_ms": 200,
    },
    {
        "name": "Q5: 单股票财务报表",
        "description": "查询单只股票的所有财务报表数据（利润表）",
        "sql": """
            SELECT report_date, pub_date, revenue, net_profit, eps
            FROM public.income_statement_pit
            WHERE symbol = 'sh600000'
            ORDER BY report_date DESC
        """,
        "target_ms": 150,
    },
    {
        "name": "Q6: 全市场复权因子",
        "description": "查询全市场所有股票的复权因子（分红、送股）",
        "sql": """
            SELECT canonical_id, symbol, effective_date, event_type, payload
            FROM public.adjustment_factor_pit
            WHERE effective_date >= CURRENT_DATE - INTERVAL '1 year'
        """,
        "target_ms": 300,
    },
    {
        "name": "Q7: 单股票日线+复权",
        "description": "查询单只股票日线数据并关联复权因子",
        "sql": """
            SELECT d.date, d.close, d.volume, a.event_type, a.payload
            FROM tong.stock_daily d
            LEFT JOIN public.adjustment_factor_pit a
              ON a.symbol = d.symbol AND a.effective_date = d.date
            WHERE d.symbol = 'sh600000'
              AND d.date >= CURRENT_DATE - INTERVAL '1 year'
            ORDER BY d.date
        """,
        "target_ms": 200,
    },
    {
        "name": "Q8: 全市场Canonical Symbol",
        "description": "查询全市场所有股票的Canonical Symbol映射",
        "sql": """
            SELECT canonical_id, symbol, exchange, code, asset_class, list_date
            FROM public.canonical_symbol
            ORDER BY symbol
        """,
        "target_ms": 100,
    },
    {
        "name": "Q9: 数据新鲜度监控",
        "description": "查询所有数据源的新鲜度状态",
        "sql": """
            SELECT namespace, table_name, max_date, row_count, refreshed_at
            FROM public.data_freshness
            ORDER BY refreshed_at DESC
        """,
        "target_ms": 50,
    },
    {
        "name": "Q10: 复杂分析查询",
        "description": "复杂分析：查询过去1年市值增长最快的前100只股票",
        "sql": """
            WITH latest AS (
                SELECT canonical_id, symbol, capitalization
                FROM public.valuation_pit
                WHERE effective_to >= CURRENT_DATE - INTERVAL '7 days'
            ),
            year_ago AS (
                SELECT canonical_id, capitalization
                FROM public.valuation_pit
                WHERE effective_from <= CURRENT_DATE - INTERVAL '1 year'
                  AND effective_to >= CURRENT_DATE - INTERVAL '1 year' - INTERVAL '7 days'
            )
            SELECT l.symbol, l.capitalization AS latest_cap, y.capitalization AS year_ago_cap,
                   (l.capitalization - y.capitalization) / NULLIF(y.capitalization, 0) AS growth_rate
            FROM latest l
            JOIN year_ago y ON l.canonical_id = y.canonical_id
            WHERE y.capitalization > 0
            ORDER BY growth_rate DESC
            LIMIT 100
        """,
        "target_ms": 500,
    },
]


def run_query_with_timing(sql: str, iterations: int = 100) -> Dict[str, float]:
    """运行查询并测量延迟。

    Args:
        sql: SQL 查询语句
        iterations: 运行次数

    Returns:
        {
            "p50_ms": float,
            "p95_ms": float,
            "p99_ms": float,
            "mean_ms": float,
            "min_ms": float,
            "max_ms": float,
        }
    """
    latencies = []

    for _ in range(iterations):
        start = time.perf_counter()
        try:
            df = read_sql(sql)
        except Exception as e:
            log.warning(f"查询失败: {e}")
            continue
        end = time.perf_counter()
        latencies.append((end - start) * 1000)  # 转换为毫秒

    if not latencies:
        return {
            "p50_ms": 0,
            "p95_ms": 0,
            "p99_ms": 0,
            "mean_ms": 0,
            "min_ms": 0,
            "max_ms": 0,
        }

    latencies.sort()
    n = len(latencies)

    return {
        "p50_ms": latencies[int(n * 0.50)],
        "p95_ms": latencies[int(n * 0.95)],
        "p99_ms": latencies[int(n * 0.99)],
        "mean_ms": sum(latencies) / n,
        "min_ms": min(latencies),
        "max_ms": max(latencies),
    }


def analyze_query_plan(sql: str) -> Dict[str, Any]:
    """用 EXPLAIN ANALYZE 分析查询执行计划。

    Args:
        sql: SQL 查询语句

    Returns:
        {
            "plan": str,
            "execution_time_ms": float,
        }
    """
    explain_sql = f"EXPLAIN ANALYZE {sql}"
    try:
        result = read_sql(explain_sql)
        plan_lines = result.iloc[:, 0].tolist()
        plan_text = "\n".join(plan_lines)

        # 提取执行时间
        execution_time = 0
        for line in plan_lines:
            if "Execution Time:" in line:
                parts = line.split(":")
                if len(parts) > 1:
                    execution_time = float(parts[1].strip().split()[0])
                break

        return {
            "plan": plan_text,
            "execution_time_ms": execution_time,
        }
    except Exception as e:
        return {
            "plan": f"分析失败: {e}",
            "execution_time_ms": 0,
        }


def benchmark_all_queries(iterations: int = 100) -> Dict[str, Any]:
    """基准测试所有典型查询。

    Args:
        iterations: 每个查询运行次数

    Returns:
        {
            "timestamp": str,
            "iterations": int,
            "queries": [...],
            "summary": {
                "total_queries": int,
                "passed_queries": int,
                "failed_queries": int,
            }
        }
    """
    log.info("=" * 60)
    log.info(f"查询性能基准测试: iterations={iterations}")
    log.info("=" * 60)

    results = []

    for query_def in TYPICAL_QUERIES:
        name = query_def["name"]
        sql = query_def["sql"]
        target_ms = query_def["target_ms"]

        log.info(f"测试: {name}")

        # 测量延迟
        timing = run_query_with_timing(sql, iterations)

        # 分析执行计划
        plan = analyze_query_plan(sql)

        # 判断是否通过
        passed = timing["p95_ms"] <= target_ms

        result = {
            "name": name,
            "description": query_def["description"],
            "target_ms": target_ms,
            "timing": timing,
            "plan": plan["plan"],
            "passed": passed,
        }

        results.append(result)

        status = "✓" if passed else "✗"
        log.info(
            f"  {status} P95={timing['p95_ms']:.1f}ms (目标 <{target_ms}ms) "
            f"mean={timing['mean_ms']:.1f}ms"
        )

    # 汇总
    passed_count = sum(1 for r in results if r["passed"])
    failed_count = len(results) - passed_count

    summary = {
        "timestamp": datetime.now().isoformat(),
        "iterations": iterations,
        "queries": results,
        "summary": {
            "total_queries": len(results),
            "passed_queries": passed_count,
            "failed_queries": failed_count,
            "pass_rate": passed_count / len(results) if results else 0,
        },
    }

    log.info("=" * 60)
    log.info(f"基准测试汇总: {passed_count}/{len(results)} 通过")
    log.info("=" * 60)

    return summary


def generate_optimization_report(benchmark_result: Dict[str, Any]) -> str:
    """生成优化建议报告。

    Args:
        benchmark_result: 基准测试结果

    Returns:
        Markdown 格式的优化建议报告
    """
    lines = [
        "# 查询性能优化建议报告",
        "",
        f"生成时间: {benchmark_result['timestamp']}",
        f"测试次数: {benchmark_result['iterations']}",
        "",
        "## 汇总",
        "",
        f"- 总查询数: {benchmark_result['summary']['total_queries']}",
        f"- 通过: {benchmark_result['summary']['passed_queries']}",
        f"- 失败: {benchmark_result['summary']['failed_queries']}",
        f"- 通过率: {benchmark_result['summary']['pass_rate']:.1%}",
        "",
        "## 失败查询分析",
        "",
    ]

    failed_queries = [q for q in benchmark_result["queries"] if not q["passed"]]

    if not failed_queries:
        lines.append("✓ 所有查询均通过性能目标，无需优化。")
    else:
        for q in failed_queries:
            lines.append(f"### {q['name']}")
            lines.append("")
            lines.append(f"- 目标: <{q['target_ms']}ms")
            lines.append(f"- 实际 P95: {q['timing']['p95_ms']:.1f}ms")
            lines.append(f"- 实际 Mean: {q['timing']['mean_ms']:.1f}ms")
            lines.append("")
            lines.append("**执行计划:**")
            lines.append("```")
            lines.append(q["plan"])
            lines.append("```")
            lines.append("")
            lines.append("**优化建议:**")
            lines.append("")

            # 根据延迟给出建议
            if q["timing"]["p95_ms"] > q["target_ms"] * 2:
                lines.append("1. **添加索引**: 检查 WHERE 和 JOIN 条件的列是否有索引")
                lines.append("2. **分区表**: 如果表数据量很大（>1000万行），考虑按时间分区")
                lines.append("3. **物化视图**: 如果查询复杂且频繁，考虑创建物化视图")
            else:
                lines.append("1. **微调索引**: 检查现有索引是否覆盖查询条件")
                lines.append("2. **查询重写**: 检查是否有更高效的写法")

            lines.append("")

    lines.extend([
        "## 通过的查询",
        "",
    ])

    passed_queries = [q for q in benchmark_result["queries"] if q["passed"]]
    for q in passed_queries:
        lines.append(f"- ✓ {q['name']}: P95={q['timing']['p95_ms']:.1f}ms (目标 <{q['target_ms']}ms)")

    return "\n".join(lines)


@app.command()
def run(
    iterations: int = typer.Option(100, help="每个查询运行次数"),
    output: str = typer.Option("benchmarks/report.json", help="输出报告路径（JSON）"),
    report_path: str = typer.Option("benchmarks/OPTIMIZATION_REPORT.md", help="优化建议报告路径"),
):
    """运行查询性能基准测试。"""
    import json

    result = benchmark_all_queries(iterations)

    # 保存 JSON 报告
    with open(output, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    typer.echo(f"基准测试报告已保存: {output}")

    # 生成优化建议报告
    report = generate_optimization_report(result)
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)
    typer.echo(f"优化建议报告已保存: {report_path}")

    # 输出汇总
    typer.echo(json.dumps(result["summary"], ensure_ascii=False, indent=2))

    if result["summary"]["failed_queries"] > 0:
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
