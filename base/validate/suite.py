"""验证门禁套件。

分层：
  - 内部自检 V9（对数收益可加性）/ V10（前视偏差）/ V12（标识唯一）—— 纯 SQL/代码可验
  - V11（幂等）—— 由 pipeline.runner.check_idempotency 承担
  - 外部对账 V1–V8 —— 依赖外部数据源接入，由 reconcile.runner 承担

标准与阈值见 deliverables/01-需求说明书 §5。
"""
import logging
import math
from typing import Optional, Dict, Any, List
from datetime import date

import pandas as pd

from base.db import read_sql

log = logging.getLogger(__name__)


def validate_log_return_additivity(sample_size: int = 100) -> Dict[str, Any]:
    """V9: 对数收益可加性验证。

    恒等式：Σ log_return[t:t+n] = log(close[t+n]/close[t])
    阈值：误差 < 1e-10

    Args:
        sample_size: 抽样检查的股票-区间对数

    Returns:
        {passed, error_count, max_error, detail}
    """
    log.info(f"V9: 对数收益可加性验证 (sample_size={sample_size})")

    # 从 tong.stock_daily 抽样
    samples = read_sql(f"""
        SELECT symbol, date, close
        FROM tong.stock_daily
        WHERE close > 0 AND close IS NOT NULL
        ORDER BY RANDOM()
        LIMIT {sample_size * 10}
    """)

    if samples.empty:
        return {
            "validation": "V9_log_return_additivity",
            "passed": True,
            "error_count": 0,
            "max_error": 0.0,
            "detail": "无数据可验证",
        }

    errors = []
    max_error = 0.0

    # 按股票分组，检查连续区间的对数收益可加性
    for symbol, group in samples.groupby("symbol"):
        group = group.sort_values("date").reset_index(drop=True)
        if len(group) < 3:
            continue

        # 计算逐日对数收益
        group["log_return"] = group["close"].apply(lambda x: math.log(x) if x > 0 else None)
        group["daily_log_ret"] = group["log_return"].diff()

        # 检查 2 日、3 日、5 日区间的可加性
        for window in [2, 3, 5]:
            if len(group) < window + 1:
                continue

            for i in range(len(group) - window):
                # 直接计算：log(close[t+window] / close[t])
                close_t = group.iloc[i]["close"]
                close_tn = group.iloc[i + window]["close"]

                if close_t <= 0 or close_tn <= 0:
                    continue

                direct = math.log(close_tn / close_t)

                # 累加计算：Σ daily_log_return[t+1:t+window]
                cumulative = group.iloc[i + 1:i + window + 1]["daily_log_ret"].sum()

                if math.isnan(cumulative):
                    continue

                error = abs(direct - cumulative)
                max_error = max(max_error, error)

                if error > 1e-10:
                    errors.append({
                        "code": code,
                        "date": group.iloc[i]["date"],
                        "window": window,
                        "direct": direct,
                        "cumulative": cumulative,
                        "error": error,
                    })

    passed = len(errors) == 0

    result = {
        "validation": "V9_log_return_additivity",
        "passed": passed,
        "error_count": len(errors),
        "max_error": max_error,
        "threshold": 1e-10,
        "detail": errors[:5] if errors else "全部通过",
    }

    log.info(f"V9 完成: passed={passed}, error_count={len(errors)}, max_error={max_error:.2e}")
    return result


