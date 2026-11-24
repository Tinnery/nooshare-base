"""P5: 数据新鲜度看板采集器。

职责：扫描各源 namespace 的关键表，记录 max_date / row_count / status 到 public.data_freshness。

下游分析前强制检查依赖源 status = fresh，避免用过期数据回测。

stale 阈值配置（按表）：
  - tong.stock_daily / tong.index_daily / tong.etf_daily: 1 个交易日（默认 2 天）
  - ju.stocks / ju.trading_day: 7 天
  - ju.pub_date / ju.st_period / ju.valuation: 30 天
  - finance.*: 90 天（财报季度更新）
"""
import logging
from typing import Optional, Dict, Any, List
from datetime import date, datetime, timedelta

import pandas as pd

from base.db import read_sql, execute_sql

log = logging.getLogger(__name__)


# stale 阈值配置（天数）
STALE_THRESHOLDS = {
    # tong 日线数据：1 个交易日（默认 2 天）
    ("tong", "stock_daily"): 2,
    ("tong", "index_daily"): 2,
    ("tong", "etf_daily"): 2,
    ("tong", "gbbq"): 7,
    ("tong", "symbol_meta"): 30,

    # ju 元数据
    ("ju", "stocks"): 7,
    ("ju", "trading_day"): 7,
    ("ju", "pub_date"): 30,
    ("ju", "st_period"): 30,
    ("ju", "valuation"): 30,
    ("ju", "limit_ratio"): 30,
    ("ju", "name_history"): 90,

    # finance 财报数据
    ("finance", "income_statement"): 90,
    ("finance", "balance_sheet"): 90,
    ("finance", "cash_flow_statement"): 90,

    # public 层产出
    ("public", "canonical_symbol"): 7,
    ("public", "corporate_action_event"): 7,
}

# 默认 stale 阈值
DEFAULT_STALE_DAYS = 30


def collect_table_freshness(
    namespace: str,
    table_name: str,
    date_col: Optional[str] = None,
) -> Dict[str, Any]:
    """采集单张表的新鲜度信息。

    Args:
        namespace: 命名空间（tong/ju/finance/public）
        table_name: 表名
        date_col: 时间戳列名（如 trade_date/report_date）。如果为 None，只统计行数。

    Returns:
        {namespace, table_name, max_date, row_count, refreshed_at, status}
    """
    full_table = f"{namespace}.{table_name}"
    log.info(f"采集新鲜度: {full_table}")

    # 检查表是否存在
    exists_result = read_sql("""
        SELECT COUNT(*) as cnt
        FROM information_schema.tables
        WHERE table_schema = %s AND table_name = %s
    """, params=(namespace, table_name))

    if exists_result.empty or exists_result.iloc[0]["cnt"] == 0:
        log.warning(f"表不存在: {full_table}")
        return {
            "namespace": namespace,
            "table_name": table_name,
            "max_date": None,
            "row_count": 0,
            "refreshed_at": datetime.now(),
            "status": "missing",
        }

    # 统计行数
    count_result = read_sql(f"SELECT COUNT(*) as cnt FROM {full_table}")
    row_count = int(count_result.iloc[0]["cnt"]) if not count_result.empty else 0

    # 获取 max_date
    max_date = None
    if date_col and row_count > 0:
        try:
            max_date_result = read_sql(f"SELECT MAX({date_col}) as max_date FROM {full_table}")
            if not max_date_result.empty:
                max_date_val = max_date_result.iloc[0]["max_date"]
                if pd.notna(max_date_val):
                    if isinstance(max_date_val, (date, datetime)):
                        max_date = max_date_val
                    else:
                        val_str = str(max_date_val)
                        if val_str.isdigit() and len(val_str) == 8:
                            # YYYYMMDD（tong.* 为 INTEGER、ju.*/finance.* 为 TEXT 数字串）。
                            # 不能让 pandas 把整数当 ns epoch 解析（会得到 1970-01-01）。
                            max_date = pd.to_datetime(val_str, format="%Y%m%d").date()
                        else:
                            max_date = pd.to_datetime(max_date_val).date()
        except Exception as e:
            log.warning(f"获取 max_date 失败: {full_table}.{date_col}: {e}")

    # 判断 status
    # P1.4 修复: 当没有 date_col 但表有数据时,用 refreshed_at 作为代理指标
    refreshed_at = datetime.now()
    if max_date is None and row_count > 0 and not date_col:
        # 没有日期列但有数据,尝试从 data_freshness 表查询上次的 refreshed_at
        # 如果是首次采集,使用当前时间作为 refreshed_at,状态设为 fresh
        prev_freshness = read_sql("""
            SELECT refreshed_at, status
            FROM public.data_freshness
            WHERE namespace = %s AND table_name = %s
        """, params=(namespace, table_name))
        
        if not prev_freshness.empty:
            # 有历史记录,用上次的 refreshed_at 判断
            prev_refreshed = prev_freshness.iloc[0]["refreshed_at"]
            if prev_refreshed:
                # 用 refreshed_at 代替 max_date 进行状态判断
                status = _determine_status_by_refreshed(namespace, table_name, prev_refreshed)
            else:
                status = "fresh"  # 首次采集且有数据
        else:
            status = "fresh"  # 首次采集且有数据
    else:
        status = _determine_status(namespace, table_name, max_date)

    return {
        "namespace": namespace,
        "table_name": table_name,
        "max_date": max_date,
        "row_count": row_count,
        "refreshed_at": refreshed_at,
        "status": status,
    }


