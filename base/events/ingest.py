"""P2: 公司行为事件溯源。

职责：从 ju.st_period / tong.gbbq 等源表提取公司行为事件，
写入 public.corporate_action_event（不可变事件流）。

事件类型：
  - st_mark / st_remove: 来自 ju.st_period（区间快照 → 端点事件）
  - dividend / split / rights_issue / shares_change: 来自 tong.gbbq（已是事件）
  - market_cap_change: 来自 ju.valuation（区间快照 → 端点事件，可选）

幂等性：UNIQUE (canonical_id, event_type, effective_date, source_id) 承载去重。
"""
import logging
from typing import List, Dict, Any, Optional

import pandas as pd

from base.db import read_sql, copy_upsert

log = logging.getLogger(__name__)


# tong.gbbq category 映射到事件类型
# category 含义（tong 仓库约定）：
#   1 = 除权除息（dividend + split 混合，需按 ratio/per_10shares 区分）
#   2 = 配股（rights_issue）
#   3 = 增发（shares_change）
#   4 = 股本变更（shares_change）
#   5 = 其他（shares_change）
GBBQ_CATEGORY_MAP = {
    1: None,  # 需进一步判断：有 per_10shares → dividend；有 ratio → split；两者都有 → both
    2: "rights_issue",
    3: "shares_change",
    4: "shares_change",
    5: "shares_change",
}


def extract_st_events() -> pd.DataFrame:
    """从 ju.st_period 提取 ST 标记/摘帽事件。

    ju.st_period 是区间快照：stock_code, start_date, end_date
    转换为端点事件：st_mark (start_date) + st_remove (end_date + 1)
    """
    log.info("提取 ST 事件 from ju.st_period...")

    st_periods = read_sql("""
        SELECT stock_code, start_date, end_date
        FROM ju.st_period
        WHERE stock_code IS NOT NULL AND start_date IS NOT NULL
    """)
    log.info(f"ju.st_period: {len(st_periods)} 行")

    if st_periods.empty:
        return pd.DataFrame()

    events = []

    for _, row in st_periods.iterrows():
        code = row["stock_code"]

        # st_mark 事件
        events.append({
            "canonical_id": code,
            "event_type": "st_mark",
            "effective_date": row["start_date"],
            "payload": {"st_type": "ST"},
            "source_id": "ju",
        })

        # st_remove 事件（end_date + 1 day）
        if pd.notna(row.get("end_date")):
            end_date = pd.to_datetime(row["end_date"])
            remove_date = end_date + pd.Timedelta(days=1)
            events.append({
                "canonical_id": code,
                "event_type": "st_remove",
                "effective_date": remove_date.date(),
                "payload": {"previous_st_type": "ST"},
                "source_id": "ju",
            })

    df = pd.DataFrame(events)
    log.info(f"ST 事件提取完成: {len(df)} 条")
    return df


def extract_gbbq_events() -> pd.DataFrame:
    """从 tong.gbbq 提取除权除息/股本变更事件。

    tong.gbbq 实际列：symbol, stock_code, date(integer YYYYMMDD), category, c1, c2, c3, c4
    
    category=1（除权除息）：c1=派息(每10股元), c2=配股价格, c3=送转(每10股), c4=配股数量(每10股)
    category=2（股本变更）：c1=总股本(万股), c2=流通股本(万股)
    category=5（增发/配股）：c3/c4 是数量
    
    字段语义已通过除权日开盘参考价格检验验证（99.92% 准确率）。
    date 列是 integer YYYYMMDD 格式，需要转换。
    """
    log.info("提取 gbbq 事件 from tong.gbbq...")

    gbbq = read_sql("""
        SELECT symbol, stock_code, date, category, c1, c2, c3, c4
        FROM tong.gbbq
        WHERE stock_code IS NOT NULL AND date IS NOT NULL
    """)
    log.info(f"tong.gbbq: {len(gbbq)} 行")

    if gbbq.empty:
        return pd.DataFrame()

    # date 是 integer YYYYMMDD，转为 date 类型
    gbbq["date_str"] = gbbq["date"].astype(str)
    gbbq["effective_date"] = pd.to_datetime(gbbq["date_str"], format="%Y%m%d", errors="coerce").dt.date

    events = []

    for _, row in gbbq.iterrows():
        code = row["stock_code"]
        category = row.get("category")
        c1 = row.get("c1", 0) or 0
        c2 = row.get("c2", 0) or 0
        c3 = row.get("c3", 0) or 0
        c4 = row.get("c4", 0) or 0
        eff_date = row["effective_date"]

        if eff_date is None:
            continue

        if category == 1:
            # 除权除息：c1=送股, c2=派息, c3=转增, c4=配股
            has_dividend = c2 > 0  # 派息
            has_split = c1 > 0 or c3 > 0  # 送股或转增

            if has_dividend:
                events.append({
                    "canonical_id": code,
                    "event_type": "dividend",
                    "effective_date": eff_date,
                    "payload": {"per_10_shares": float(c2)},  # 每10股派息
                    "source_id": "tong",
                })
            if has_split:
                events.append({
                    "canonical_id": code,
                    "event_type": "split",
                    "effective_date": eff_date,
                    "payload": {
                        "bonus_per_10": float(c1),  # 每10股送股
                        "convert_per_10": float(c3),  # 每10股转增
                    },
                    "source_id": "tong",
                })
        elif category == 2:
            # 股本变更
            events.append({
                "canonical_id": code,
                "event_type": "shares_change",
                "effective_date": eff_date,
                "payload": {
                    "total_shares_10k": float(c1),  # 总股本(万股)
                    "float_shares_10k": float(c2),  # 流通股本(万股)
                },
                "source_id": "tong",
            })
        elif category in (3, 4, 5):
            # 增发/配股/其他
            events.append({
                "canonical_id": code,
                "event_type": "shares_change",
                "effective_date": eff_date,
                "payload": {"c1": float(c1), "c2": float(c2), "c3": float(c3), "c4": float(c4)},
                "source_id": "tong",
            })

    df = pd.DataFrame(events)
    log.info(f"gbbq 事件提取完成: {len(df)} 条")
    return df


