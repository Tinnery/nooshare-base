"""P3: 双时态查询工具。

职责：提供 point-in-time 查询辅助函数，支持按 pub_date / effective_date 做 as-of 过滤。

PIT 视图已在 migration 002 中定义（income_statement_pit / st_status_pit 等），
本模块提供 Python 侧的查询封装，确保下游消费者正确使用 PIT 语义。

核心铁律：T 日分析只能看到 pub_date ≤ T 的数据 —— 零前视偏差。

修复记录：
  2026-11-27: Phase A - query_adjustment_factors() 扩投影到 rights_issue。
              增加 PIT_UNPROVEN 标注（observed_at 为批量导入时刻，不构成历史 PIT 证明）。
"""
import logging
from typing import Optional, Union
from datetime import date, datetime

import pandas as pd

from base.db import read_sql

log = logging.getLogger(__name__)


def _normalize_date(d: Union[str, date, datetime, None]) -> Optional[str]:
    """将各种日期格式标准化为 YYYY-MM-DD 字符串。"""
    if d is None:
        return None
    if isinstance(d, datetime):
        return d.strftime("%Y-%m-%d")
    if isinstance(d, date):
        return d.strftime("%Y-%m-%d")
    if isinstance(d, str):
        # 支持 YYYYMMDD 和 YYYY-MM-DD 两种格式
        d = d.replace("-", "")
        if len(d) == 8:
            return f"{d[:4]}-{d[4:6]}-{d[6:8]}"
    raise ValueError(f"无法解析日期: {d}")


def query_st_status(as_of: Union[str, date, datetime]) -> pd.DataFrame:
    """查询 T 日的 ST 状态（point-in-time）。

    返回 as_of 当日所有 ST 股票列表。
    语义：effective_from ≤ as_of AND (effective_to IS NULL OR effective_to > as_of)

    Args:
        as_of: 查询日期（T 日）

    Returns:
        DataFrame: canonical_id, code, effective_from, effective_to
    """
    as_of_str = _normalize_date(as_of)
    log.info(f"查询 ST 状态 as_of={as_of_str}")

    df = read_sql("""
        SELECT canonical_id, symbol, effective_from, effective_to
        FROM public.st_status_pit
        WHERE effective_from <= %s
          AND (effective_to IS NULL OR effective_to > %s)
    """, params=(as_of_str, as_of_str))

    log.info(f"ST 股票数量: {len(df)}")
    return df


def query_financial_data(
    canonical_id: Optional[str] = None,
    as_of: Optional[Union[str, date, datetime]] = None,
) -> pd.DataFrame:
    """查询财报数据（point-in-time）。

    如果 as_of 不为空，只返回 pub_date ≤ as_of 的行 —— 零前视偏差。
    如果 canonical_id 不为空，只返回该股票的数据。

    Args:
        canonical_id: 证券代码（可选）
        as_of: 查询日期（可选）

    Returns:
        DataFrame: canonical_id, code, report_date, pub_date, revenue, net_profit, eps, ...
    """
    as_of_str = _normalize_date(as_of)

    conditions = []
    params = []

    if canonical_id:
        conditions.append("canonical_id = %s")
        params.append(canonical_id)

    if as_of_str:
        conditions.append("pub_date <= %s")
        params.append(as_of_str)

    where_clause = " AND ".join(conditions) if conditions else "1=1"

    query = f"""
        SELECT canonical_id, symbol, report_date, pub_date,
               revenue, operating_revenue,
               net_profit, net_profit_parent, eps
        FROM public.income_statement_pit
        WHERE {where_clause}
        ORDER BY canonical_id, report_date DESC
    """

    log.info(f"查询财报数据: canonical_id={canonical_id}, as_of={as_of_str}")
    df = read_sql(query, params=tuple(params) if params else None)
    log.info(f"返回 {len(df)} 行")

    return df


