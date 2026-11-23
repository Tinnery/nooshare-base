"""数据质量 SLA 监控（Service Level Agreement）。

目标：
  数据延迟 > 1小时自动告警，SLA 达成率 > 99%。

核心能力：
  1. 定义每个表的 SLA（如 ju.valuation 每天 9:30 前就绪）
  2. 监控 data_freshness.refreshed_at，违约时触发告警
  3. 统计 SLA 达成率

SLA 定义格式：
  {
      "namespace": "ju",
      "table_name": "valuation",
      "sla_type": "daily_refresh",  # 每日刷新
      "expected_time": "09:30",      # 期望时间
      "timezone": "Asia/Shanghai",
      "grace_period_minutes": 60,    # 宽限期（分钟）
      "priority": "high",            # 优先级
  }

CLI:
  python -m base.monitor.sla --check
  python -m base.monitor.sla --report --days 7
  python -m base.monitor.sla --define-sla ju.valuation --time 09:30
"""
import json
import logging
from datetime import datetime, time, timedelta
from typing import Dict, Any, List, Optional

import pandas as pd
import pytz
import typer

from base.db import read_sql, execute_sql, get_connection

log = logging.getLogger(__name__)

app = typer.Typer(help="数据质量 SLA 监控")


# ─── 默认 SLA 定义 ───────────────────────────────────────────────────────────

DEFAULT_SLAS = [
    {
        "namespace": "ju",
        "table_name": "valuation",
        "sla_type": "daily_refresh",
        "expected_time": "09:30",
        "timezone": "Asia/Shanghai",
        "grace_period_minutes": 60,
        "priority": "high",
        "description": "估值数据每个交易日 9:30 前就绪",
    },
    {
        "namespace": "ju",
        "table_name": "trading_day",
        "sla_type": "daily_refresh",
        "expected_time": "08:00",
        "timezone": "Asia/Shanghai",
        "grace_period_minutes": 30,
        "priority": "critical",
        "description": "交易日历每天 8:00 前就绪",
    },
    {
        "namespace": "ju",
        "table_name": "st_period",
        "sla_type": "daily_refresh",
        "expected_time": "10:00",
        "timezone": "Asia/Shanghai",
        "grace_period_minutes": 120,
        "priority": "medium",
        "description": "ST 状态数据每天 10:00 前就绪",
    },
    {
        "namespace": "tong",
        "table_name": "stock_daily",
        "sla_type": "daily_refresh",
        "expected_time": "16:00",
        "timezone": "Asia/Shanghai",
        "grace_period_minutes": 60,
        "priority": "high",
        "description": "股票日线数据每天 16:00 前就绪（收盘后）",
    },
    {
        "namespace": "tong",
        "table_name": "symbol_meta",
        "sla_type": "weekly_refresh",
        "expected_time": "Monday 09:00",
        "timezone": "Asia/Shanghai",
        "grace_period_minutes": 1440,  # 24 小时
        "priority": "low",
        "description": "股票元数据每周一 9:00 前就绪",
    },
    {
        "namespace": "finance",
        "table_name": "income_statement",
        "sla_type": "quarterly_refresh",
        "expected_time": "T+30 10:00",  # 季度结束后 30 天
        "timezone": "Asia/Shanghai",
        "grace_period_minutes": 10080,  # 7 天
        "priority": "medium",
        "description": "利润表每季度结束后 30 天就绪",
    },
    {
        "namespace": "finance",
        "table_name": "balance_sheet",
        "sla_type": "quarterly_refresh",
        "expected_time": "T+30 10:00",
        "timezone": "Asia/Shanghai",
        "grace_period_minutes": 10080,
        "priority": "medium",
        "description": "资产负债表每季度结束后 30 天就绪",
    },
    {
        "namespace": "finance",
        "table_name": "cash_flow",
        "sla_type": "quarterly_refresh",
        "expected_time": "T+30 10:00",
        "timezone": "Asia/Shanghai",
        "grace_period_minutes": 10080,
        "priority": "medium",
        "description": "现金流量表每季度结束后 30 天就绪",
    },
]


