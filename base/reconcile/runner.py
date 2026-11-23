"""P4: 跨源对账执行器。

职责：对重叠数据交叉验证，差异记录到 public.reconcile_status，不做自动修正。

对账规则：
  - stocks_vs_symbol_meta: ju.stocks ↔ tong.symbol_meta（V12 标识唯一性）
  - calendar_completeness: ju.trading_day 完整性检查（V3 需外部数据，此处做基础检查）
  - event_anomalies: corporate_action_event 异常检测（P2 事件流质量）
  - symbol_coverage: canonical_symbol 覆盖率检查（V4 股票列表完整性）

差异只记录不修正，人工裁决。
"""
import logging
from typing import Optional, Dict, Any, List
from datetime import date, datetime

import pandas as pd

from base.db import read_sql, execute_sql

log = logging.getLogger(__name__)


def reconcile_stocks_vs_symbol_meta(run_date: Optional[str] = None) -> Dict[str, Any]:
    """对账：ju.stocks ↔ tong.symbol_meta（V12 标识唯一性）。

    检查：
      - ju.stocks 中的股票是否都在 tong.symbol_meta 中
      - tong.symbol_meta 中的股票是否都在 ju.stocks 中
      - exchange 映射是否一致

    Returns:
        对账结果 {left_count, right_count, match_count, mismatch_count, status, detail}
    """
    log.info("对账: ju.stocks ↔ tong.symbol_meta")

    if run_date is None:
        run_date = date.today().isoformat()

    # 读取 ju.stocks
    ju_stocks = read_sql("""
        SELECT stock_code, exchange
        FROM ju.stocks
        WHERE stock_code IS NOT NULL
    """)
    ju_codes = set(ju_stocks["stock_code"].unique())

    # 读取 tong.symbol_meta（只取股票类）
    tong_meta = read_sql("""
        SELECT symbol, class
        FROM tong.symbol_meta
        WHERE symbol IS NOT NULL AND class IN ('stock', 'bstock')
    """)
    # 提取 code（去掉 sh/sz/bj 前缀）
    tong_meta["code"] = tong_meta["symbol"].str[2:]
    tong_codes = set(tong_meta["code"].unique())

    # 比对
    only_in_ju = ju_codes - tong_codes
    only_in_tong = tong_codes - ju_codes
    common = ju_codes & tong_codes

    left_count = len(ju_codes)
    right_count = len(tong_codes)
    match_count = len(common)
    mismatch_count = len(only_in_ju) + len(only_in_tong)

    status = "pass" if mismatch_count == 0 else "warn" if mismatch_count < 10 else "fail"

    detail = {
        "only_in_ju_sample": list(only_in_ju)[:10],
        "only_in_tong_sample": list(only_in_tong)[:10],
    }

    result = {
        "check_name": "stocks_vs_symbol_meta",
        "run_date": run_date,
        "left_count": left_count,
        "right_count": right_count,
        "match_count": match_count,
        "mismatch_count": mismatch_count,
        "status": status,
        "detail": detail,
    }

    # 写入 reconcile_status
    _write_reconcile_result(result)

    log.info(f"对账完成: match={match_count}, mismatch={mismatch_count}, status={status}")
    return result


