"""CLI 入口。CLI 是唯一的数据处理入口（沿用矩阵 typer 惯例）。"""
import typer

from base import __version__

app = typer.Typer(
    help="nooshare-base：Point-in-Time 正确的基础数据产品层",
    no_args_is_help=True,
)


def _version_callback(value: bool):
    if value:
        typer.echo(f"nooshare-base {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False, "--version", "-v", callback=_version_callback, is_eager=True
    )
):
    """nooshare-base CLI。"""


@app.command()
def init_db():
    """Initialize public schema and tables."""
    from base.db import init_schema

    init_schema()
    typer.echo("Done!")


@app.command()
def build(
    start_date: str = typer.Option(None, help="起始日期 YYYYMMDD（增量重算用）"),
):
    """完整管线：P1 符号主数据 → P2 事件溯源 → P3 PIT → P4 对账 → P5 新鲜度，记录 pipeline_run。"""
    from base.pipeline.runner import run_pipeline

    run_pipeline(start_date=start_date)
    typer.echo("✓ build 完成")


@app.command()
def reconcile():
    """只跑跨源对账（P4），差异写 public.reconcile_status。"""
    from base.reconcile.runner import run_reconciliation

    run_reconciliation()
    typer.echo("✓ reconcile 完成")


@app.command()
def freshness():
    """只刷新数据新鲜度看板（P5）。"""
    from base.freshness.collect import collect_freshness

    collect_freshness()
    typer.echo("✓ freshness 完成")


@app.command()
def hfq(
    symbols: str = typer.Option(None, help="逗号分隔 symbol 列表（如 sz000001,sh600519；缺省全市场）"),
):
    """计算并写入 public.hfq_daily 后复权价格序列（源: tong.gbbq category=1 + tong.stock_daily）。"""
    import json
    from base.adjust.hfq import run

    symbol_list = [s.strip() for s in symbols.split(",")] if symbols else None
    stats = run(symbols=symbol_list)
    typer.echo(json.dumps(stats, ensure_ascii=False, indent=2))


@app.command()
def validate():
    """验证门禁：内部自检（V9–V12）必跑；外部对账（V1–V8）按已接入数据源执行。"""
    from base.validate.suite import run_validation

    ok = run_validation()
    if ok:
        typer.echo("✓ validate 全部通过")
    else:
        typer.echo("✗ validate 存在未通过项", err=True)
        raise typer.Exit(code=1)


@app.command()
def monitor(
    max_age: int = typer.Option(7, help="数据陈旧阈值（天）"),
    alert_type: str = typer.Option("log", help="告警类型: log/email/webhook"),
):
    """监控数据新鲜度，超过阈值时发送告警。"""
    from base.monitor.alert import run_monitor

    ok = run_monitor(max_age_days=max_age, alert_type=alert_type)
    if ok:
        typer.echo("✓ monitor 检查通过")
    else:
        typer.echo("✗ monitor 发现陈旧数据", err=True)
        raise typer.Exit(code=1)


# ─── 深度演进功能 ─────────────────────────────────────────────────────────────

@app.command()
def backtest(
    sample_size: int = typer.Option(100, help="随机抽取的日期数量"),
    seed: int = typer.Option(42, help="随机种子"),
    output: str = typer.Option(None, help="输出报告路径（JSON）"),
):
    """前视偏差回测验证（金融数据质量黄金标准）。"""
    from tests.pit_backtest import run_backtest
    import json

    result = run_backtest(sample_size, seed)

    if output:
        with open(output, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2, default=str)
        typer.echo(f"回测报告已保存: {output}")

    typer.echo(json.dumps(result, ensure_ascii=False, indent=2, default=str))

    if not result["overall_passed"]:
        raise typer.Exit(code=1)


@app.command()
def contract_test(
    table: str = typer.Option(None, help="指定表名（如 public.st_status_pit）"),
    sample_size: int = typer.Option(1000, help="抽样验证行数"),
    output: str = typer.Option(None, help="输出报告路径（JSON）"),
):
    """契约测试自动化：验证数据是否符合 YAML 契约。"""
    from tests.contract_tests import validate_table_against_contract, validate_all_public_tables
    import json

    if table:
        result = validate_table_against_contract(table, sample_size=sample_size)
        results = {"tables_tested": 1, "results": [result], "overall_passed": result["passed"]}
    else:
        results = validate_all_public_tables(sample_size)

    if output:
        with open(output, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2, default=str)
        typer.echo(f"验证报告已保存: {output}")

    typer.echo(json.dumps(results, ensure_ascii=False, indent=2, default=str))

    if not results["overall_passed"]:
        raise typer.Exit(code=1)