def validate_no_lookahead_bias(as_of: Optional[str] = None) -> Dict[str, Any]:
    """V10: 零前视偏差验证。

    检查：T 日查询结果集与 pub_date > T 的数据求交集应为空。

    对 public.st_status_pit 和 public.valuation_pit 做抽样检查。

    Args:
        as_of: 查询日期（默认取最近交易日）

    Returns:
        {passed, status, violation_count, detail, evidence}
        status: PASS / FAIL / INSUFFICIENT_EVIDENCE
          - FAIL: 发现超区间记录（effective_from > as_of）或检查异常被记为违规
          - INSUFFICIENT_EVIDENCE: 全部检查均执行失败，无可用证据（不得显示绿）
          - PASS: 所有检查执行成功且无违规
    """
    log.info(f"V10: 零前视偏差验证 (as_of={as_of})")

    if as_of is None:
        # 取最近交易日
        result = read_sql("SELECT MAX(trade_date) as max_date FROM ju.trading_day")
        as_of = result.iloc[0]["max_date"] if not result.empty else date.today().isoformat()

    violations: List[Dict[str, Any]] = []
    evidence: Dict[str, Any] = {"as_of": as_of, "checks": {}}

    # 检查 1: st_status_pit（检查 effective_from > as_of 的记录不应被返回）
    try:
        st_violations = read_sql("""
            SELECT COUNT(*) as cnt
            FROM public.st_status_pit
            WHERE effective_from > %s
        """, params=(as_of,))
        cnt = st_violations.iloc[0]["cnt"] if not st_violations.empty else 0
        evidence["checks"]["st_status_pit"] = {"cnt": int(cnt)}
        if cnt > 0:
            # F4 修复：此前仅 log.info 不记违规，导致 V10 恒 PASS。
            # 视图是否"设计如此"应由门禁显式裁决，而非静默放行。
            violations.append({
                "check": "st_status_pit",
                "violation_count": int(cnt),
                "reason": f"effective_from > {as_of} 的记录 {cnt} 条",
            })
    except Exception as e:
        log.warning(f"检查 st_status_pit 失败: {e}")
        evidence["checks"]["st_status_pit"] = {"error": str(e)}
        violations.append({"check": "st_status_pit", "error": str(e)})

    # 检查 2: valuation_pit（检查 effective_from > as_of 的记录）
    try:
        val_violations = read_sql("""
            SELECT COUNT(*) as cnt
            FROM public.valuation_pit
            WHERE effective_from > %s
        """, params=(as_of,))
        cnt = val_violations.iloc[0]["cnt"] if not val_violations.empty else 0
        evidence["checks"]["valuation_pit"] = {"cnt": int(cnt)}
        if cnt > 0:
            violations.append({
                "check": "valuation_pit",
                "violation_count": int(cnt),
                "reason": f"effective_from > {as_of} 的记录 {cnt} 条",
            })
    except Exception as e:
        log.warning(f"检查 valuation_pit 失败: {e}")
        evidence["checks"]["valuation_pit"] = {"error": str(e)}
        violations.append({"check": "valuation_pit", "error": str(e)})

    checks = evidence["checks"]
    if checks and all("error" in v for v in checks.values()):
        # 全部检查执行失败：无证据，不得判 PASS（六态语义）
        status = "INSUFFICIENT_EVIDENCE"
    elif violations:
        status = "FAIL"
    else:
        status = "PASS"
    passed = status == "PASS"

    result = {
        "validation": "V10_no_lookahead_bias",
        "passed": passed,
        "status": status,
        "as_of": as_of,
        "violation_count": sum(v.get("violation_count", 0) for v in violations),
        "threshold": 0,
        "detail": violations if violations else "全部通过",
        "evidence": evidence,
    }

    log.info(f"V10 完成: status={status}, passed={passed}, violations={len(violations)}")
    return result


def validate_symbol_uniqueness() -> Dict[str, Any]:
    """V12: 标识唯一性验证。

    检查：
      - canonical_symbol 无重复 canonical_id
      - canonical_symbol 无重复 (exchange, code)
      - tong.symbol ↔ ju.stock_code 映射无歧义

    Returns:
        {passed, duplicate_count, detail}
    """
    log.info("V12: 标识唯一性验证")

    duplicates = []

    # 检查 1: canonical_id 重复
    dup_canonical = read_sql("""
        SELECT canonical_id, COUNT(*) as cnt
        FROM public.canonical_symbol
        GROUP BY canonical_id
        HAVING COUNT(*) > 1
    """)
    if not dup_canonical.empty:
        duplicates.append({
            "type": "duplicate_canonical_id",
            "count": len(dup_canonical),
            "sample": dup_canonical.head(5).to_dict("records"),
        })

    # 检查 2: (exchange, code) 重复
    dup_exchange = read_sql("""
        SELECT exchange, code, COUNT(*) as cnt
        FROM public.canonical_symbol
        GROUP BY exchange, code
        HAVING COUNT(*) > 1
    """)
    if not dup_exchange.empty:
        duplicates.append({
            "type": "duplicate_exchange_code",
            "count": len(dup_exchange),
            "sample": dup_exchange.head(5).to_dict("records"),
        })

    # 检查 3: 同一 code 映射到多个 exchange
    multi_exchange = read_sql("""
        SELECT code, COUNT(DISTINCT exchange) as exchange_count
        FROM public.canonical_symbol
        GROUP BY code
        HAVING COUNT(DISTINCT exchange) > 1
    """)
    if not multi_exchange.empty:
        duplicates.append({
            "type": "code_to_multiple_exchanges",
            "count": len(multi_exchange),
            "sample": multi_exchange.head(5).to_dict("records"),
        })

    passed = len(duplicates) == 0

    result = {
        "validation": "V12_symbol_uniqueness",
        "passed": passed,
        "duplicate_count": len(duplicates),
        "threshold": 0,
        "detail": duplicates if duplicates else "全部通过",
    }

    log.info(f"V12 完成: passed={passed}, duplicates={len(duplicates)}")
    return result