def reconcile_calendar_completeness(run_date: Optional[str] = None) -> Dict[str, Any]:
    """对账：ju.trading_day 完整性检查。

    基础检查（V3 需外部数据，此处做基础合理性检查）：
      - 交易日不应在周末（周六/周日）
      - 交易日不应在重大节假日（春节/国庆等，需外部数据）
      - 交易日连续性检查（不应有超过 N 天的间隔）

    Returns:
        对账结果
    """
    log.info("对账: ju.trading_day 完整性检查")

    if run_date is None:
        run_date = date.today().isoformat()

    # 读取交易日历
    trading_days = read_sql("""
        SELECT trade_date
        FROM ju.trading_day
        WHERE trade_date IS NOT NULL
        ORDER BY trade_date
    """)

    if trading_days.empty:
        return {
            "check_name": "calendar_completeness",
            "run_date": run_date,
            "left_count": 0,
            "right_count": 0,
            "match_count": 0,
            "mismatch_count": 0,
            "status": "fail",
            "detail": {"error": "ju.trading_day 为空"},
        }

    # 检查 1: 交易日是否在周末
    trading_days["date"] = pd.to_datetime(trading_days["trade_date"])
    trading_days["weekday"] = trading_days["date"].dt.weekday
    weekend_trading = trading_days[trading_days["weekday"] >= 5]

    # 检查 2: 交易日间隔（超过 7 天可能是问题）
    trading_days = trading_days.sort_values("date")
    trading_days["gap"] = trading_days["date"].diff().dt.days
    long_gaps = trading_days[trading_days["gap"] > 7]

    mismatch_count = len(weekend_trading) + len(long_gaps)
    status = "pass" if mismatch_count == 0 else "warn"

    detail = {
        "weekend_trading_count": len(weekend_trading),
        "weekend_trading_sample": weekend_trading["trade_date"].head(5).tolist(),
        "long_gap_count": len(long_gaps),
        "long_gap_sample": long_gaps[["trade_date", "gap"]].head(5).to_dict("records"),
    }

    result = {
        "check_name": "calendar_completeness",
        "run_date": run_date,
        "left_count": len(trading_days),
        "right_count": None,  # 无外部数据源
        "match_count": len(trading_days) - mismatch_count,
        "mismatch_count": mismatch_count,
        "status": status,
        "detail": detail,
    }

    _write_reconcile_result(result)

    log.info(f"对账完成: weekend_trading={len(weekend_trading)}, long_gaps={len(long_gaps)}, status={status}")
    return result


def reconcile_event_anomalies(run_date: Optional[str] = None) -> Dict[str, Any]:
    """对账：corporate_action_event 异常检测。

    调用 events.ingest.detect_event_anomalies() 检测事件流质量。

    Returns:
        对账结果
    """
    log.info("对账: corporate_action_event 异常检测")

    if run_date is None:
        run_date = date.today().isoformat()

    from base.events.ingest import detect_event_anomalies

    anomalies = detect_event_anomalies()

    # 统计事件总数
    event_count_result = read_sql("SELECT COUNT(*) as cnt FROM public.corporate_action_event")
    event_count = event_count_result.iloc[0]["cnt"] if not event_count_result.empty else 0

    mismatch_count = len(anomalies)
    status = "pass" if mismatch_count == 0 else "warn" if mismatch_count < 10 else "fail"

    detail = {
        "anomalies_sample": anomalies[:10],
    }

    result = {
        "check_name": "event_anomalies",
        "run_date": run_date,
        "left_count": event_count,
        "right_count": None,
        "match_count": event_count - mismatch_count,
        "mismatch_count": mismatch_count,
        "status": status,
        "detail": detail,
    }

    _write_reconcile_result(result)

    log.info(f"对账完成: events={event_count}, anomalies={mismatch_count}, status={status}")
    return result