def _determine_status(namespace: str, table_name: str, max_date: Optional[date]) -> str:
    """根据 max_date 和阈值判断状态。

    Returns:
        fresh / stale / missing
    """
    if max_date is None:
        return "missing"

    threshold_days = STALE_THRESHOLDS.get((namespace, table_name), DEFAULT_STALE_DAYS)
    today = date.today()

    if isinstance(max_date, datetime):
        max_date = max_date.date()

    days_since = (today - max_date).days

    if days_since <= threshold_days:
        return "fresh"
    elif days_since <= threshold_days * 2:
        return "stale"
    else:
        return "missing"


def _determine_status_by_refreshed(namespace: str, table_name: str, refreshed_at: datetime) -> str:
    """P1.4: 根据上次刷新时间判断状态（用于没有日期列的表）。

    对于没有 date_col 的表（如元数据表），用 refreshed_at 代替 max_date 判断。

    Args:
        namespace: 命名空间
        table_name: 表名
        refreshed_at: 上次刷新时间

    Returns:
        fresh / stale / missing
    """
    threshold_days = STALE_THRESHOLDS.get((namespace, table_name), DEFAULT_STALE_DAYS)
    today = date.today()

    if refreshed_at.tzinfo is not None:
        refreshed_at = refreshed_at.replace(tzinfo=None)

    refreshed_date = refreshed_at.date()
    days_since = (today - refreshed_date).days

    if days_since <= threshold_days:
        return "fresh"
    elif days_since <= threshold_days * 2:
        return "stale"
    else:
        return "missing"


def collect_all_freshness() -> List[Dict[str, Any]]:
    """采集所有关键表的新鲜度。

    Returns:
        新鲜度记录列表
    """
    log.info("开始采集数据新鲜度...")

    results = []

    # tong 表
    results.append(collect_table_freshness("tong", "stock_daily", "date"))
    results.append(collect_table_freshness("tong", "index_daily", "date"))
    results.append(collect_table_freshness("tong", "etf_daily", "date"))
    results.append(collect_table_freshness("tong", "gbbq", "date"))
    results.append(collect_table_freshness("tong", "symbol_meta"))

    # ju 表
    results.append(collect_table_freshness("ju", "stocks"))
    results.append(collect_table_freshness("ju", "trading_day", "trade_date"))
    results.append(collect_table_freshness("ju", "pub_date", "pub_date"))
    results.append(collect_table_freshness("ju", "st_period", "start_date"))
    results.append(collect_table_freshness("ju", "valuation", "start_date"))

    # finance 表
    results.append(collect_table_freshness("finance", "income_statement", "report_date"))
    results.append(collect_table_freshness("finance", "balance_sheet", "report_date"))
    results.append(collect_table_freshness("finance", "cash_flow_statement", "report_date"))

    # public 表
    results.append(collect_table_freshness("public", "canonical_symbol"))
    results.append(collect_table_freshness("public", "corporate_action_event", "effective_date"))

    log.info(f"采集完成: {len(results)} 张表")
    return results


def write_freshness_to_db(freshness_records: List[Dict[str, Any]]) -> None:
    """将新鲜度记录写入 public.data_freshness。"""
    import json

    log.info(f"写入 {len(freshness_records)} 条新鲜度记录...")

    for record in freshness_records:
        max_date_str = record["max_date"].isoformat() if record["max_date"] else None

        execute_sql("""
            INSERT INTO public.data_freshness
                (namespace, table_name, max_date, row_count, refreshed_at, status)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (namespace, table_name) DO UPDATE SET
                max_date = EXCLUDED.max_date,
                row_count = EXCLUDED.row_count,
                refreshed_at = EXCLUDED.refreshed_at,
                status = EXCLUDED.status
        """, params=(
            record["namespace"],
            record["table_name"],
            max_date_str,
            record["row_count"],
            record["refreshed_at"],
            record["status"],
        ))

    log.info("新鲜度写入完成")


def collect_freshness(as_of: Optional[str] = None) -> List[Dict[str, Any]]:
    """采集并写入数据新鲜度看板。

    Args:
        as_of: 保留参数（当前未使用，未来支持历史快照）

    Returns:
        新鲜度记录列表
    """
    freshness_records = collect_all_freshness()
    write_freshness_to_db(freshness_records)

    # 汇总统计
    fresh_count = sum(1 for r in freshness_records if r["status"] == "fresh")
    stale_count = sum(1 for r in freshness_records if r["status"] == "stale")
    missing_count = sum(1 for r in freshness_records if r["status"] == "missing")

    log.info(f"新鲜度汇总: fresh={fresh_count}, stale={stale_count}, missing={missing_count}")

    return freshness_records


def check_dependencies_fresh(dependencies: List[tuple]) -> bool:
    """检查依赖的数据源是否都是 fresh 状态。

    下游分析前调用，确保不会用过期数据回测。

    Args:
        dependencies: 依赖列表，如 [("tong", "stock_daily"), ("ju", "stocks")]

    Returns:
        True = 全部 fresh; False = 存在 stale/missing
    """
    log.info(f"检查依赖新鲜度: {dependencies}")

    all_fresh = True

    for namespace, table_name in dependencies:
        result = read_sql("""
            SELECT status, max_date, refreshed_at
            FROM public.data_freshness
            WHERE namespace = %s AND table_name = %s
        """, params=(namespace, table_name))

        if result.empty:
            log.warning(f"依赖缺失: {namespace}.{table_name} 无新鲜度记录")
            all_fresh = False
            continue

        status = result.iloc[0]["status"]
        if status != "fresh":
            log.warning(f"依赖不新鲜: {namespace}.{table_name} status={status}")
            all_fresh = False

    if all_fresh:
        log.info("依赖新鲜度检查通过")
    else:
        log.warning("依赖新鲜度检查未通过")

    return all_fresh