@app.command()
def benchmark(
    iterations: int = typer.Option(100, help="每个查询运行次数"),
    output: str = typer.Option("benchmarks/report.json", help="输出报告路径"),
):
    """查询性能基准测试：测量 P50/P95/P99 延迟。"""
    from benchmarks.query_performance import benchmark_all_queries, generate_optimization_report
    import json

    result = benchmark_all_queries(iterations)

    with open(output, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    typer.echo(f"基准测试报告已保存: {output}")

    report = generate_optimization_report(result)
    with open("benchmarks/OPTIMIZATION_REPORT.md", "w", encoding="utf-8") as f:
        f.write(report)
    typer.echo("优化建议报告已保存: benchmarks/OPTIMIZATION_REPORT.md")

    typer.echo(json.dumps(result["summary"], ensure_ascii=False, indent=2))

    if result["summary"]["failed_queries"] > 0:
        raise typer.Exit(code=1)


@app.command()
def lineage(
    action: str = typer.Argument(..., help="操作: build/trace/stats"),
    table: str = typer.Option(None, help="表名"),
    row_id: str = typer.Option(None, help="行标识"),
):
    """数据血缘追踪：追溯数据来源。"""
    from base.lineage.tracker import LineageTracker, build_valuation_lineage, build_st_status_lineage
    import json

    tracker = LineageTracker()

    if action == "build":
        build_valuation_lineage()
        build_st_status_lineage()
        typer.echo("✓ 血缘关系构建完成")

    elif action == "trace":
        if not table or not row_id:
            typer.echo("请提供 --table 和 --row-id", err=True)
            raise typer.Exit(code=1)
        sources = tracker.trace_forward(table, row_id)
        typer.echo(json.dumps(sources, ensure_ascii=False, indent=2, default=str))

    elif action == "stats":
        stats = tracker.get_lineage_stats()
        typer.echo(json.dumps(stats, ensure_ascii=False, indent=2))

    else:
        typer.echo(f"未知操作: {action}", err=True)
        raise typer.Exit(code=1)


@app.command()
def sla(
    action: str = typer.Argument(..., help="操作: check/report/list/define"),
    table: str = typer.Option(None, help="表名（如 ju.valuation）"),
    time: str = typer.Option(None, help="期望时间（如 09:30）"),
    days: int = typer.Option(7, help="统计天数"),
):
    """数据质量 SLA 监控：检查数据是否按时就绪。"""
    from base.monitor.sla import SLAManager

    manager = SLAManager()

    if action == "check":
        slas = manager.get_all_slas()
        if not slas:
            manager.load_default_slas()
        result = manager.check_all_sla_violations()
        typer.echo(f"检查日期: {result['check_date']}")
        typer.echo(f"违约数: {result['violations']}/{result['total_slas']}")
        if result["violations"] > 0:
            raise typer.Exit(code=1)

    elif action == "report":
        report = manager.get_sla_compliance_report(days)
        typer.echo(f"达成率: {report['compliance_rate']:.1%}")
        typer.echo(f"违约数: {report['total_violations']}/{report['total_checks']}")

    elif action == "list":
        slas = manager.get_all_slas()
        for sla in slas:
            typer.echo(f"{sla['namespace']}.{sla['table_name']}: {sla['expected_time']}")

    elif action == "define":
        if not table or not time:
            typer.echo("请提供 --table 和 --time", err=True)
            raise typer.Exit(code=1)
        parts = table.split(".")
        if len(parts) != 2:
            typer.echo("表名格式错误", err=True)
            raise typer.Exit(code=1)
        manager.define_sla(parts[0], parts[1], "daily_refresh", time)
        typer.echo(f"✓ 已定义 SLA: {table}")

    else:
        typer.echo(f"未知操作: {action}", err=True)
        raise typer.Exit(code=1)


# ─── 可视化监控 ───────────────────────────────────────────────────────────────

@app.command()
def dashboard(
    host: str = typer.Option("127.0.0.1", help="监听地址"),
    port: int = typer.Option(5000, help="监听端口"),
    debug: bool = typer.Option(False, help="调试模式"),
):
    """启动数据质量可视化监控看板。"""
    from base.dashboard.app import run_dashboard

    typer.echo(f"🚀 启动 Dashboard: http://{host}:{port}")
    run_dashboard(host=host, port=port, debug=debug)


if __name__ == "__main__":
    app()