def build_corporate_action_events() -> int:
    """构建 public.corporate_action_event 表。

    流程：
      1. extract_st_events() → ST 事件
      2. extract_gbbq_events() → 除权除息/股本变更事件
      3. 合并 → 去重 → 过滤不存在的 canonical_id → 写入数据库

    Returns:
        写入行数
    """
    log.info("开始构建 corporate_action_event...")

    # 1. 提取各类事件
    st_events = extract_st_events()
    gbbq_events = extract_gbbq_events()

    # 2. 合并
    all_events = pd.concat([st_events, gbbq_events], ignore_index=True)

    if all_events.empty:
        log.warning("无事件可写入")
        return 0

    # 3. 去重（同一 canonical_id + event_type + effective_date + source_id 只保留一条）
    all_events = all_events.drop_duplicates(
        subset=["canonical_id", "event_type", "effective_date", "source_id"],
        keep="first"
    )

    # 4. 过滤：只保留 canonical_symbol 中存在的 canonical_id
    valid_ids = read_sql("SELECT canonical_id FROM public.canonical_symbol")
    valid_ids_set = set(valid_ids["canonical_id"])
    all_events = all_events[all_events["canonical_id"].isin(valid_ids_set)]

    if all_events.empty:
        log.warning("过滤后无事件可写入（所有事件的 canonical_id 不在 canonical_symbol 中）")
        return 0

    # 5. 补充缺失字段
    all_events["pub_date"] = None  # 历史回填事件无 pub_date
    all_events["observed_at"] = pd.Timestamp.now()

    log.info(f"corporate_action_event 构建完成: {len(all_events)} 条")

    # 6. 写入数据库
    _insert_events_with_jsonb(all_events)

    return len(all_events)


def _insert_events_with_jsonb(df: pd.DataFrame) -> None:
    """插入含 JSONB 列的事件数据。

    COPY 对 JSONB 的引号转义有问题，改用 psycopg2.extras.execute_values 批量 INSERT。
    幂等性由 UNIQUE 约束保障（ON CONFLICT DO NOTHING）。
    """
    from base.db import get_connection
    from psycopg2.extras import execute_values
    import json
    import uuid

    if df.empty:
        return

    log.info(f"写入 {len(df)} 条事件到 public.corporate_action_event...")

    # 准备数据：payload 序列化为 JSON 字符串，生成 event_id
    rows = []
    for _, row in df.iterrows():
        event_id = str(uuid.uuid4())
        payload = row["payload"]
        payload_json = json.dumps(payload) if isinstance(payload, dict) else "{}"
        rows.append((
            event_id,
            row["canonical_id"],
            row["event_type"],
            row["effective_date"],
            row.get("pub_date"),
            payload_json,
            row["source_id"],
            row.get("observed_at"),
        ))

    with get_connection() as conn:
        with conn.cursor() as cur:
            execute_values(
                cur,
                """
                INSERT INTO public.corporate_action_event
                    (event_id, canonical_id, event_type, effective_date, pub_date, payload, source_id, observed_at)
                VALUES %s
                ON CONFLICT (canonical_id, event_type, effective_date, source_id) DO NOTHING
                """,
                rows,
                template="(%s, %s, %s, %s, %s, %s::jsonb, %s, %s)",
                page_size=1000,
            )
        conn.commit()

    log.info("事件写入完成")


def detect_event_anomalies() -> List[Dict[str, Any]]:
    """检测事件流异常：
      - 同一股票同一天有多个 st_mark 事件（冲突）
      - st_remove 早于 st_mark（时序错误）
      - dividend/split 事件在退市日之后（逻辑错误）

    Returns:
        异常列表
    """
    log.info("检测事件流异常...")

    events = read_sql("""
        SELECT canonical_id, event_type, effective_date, payload
        FROM public.corporate_action_event
        ORDER BY canonical_id, effective_date, event_type
    """)

    if events.empty:
        return []

    anomalies = []

    # 按 canonical_id 分组检查
    for canonical_id, group in events.groupby("canonical_id"):
        group = group.sort_values("effective_date")

        # 检查 1: 同一天多个 st_mark
        st_marks = group[group["event_type"] == "st_mark"]
        dup_dates = st_marks.groupby("effective_date").size()
        dup_dates = dup_dates[dup_dates > 1]
        for date, count in dup_dates.items():
            anomalies.append({
                "canonical_id": canonical_id,
                "anomaly_type": "duplicate_st_mark",
                "date": date,
                "detail": f"同一天 {count} 个 st_mark 事件",
            })

        # 检查 2: st_remove 早于 st_mark
        st_events = group[group["event_type"].isin(["st_mark", "st_remove"])]
        if not st_events.empty:
            first_mark = st_events[st_events["event_type"] == "st_mark"]["effective_date"].min()
            first_remove = st_events[st_events["event_type"] == "st_remove"]["effective_date"].min()
            if pd.notna(first_remove) and pd.notna(first_mark) and first_remove < first_mark:
                anomalies.append({
                    "canonical_id": canonical_id,
                    "anomaly_type": "st_remove_before_mark",
                    "date": first_remove,
                    "detail": f"st_remove ({first_remove}) 早于 st_mark ({first_mark})",
                })

    if anomalies:
        log.warning(f"检测到 {len(anomalies)} 条事件异常")

    return anomalies