def query_adjustment_factors(
    canonical_id: Optional[str] = None,
    as_of: Optional[Union[str, date, datetime]] = None,
) -> pd.DataFrame:
    """查询复权因子（point-in-time）。

    复权因子由 corporate_action_event 中的 dividend/split/rights_issue 事件投影而来。
    只使用 ≤ as_of 的事件计算 —— 零前视偏差。

    PIT_UNPROVEN 标注：
      当前 corporate_action_event 的 pub_date 全部为 NULL，observed_at 为批量导入时刻。
      effective_date <= as_of 不足以证明历史 as-of 时点能够获得该事件数据。
      严格历史 PIT 无法证明，下游使用时应标记为 PIT_UNPROVEN。

    Args:
        canonical_id: 证券代码（可选）
        as_of: 查询日期（可选）

    Returns:
        DataFrame: canonical_id, effective_date, event_type, payload
    """
    as_of_str = _normalize_date(as_of)

    # Phase A 修复：扩投影到 rights_issue
    conditions = ["event_type IN ('dividend', 'split', 'rights_issue')"]
    params = []

    if canonical_id:
        conditions.append("canonical_id = %s")
        params.append(canonical_id)

    if as_of_str:
        conditions.append("effective_date <= %s")
        params.append(as_of_str)

    where_clause = " AND ".join(conditions)

    query = f"""
        SELECT canonical_id, effective_date, event_type, payload
        FROM public.corporate_action_event
        WHERE {where_clause}
        ORDER BY canonical_id, effective_date
    """

    log.info(f"查询复权因子: canonical_id={canonical_id}, as_of={as_of_str}")
    events = read_sql(query, params=tuple(params) if params else None)

    if events.empty:
        return pd.DataFrame()

    # 计算累计复权因子
    # dividend: cash_per_10 → 除息日股价调整为 (1 - cash_per_10/10/price)
    # split: convert_per_10 → 除权日股价调整为 1/(1 + convert_per_10/10)
    # rights_issue: rights_shares_per_10 + rights_price → 配股除权
    # 此处简化处理：返回事件列表，由下游根据 payload 计算具体复权因子
    log.info(f"返回 {len(events)} 条复权事件")
    return events


def check_no_lookahead_bias(
    table_name: str,
    as_of: Union[str, date, datetime],
    pub_date_col: str = "pub_date",
) -> bool:
    """验证零前视偏差：T 日查询结果集与 pub_date > T 的数据求交集应为空。

    Args:
        table_name: 要检查的表名（如 'public.income_statement_pit'）
        as_of: 查询日期
        pub_date_col: pub_date 列名

    Returns:
        True = 无前视偏差（交集为空）; False = 存在前视偏差
    """
    as_of_str = _normalize_date(as_of)

    # 检查是否有 pub_date > as_of 的数据被返回
    query = f"""
        SELECT COUNT(*) as violation_count
        FROM {table_name}
        WHERE {pub_date_col} > %s
    """

    result = read_sql(query, params=(as_of_str,))
    violation_count = result.iloc[0]["violation_count"] if not result.empty else 0

    if violation_count > 0:
        log.warning(f"前视偏差检测: {table_name} 在 as_of={as_of_str} 有 {violation_count} 行违规")
        return False
    else:
        log.info(f"前视偏差检测通过: {table_name} as_of={as_of_str}")
        return True


def as_of_join(
    left_df: pd.DataFrame,
    right_table: str,
    left_date_col: str,
    right_date_col: str,
    on_cols: Optional[list] = None,
) -> pd.DataFrame:
    """执行 as-of join：left_df 的每一行关联 right_table 中 ≤ left_date_col 的最新记录。

    这是 PIT 查询的常用模式：例如将交易日期与最新可用的财报数据关联。

    Args:
        left_df: 左表 DataFrame
        right_table: 右表名（SQL 表）
        left_date_col: 左表的日期列名
        right_date_col: 右表的日期列名（通常是 pub_date 或 effective_date）
        on_cols: 关联键（如 ['canonical_id']）

    Returns:
        合并后的 DataFrame
    """
    if left_df.empty:
        return left_df

    on_clause = " AND ".join(f"l.{c} = r.{c}" for c in on_cols) if on_cols else "TRUE"

    # 构建 as-of join 查询
    # 对 left_df 的每一行，找 right_table 中 ≤ left_date_col 的最新记录
    results = []

    for _, left_row in left_df.iterrows():
        left_date = _normalize_date(left_row[left_date_col])

        conditions = [f"{right_date_col} <= %s"]
        params = [left_date]

        if on_cols:
            for col in on_cols:
                conditions.append(f"{col} = %s")
                params.append(left_row[col])

        where_clause = " AND ".join(conditions)
        query = f"""
            SELECT *
            FROM {right_table}
            WHERE {where_clause}
            ORDER BY {right_date_col} DESC
            LIMIT 1
        """

        right_row = read_sql(query, params=tuple(params))
        if not right_row.empty:
            merged = {**left_row.to_dict(), **right_row.iloc[0].to_dict()}
            results.append(merged)

    return pd.DataFrame(results) if results else pd.DataFrame()