def reconcile_symbol_coverage(run_date: Optional[str] = None) -> Dict[str, Any]:
    """对账：canonical_symbol 覆盖率检查（V4 股票列表完整性）。

    检查：
      - canonical_symbol 是否覆盖 ju.stocks 全部股票
      - canonical_symbol 是否覆盖 tong.symbol_meta 全部股票类证券

    Returns:
        对账结果
    """
    log.info("对账: canonical_symbol 覆盖率检查")

    if run_date is None:
        run_date = date.today().isoformat()

    # canonical_symbol 数量
    canonical_count_result = read_sql("SELECT COUNT(*) as cnt FROM public.canonical_symbol")
    canonical_count = canonical_count_result.iloc[0]["cnt"] if not canonical_count_result.empty else 0

    # ju.stocks 数量
    ju_count_result = read_sql("SELECT COUNT(DISTINCT stock_code) as cnt FROM ju.stocks")
    ju_count = ju_count_result.iloc[0]["cnt"] if not ju_count_result.empty else 0

    # tong.symbol_meta 股票类数量
    tong_count_result = read_sql("""
        SELECT COUNT(DISTINCT symbol) as cnt
        FROM tong.symbol_meta
        WHERE class IN ('stock', 'bstock')
    """)
    tong_count = tong_count_result.iloc[0]["cnt"] if not tong_count_result.empty else 0

    # 检查覆盖率
    # canonical_symbol 应该 >= ju.stocks 且 >= tong.symbol_meta 股票类
    missing_from_ju = max(0, ju_count - canonical_count)
    missing_from_tong = max(0, tong_count - canonical_count)

    mismatch_count = missing_from_ju + missing_from_tong
    status = "pass" if mismatch_count == 0 else "warn"

    detail = {
        "canonical_count": canonical_count,
        "ju_stocks_count": ju_count,
        "tong_stock_count": tong_count,
        "missing_from_ju": missing_from_ju,
        "missing_from_tong": missing_from_tong,
    }

    result = {
        "check_name": "symbol_coverage",
        "run_date": run_date,
        "left_count": canonical_count,
        "right_count": ju_count + tong_count,
        "match_count": canonical_count,
        "mismatch_count": mismatch_count,
        "status": status,
        "detail": detail,
    }

    _write_reconcile_result(result)

    log.info(f"对账完成: canonical={canonical_count}, ju={ju_count}, tong={tong_count}, status={status}")
    return result


def _write_reconcile_result(result: Dict[str, Any]) -> None:
    """写入对账结果到 public.reconcile_status。"""
    import json

    # 从 result 推断 namespace 和 table_name
    check_name = result["check_name"]
    if check_name == "stocks_vs_symbol_meta":
        namespace = "ju"
        table_name = "stocks"
    elif check_name == "calendar_completeness":
        namespace = "ju"
        table_name = "trading_day"
    elif check_name == "event_anomalies":
        namespace = "public"
        table_name = "corporate_action_event"
    elif check_name == "symbol_coverage":
        namespace = "public"
        table_name = "canonical_symbol"
    else:
        namespace = "unknown"
        table_name = "unknown"

    execute_sql("""
        INSERT INTO public.reconcile_status
            (namespace, table_name, check_type, check_date, status, details)
        VALUES (%s, %s, %s, %s, %s, %s::jsonb)
        ON CONFLICT (namespace, table_name, check_type, check_date) DO UPDATE SET
            status = EXCLUDED.status,
            details = EXCLUDED.details,
            checked_at = NOW()
    """, params=(
        namespace,
        table_name,
        check_name,
        result["run_date"],
        result["status"],
        json.dumps(result.get("detail", {}), default=str),
    ))


def run_reconciliation(run_date: Optional[str] = None) -> List[Dict[str, Any]]:
    """执行全部对账规则，结果写 public.reconcile_status。

    规则注册表（check_name）：
      - stocks_vs_symbol_meta: ju.stocks ↔ tong.symbol_meta（V12）
      - calendar_completeness: ju.trading_day 完整性检查（V3 基础检查）
      - event_anomalies: corporate_action_event 异常检测
      - symbol_coverage: canonical_symbol 覆盖率检查（V4）

    差异只记录不修正。

    Returns:
        全部对账结果列表
    """
    log.info("开始执行跨源对账...")

    if run_date is None:
        run_date = date.today().isoformat()

    results = []

    # 执行各对账规则
    results.append(reconcile_stocks_vs_symbol_meta(run_date))
    results.append(reconcile_calendar_completeness(run_date))
    results.append(reconcile_event_anomalies(run_date))
    results.append(reconcile_symbol_coverage(run_date))

    # 汇总
    pass_count = sum(1 for r in results if r["status"] == "pass")
    warn_count = sum(1 for r in results if r["status"] == "warn")
    fail_count = sum(1 for r in results if r["status"] == "fail")

    log.info(f"对账完成: pass={pass_count}, warn={warn_count}, fail={fail_count}")

    return results