class SLAManager:
    """SLA 管理器。

    负责：
      1. 初始化 public.data_sla 表
      2. 加载 SLA 定义
      3. 检查 SLA 违约
      4. 统计 SLA 达成率
    """

    def __init__(self):
        self._init_sla_table()
        self._init_sla_violation_log()

    def _init_sla_table(self):
        """初始化 public.data_sla 表。"""
        ddl = """
        CREATE TABLE IF NOT EXISTS public.data_sla (
            sla_id SERIAL PRIMARY KEY,
            namespace TEXT NOT NULL,
            table_name TEXT NOT NULL,
            sla_type TEXT NOT NULL,
            expected_time TEXT NOT NULL,
            timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
            grace_period_minutes INTEGER NOT NULL DEFAULT 60,
            priority TEXT NOT NULL DEFAULT 'medium',
            description TEXT,
            is_active BOOLEAN NOT NULL DEFAULT true,
            created_at TIMESTAMPTZ DEFAULT now(),
            updated_at TIMESTAMPTZ DEFAULT now(),
            UNIQUE (namespace, table_name)
        );
        """
        execute_sql(ddl)
        log.info("已初始化 public.data_sla 表")

    def _init_sla_violation_log(self):
        """初始化 public.sla_violation_log 表。"""
        ddl = """
        CREATE TABLE IF NOT EXISTS public.sla_violation_log (
            violation_id BIGSERIAL PRIMARY KEY,
            sla_id INTEGER NOT NULL REFERENCES public.data_sla(sla_id),
            check_date DATE NOT NULL,
            expected_refresh_time TIMESTAMPTZ,
            actual_refresh_time TIMESTAMPTZ,
            delay_minutes INTEGER,
            is_violation BOOLEAN NOT NULL,
            created_at TIMESTAMPTZ DEFAULT now(),
            UNIQUE (sla_id, check_date)
        );

        CREATE INDEX IF NOT EXISTS idx_violation_log_date
            ON public.sla_violation_log(check_date DESC);
        """
        execute_sql(ddl)
        log.info("已初始化 public.sla_violation_log 表")

    def load_default_slas(self):
        """加载默认 SLA 定义到数据库。"""
        for sla_def in DEFAULT_SLAS:
            self.define_sla(
                namespace=sla_def["namespace"],
                table_name=sla_def["table_name"],
                sla_type=sla_def["sla_type"],
                expected_time=sla_def["expected_time"],
                timezone=sla_def.get("timezone", "Asia/Shanghai"),
                grace_period_minutes=sla_def.get("grace_period_minutes", 60),
                priority=sla_def.get("priority", "medium"),
                description=sla_def.get("description"),
            )
        log.info(f"已加载 {len(DEFAULT_SLAS)} 个默认 SLA 定义")

    def define_sla(
        self,
        namespace: str,
        table_name: str,
        sla_type: str,
        expected_time: str,
        timezone: str = "Asia/Shanghai",
        grace_period_minutes: int = 60,
        priority: str = "medium",
        description: Optional[str] = None,
    ):
        """定义或更新 SLA。

        Args:
            namespace: 命名空间（如 ju, tong, finance）
            table_name: 表名
            sla_type: SLA 类型（daily_refresh, weekly_refresh, quarterly_refresh）
            expected_time: 期望时间（如 "09:30", "Monday 09:00", "T+30 10:00"）
            timezone: 时区
            grace_period_minutes: 宽限期（分钟）
            priority: 优先级（critical, high, medium, low）
            description: 描述
        """
        sql = """
        INSERT INTO public.data_sla
            (namespace, table_name, sla_type, expected_time, timezone,
             grace_period_minutes, priority, description)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (namespace, table_name)
        DO UPDATE SET
            sla_type = EXCLUDED.sla_type,
            expected_time = EXCLUDED.expected_time,
            timezone = EXCLUDED.timezone,
            grace_period_minutes = EXCLUDED.grace_period_minutes,
            priority = EXCLUDED.priority,
            description = EXCLUDED.description,
            updated_at = now()
        """
        params = (
            namespace,
            table_name,
            sla_type,
            expected_time,
            timezone,
            grace_period_minutes,
            priority,
            description,
        )
        execute_sql(sql, params)
        log.info(f"已定义 SLA: {namespace}.{table_name}")

    def get_all_slas(self) -> List[Dict[str, Any]]:
        """获取所有 SLA 定义。"""
        df = read_sql("SELECT * FROM public.data_sla WHERE is_active = true")
        return df.to_dict("records") if not df.empty else []

    def check_sla_violation(
        self,
        sla: Dict[str, Any],
        check_date: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """检查单个 SLA 是否违约。

        Args:
            sla: SLA 定义
            check_date: 检查日期（默认今天）

        Returns:
            {
                "sla_id": int,
                "namespace": str,
                "table_name": str,
                "check_date": date,
                "expected_refresh_time": datetime,
                "actual_refresh_time": datetime,
                "delay_minutes": int,
                "is_violation": bool,
            }
        """
        if check_date is None:
            check_date = datetime.now()

        namespace = sla["namespace"]
        table_name = sla["table_name"]
        expected_time_str = sla["expected_time"]
        timezone_str = sla.get("timezone", "Asia/Shanghai")
        grace_period = sla.get("grace_period_minutes", 60)

        tz = pytz.timezone(timezone_str)
        check_date_tz = tz.localize(check_date.replace(hour=0, minute=0, second=0, microsecond=0))

        # 解析期望时间
        expected_refresh_time = self._parse_expected_time(
            expected_time_str, check_date_tz, sla["sla_type"]
        )

        # 查询实际刷新时间
        freshness_df = read_sql("""
            SELECT refreshed_at
            FROM public.data_freshness
            WHERE namespace = %s AND table_name = %s
        """, params=(namespace, table_name))

        if freshness_df.empty or freshness_df.iloc[0]["refreshed_at"] is None:
            # 没有刷新记录，视为违约
            actual_refresh_time = None
            delay_minutes = None
            is_violation = True
        else:
            actual_refresh_time = freshness_df.iloc[0]["refreshed_at"]
            if actual_refresh_time.tzinfo is None:
                actual_refresh_time = tz.localize(actual_refresh_time)

            # 计算延迟
            delay = actual_refresh_time - expected_refresh_time
            delay_minutes = int(delay.total_seconds() / 60)

            # 判断是否违约（超过宽限期）
            is_violation = delay_minutes > grace_period

        result = {
            "sla_id": sla["sla_id"],
            "namespace": namespace,
            "table_name": table_name,
            "check_date": check_date_tz.date(),
            "expected_refresh_time": expected_refresh_time,
            "actual_refresh_time": actual_refresh_time,
            "delay_minutes": delay_minutes,
            "is_violation": is_violation,
        }

        # 记录到 violation_log
        self._log_violation(result)

        return result

    def _parse_expected_time(
        self,
        expected_time_str: str,
        check_date: datetime,
        sla_type: str,
    ) -> datetime:
        """解析期望时间字符串为 datetime。

        Args:
            expected_time_str: 期望时间（如 "09:30", "Monday 09:00"）
            check_date: 检查日期
            sla_type: SLA 类型

        Returns:
            期望的刷新时间
        """
        if sla_type == "daily_refresh":
            # 解析 "09:30"
            parts = expected_time_str.split(":")
            hour, minute = int(parts[0]), int(parts[1])
            return check_date.replace(hour=hour, minute=minute, second=0, microsecond=0)

        elif sla_type == "weekly_refresh":
            # 解析 "Monday 09:00"
            parts = expected_time_str.split()
            day_of_week = parts[0]  # Monday
            time_parts = parts[1].split(":")
            hour, minute = int(time_parts[0]), int(time_parts[1])

            # 找到最近的 Monday
            weekday_map = {
                "Monday": 0, "Tuesday": 1, "Wednesday": 2, "Thursday": 3,
                "Friday": 4, "Saturday": 5, "Sunday": 6,
            }
            target_weekday = weekday_map.get(day_of_week, 0)
            days_ahead = target_weekday - check_date.weekday()
            if days_ahead < 0:  # 目标日期已过，等到下周
                days_ahead += 7
            next_weekday = check_date + timedelta(days=days_ahead)
            return next_weekday.replace(hour=hour, minute=minute, second=0, microsecond=0)

        elif sla_type == "quarterly_refresh":
            # 解析 "T+30 10:00"（季度结束后 30 天）
            # 简化处理：假设每季度末月（3/6/9/12）的 30 号
            parts = expected_time_str.split()
            day_offset = int(parts[0].replace("T+", "").replace("T", ""))
            time_parts = parts[1].split(":")
            hour, minute = int(time_parts[0]), int(time_parts[1])

            # 找到最近的季度末
            quarter_end_months = [3, 6, 9, 12]
            current_month = check_date.month
            current_year = check_date.year

            # 找到当前季度的结束月份
            quarter_end_month = next(m for m in quarter_end_months if m >= current_month)
            quarter_end_date = check_date.replace(
                year=current_year,
                month=quarter_end_month,
                day=30,  # 假设季度末为 30 号
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            )

            # 加上偏移天数
            expected_date = quarter_end_date + timedelta(days=day_offset)
            return expected_date.replace(hour=hour, minute=minute)

        else:
            # 默认：当天 00:00
            return check_date.replace(hour=0, minute=0, second=0, microsecond=0)

    def _log_violation(self, violation: Dict[str, Any]):
        """记录违约日志。"""
        sql = """
        INSERT INTO public.sla_violation_log
            (sla_id, check_date, expected_refresh_time, actual_refresh_time,
             delay_minutes, is_violation)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (sla_id, check_date)
        DO UPDATE SET
            actual_refresh_time = EXCLUDED.actual_refresh_time,
            delay_minutes = EXCLUDED.delay_minutes,
            is_violation = EXCLUDED.is_violation
        """
        params = (
            violation["sla_id"],
            violation["check_date"],
            violation["expected_refresh_time"],
            violation["actual_refresh_time"],
            violation["delay_minutes"],
            violation["is_violation"],
        )
        execute_sql(sql, params)

    def check_all_sla_violations(
        self,
        check_date: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """检查所有 SLA 是否违约。

        Args:
            check_date: 检查日期（默认今天）

        Returns:
            {
                "check_date": date,
                "total_slas": int,
                "violations": int,
                "violation_rate": float,
                "details": [...],
            }
        """
        slas = self.get_all_slas()
        details = []

        for sla in slas:
            result = self.check_sla_violation(sla, check_date)
            details.append(result)

        violations = sum(1 for d in details if d["is_violation"])
        total = len(details)

        return {
            "check_date": check_date.date() if check_date else datetime.now().date(),
            "total_slas": total,
            "violations": violations,
            "violation_rate": violations / total if total > 0 else 0,
            "details": details,
        }

    def get_sla_compliance_report(
        self,
        days: int = 7,
    ) -> Dict[str, Any]:
        """生成 SLA 达成率报告。

        Args:
            days: 统计天数

        Returns:
            {
                "period_days": int,
                "total_checks": int,
                "total_violations": int,
                "compliance_rate": float,
                "by_sla": [...],
            }
        """
        start_date = datetime.now().date() - timedelta(days=days)

        # 查询违约日志
        df = read_sql("""
            SELECT
                s.namespace,
                s.table_name,
                s.priority,
                COUNT(*) as total_checks,
                SUM(CASE WHEN v.is_violation THEN 1 ELSE 0 END) as violations
            FROM public.sla_violation_log v
            JOIN public.data_sla s ON s.sla_id = v.sla_id
            WHERE v.check_date >= %s
            GROUP BY s.namespace, s.table_name, s.priority
            ORDER BY violations DESC
        """, params=(start_date,))

        if df.empty:
            return {
                "period_days": days,
                "total_checks": 0,
                "total_violations": 0,
                "compliance_rate": 1.0,
                "by_sla": [],
            }

        total_checks = df["total_checks"].sum()
        total_violations = df["violations"].sum()
        compliance_rate = 1 - (total_violations / total_checks) if total_checks > 0 else 1.0

        return {
            "period_days": days,
            "total_checks": int(total_checks),
            "total_violations": int(total_violations),
            "compliance_rate": compliance_rate,
            "by_sla": df.to_dict("records"),
        }


# ─── CLI ─────────────────────────────────────────────────────────────────────

@app.command()
def check(
    alert_type: str = typer.Option("log", help="告警类型: log/email/webhook"),
):
    """检查所有 SLA 是否违约。"""
    manager = SLAManager()

    # 如果没有 SLA 定义，加载默认
    slas = manager.get_all_slas()
    if not slas:
        log.info("未找到 SLA 定义，加载默认 SLA...")
        manager.load_default_slas()

    result = manager.check_all_sla_violations()

    typer.echo(f"检查日期: {result['check_date']}")
    typer.echo(f"总 SLA 数: {result['total_slas']}")
    typer.echo(f"违约数: {result['violations']}")
    typer.echo(f"违约率: {result['violation_rate']:.1%}")

    if result["violations"] > 0:
        typer.echo("\n违约详情:")
        for d in result["details"]:
            if d["is_violation"]:
                typer.echo(
                    f"  ✗ {d['namespace']}.{d['table_name']}: "
                    f"延迟 {d['delay_minutes']} 分钟"
                )

        # 发送告警
        if alert_type == "log":
            log.warning(f"SLA 违约: {result['violations']}/{result['total_slas']}")
        # TODO: 实现 email/webhook 告警

        raise typer.Exit(code=1)
    else:
        typer.echo("✓ 所有 SLA 均达成")


@app.command()
def report(
    days: int = typer.Option(7, help="统计天数"),
):
    """生成 SLA 达成率报告。"""
    manager = SLAManager()
    report = manager.get_sla_compliance_report(days)

    typer.echo(f"统计周期: 最近 {report['period_days']} 天")
    typer.echo(f"总检查数: {report['total_checks']}")
    typer.echo(f"总违约数: {report['total_violations']}")
    typer.echo(f"达成率: {report['compliance_rate']:.1%}")

    if report["by_sla"]:
        typer.echo("\n按 SLA 统计:")
        for sla in report["by_sla"]:
            typer.echo(
                f"  {sla['namespace']}.{sla['table_name']} ({sla['priority']}): "
                f"{sla['violations']}/{sla['total_checks']} 违约"
            )


@app.command()
def define_sla(
    table: str = typer.Argument(..., help="表名（如 ju.valuation）"),
    time: str = typer.Option(..., help="期望时间（如 09:30）"),
    sla_type: str = typer.Option("daily_refresh", help="SLA 类型"),
    grace_period: int = typer.Option(60, help="宽限期（分钟）"),
    priority: str = typer.Option("medium", help="优先级"),
    description: str = typer.Option(None, help="描述"),
):
    """定义 SLA。"""
    parts = table.split(".")
    if len(parts) != 2:
        typer.echo("表名格式错误，应为 namespace.table_name", err=True)
        raise typer.Exit(code=1)

    namespace, table_name = parts

    manager = SLAManager()
    manager.define_sla(
        namespace=namespace,
        table_name=table_name,
        sla_type=sla_type,
        expected_time=time,
        grace_period_minutes=grace_period,
        priority=priority,
        description=description,
    )

    typer.echo(f"✓ 已定义 SLA: {table}")


@app.command()
def list_slas():
    """列出所有 SLA 定义。"""
    manager = SLAManager()
    slas = manager.get_all_slas()

    if not slas:
        typer.echo("未找到 SLA 定义")
        return

    typer.echo(f"共 {len(slas)} 个 SLA 定义:\n")
    for sla in slas:
        typer.echo(f"{sla['namespace']}.{sla['table_name']}")
        typer.echo(f"  类型: {sla['sla_type']}")
        typer.echo(f"  期望时间: {sla['expected_time']} ({sla['timezone']})")
        typer.echo(f"  宽限期: {sla['grace_period_minutes']} 分钟")
        typer.echo(f"  优先级: {sla['priority']}")
        typer.echo(f"  描述: {sla.get('description', 'N/A')}")
        typer.echo()


if __name__ == "__main__":
    app()