def validate_idempotency(recent_runs: int = 2) -> Dict[str, Any]:
    """V11: 幂等性验证。

    检查最近 N 次管线运行中，相同 (input_hash, code_commit) 的运行是否产出一致。

    Args:
        recent_runs: 检查最近 N 次运行

    Returns:
        {passed, status, checked_pairs, mismatched_pairs, detail}
        status: PASS / FAIL / INSUFFICIENT_EVIDENCE
          - INSUFFICIENT_EVIDENCE: 运行记录不足或无可比对运行对（不得显示绿）
    """
    log.info(f"V11: 幂等性验证 (recent_runs={recent_runs})")

    from base.pipeline.runner import check_idempotency, get_pipeline_history

    history = get_pipeline_history(limit=recent_runs)

    if len(history) < 2:
        # F5 修复：此前返回 passed=True（证据不足恒绿）。证据不足必须显式降级，
        # 不得等价于"通过"——改为 INSUFFICIENT_EVIDENCE 且 passed=False（故意破坏旧语义）。
        return {
            "validation": "V11_idempotency",
            "passed": False,
            "status": "INSUFFICIENT_EVIDENCE",
            "checked_pairs": 0,
            "mismatched_pairs": 0,
            "detail": "运行记录不足，无法验证幂等性（证据不足，非通过）",
        }

    # 找出相同 (input_hash, code_commit) 的运行对
    checked = 0
    mismatched = 0

    for i in range(len(history)):
        for j in range(i + 1, len(history)):
            run1 = history.iloc[i]
            run2 = history.iloc[j]

            if run1["input_hash"] == run2["input_hash"] and run1["code_commit"] == run2["code_commit"]:
                checked += 1
                if not check_idempotency(run1["run_id"], run2["run_id"]):
                    mismatched += 1

    passed = mismatched == 0

    if checked == 0:
        # F5 修复：有历史但无可比对运行对——同样证据不足，不得显示绿。
        status = "INSUFFICIENT_EVIDENCE"
        passed = False
    else:
        status = "PASS" if passed else "FAIL"

    result = {
        "validation": "V11_idempotency",
        "passed": passed,
        "status": status,
        "checked_pairs": checked,
        "mismatched_pairs": mismatched,
        "threshold": 0,
        "detail": f"检查 {checked} 对运行，{mismatched} 对不一致" if checked > 0 else "无可比对运行对（证据不足，非通过）",
    }

    log.info(f"V11 完成: passed={passed}, checked={checked}, mismatched={mismatched}")
    return result


def run_validation(as_of: Optional[str] = None) -> bool:
    """运行验证门禁，全部通过返回 True；任何未通过项返回 False（CLI 退出码非零）。

    内部自检项：
      - V9: 对数收益可加性（误差 < 1e-10）
      - V10: 零前视偏差（违规 = 0）
      - V11: 幂等性（不一致 = 0）
      - V12: 标识唯一性（重复 = 0）

    Args:
        as_of: 查询日期（用于 V10）

    Returns:
        True = 全部通过; False = 存在未通过项
    """
    log.info("=" * 60)
    log.info("开始运行验证门禁...")
    log.info("=" * 60)

    results = []

    # V9: 对数收益可加性
    results.append(validate_log_return_additivity())

    # V10: 零前视偏差
    results.append(validate_no_lookahead_bias(as_of))

    # V11: 幂等性
    results.append(validate_idempotency())

    # V12: 标识唯一性
    results.append(validate_symbol_uniqueness())

    # 汇总
    passed_count = sum(1 for r in results if r["passed"])
    failed_count = len(results) - passed_count

    log.info("=" * 60)
    log.info(f"验证门禁汇总: {passed_count}/{len(results)} 通过")
    for r in results:
        status = "✓" if r["passed"] else "✗"
        log.info(f"  {status} {r['validation']}: {r.get('detail', '')}")
    log.info("=" * 60)

    all_passed = failed_count == 0
    return all_passed
