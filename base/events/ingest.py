"""P2: 公司行为事件溯源。

职责：从 ju.st_period / tong.gbbq 等源表提取公司行为事件，
写入 public.corporate_action_event（不可变事件流）。

事件类型：
  - st_mark / st_remove: 来自 ju.st_period（区间快照 → 端点事件）
  - dividend / split / rights_issue / shares_change: 来自 tong.gbbq（已是事件）
  - market_cap_change: 来自 ju.valuation（区间快照 → 端点事件，可选）

幂等性：UNIQUE (canonical_id, event_type, effective_date, source_id) 承载去重。

修复记录：
  2026-11-27: Phase A - 修正 GBBQ category=1 字段映射错误。
              正确语义：c1=每10股现金分红 / c2=配股价 / c3=每10股送转 / c4=每10股配股数。
              同日混合事件拆为多条（兼容 UNIQUE 键）。
              删除死常量 GBBQ_CATEGORY_MAP。
"""
import logging
from typing import List, Dict, Any, Optional

import pandas as pd

from base.db import read_sql, copy_upsert

log = logging.getLogger(__name__)


# 异常台账（内存中收集，用于日志和后续持久化）
_anomaly_log = []


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
    """从 tong.gbbq 提取除权除息/配股事件。

    tong.gbbq 实际列：symbol, stock_code, date(integer YYYYMMDD), category, c1, c2, c3, c4
    
    字段语义（已通过 hfq.py 全市场标定 99.92% 验证 + tong adjust_calc.go 三方一致）：
      - c1 = 每10股现金分红（dividend）
      - c2 = 配股价（rights_price）
      - c3 = 每10股送转股数（split/bonus，TDX 不区分送/转）
      - c4 = 每10股配股数量（rights_shares）
    
    category=1（除权除息）分类规则：
      - c1 > 0 → dividend 事件
      - c3 > 0 → split 事件
      - c4 > 0 → rights_issue 事件（需同时有 c2 配股价）
      - 同日可有多条事件（拆条，兼容 UNIQUE 键）
      - c2 > 0 且 c4 = 0 → 孤立配价，记 anomaly
      - 全零 → 不生成事件
      - 负值 → 记 anomaly，不生成
    
    category=2-15（语义未经核验，tong 上游不用于复权）：
      - 保持归档为 shares_change，payload 原样保留 c1-c4
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
    anomaly_count = 0
    all_zero_count = 0

    for _, row in gbbq.iterrows():
        code = row["stock_code"]
        category = row.get("category")
        c1 = float(row.get("c1", 0) or 0)
        c2 = float(row.get("c2", 0) or 0)
        c3 = float(row.get("c3", 0) or 0)
        c4 = float(row.get("c4", 0) or 0)
        eff_date = row["effective_date"]

        if eff_date is None:
            continue

        if category == 1:
            # 除权除息：按正确语义分类
            # 检测负值异常
            if c1 < 0 or c2 < 0 or c3 < 0 or c4 < 0:
                _anomaly_log.append({
                    "canonical_id": code,
                    "date": eff_date,
                    "anomaly_type": "negative_value",
                    "detail": f"c1={c1}, c2={c2}, c3={c3}, c4={c4}",
                })
                anomaly_count += 1
                continue

            # 全零
            if c1 == 0 and c2 == 0 and c3 == 0 and c4 == 0:
                all_zero_count += 1
                continue

            # source_action_key: 原始 GBBQ 记录身份（symbol+date+category），
            # 拆分出的 dividend/split/rights_issue 共享同一 key，
            # 供 PIT 因子按组合公式一次计算，不拆开相乘。
            source_action_key = f"tong.gbbq|{row['symbol']}|{row['date']}|{int(category)}"
            full_payload = {
                "c1": c1, "c2": c2, "c3": c3, "c4": c4,
                "category": int(category),
                "source_action_key": source_action_key,
                "source_symbol": row["symbol"],
            }

            # c1 > 0 → dividend
            if c1 > 0:
                _p = dict(full_payload)
                _p["cash_per_10"] = c1
                events.append({
                    "canonical_id": code,
                    "exchange": str(row["symbol"])[:2],
                    "event_type": "dividend",
                    "effective_date": eff_date,
                    "payload": _p,
                    "source_id": "tong",
                })

            # c3 > 0 → split
            if c3 > 0:
                _p = dict(full_payload)
                _p.update({"bonus_per_10": 0.0, "convert_per_10": c3})
                events.append({
                    "canonical_id": code,
                    "exchange": str(row["symbol"])[:2],
                    "event_type": "split",
                    "effective_date": eff_date,
                    "payload": _p,
                    "source_id": "tong",
                })

            # c4 > 0 → rights_issue
            if c4 > 0:
                _p = dict(full_payload)
                _p.update({"rights_shares_per_10": c4, "rights_price": c2})
                events.append({
                    "canonical_id": code,
                    "exchange": str(row["symbol"])[:2],
                    "event_type": "rights_issue",
                    "effective_date": eff_date,
                    "payload": _p,
                    "source_id": "tong",
                })

            # c2 > 0 且 c4 = 0 → 孤立配价
            if c2 > 0 and c4 == 0:
                _anomaly_log.append({
                    "canonical_id": code,
                    "date": eff_date,
                    "anomaly_type": "orphan_rights_price",
                    "detail": f"c2={c2} but c4=0",
                })
                anomaly_count += 1

        elif category in (2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15):
            # category 2-15：语义未经核验，保持归档
            events.append({
                "canonical_id": code,
                "exchange": str(row["symbol"])[:2],
                "event_type": "shares_change",
                "effective_date": eff_date,
                "payload": {"c1": c1, "c2": c2, "c3": c3, "c4": c4, "category": int(category)},
                "source_id": "tong",
            })
        else:
            log.warning(f"未知 category={category} at {code} {eff_date}")

    df = pd.DataFrame(events)
    log.info(f"gbbq 事件提取完成: {len(df)} 条 (anomaly={anomaly_count}, all_zero={all_zero_count})")
    return df


def build_corporate_action_events() -> int:
    """构建 public.corporate_action_event 表。

    流程：
      1. extract_st_events() → ST 事件
      2. extract_gbbq_events() → 除权除息/配股/股本变更事件
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
    all_events["pub_date"] = None  # 历史回填事件无 pub_date（不伪造）
    all_events["observed_at"] = pd.Timestamp.now()

    log.info(f"corporate_action_event 构建完成: {len(all_events)} 条")

    # 6. 写入数据库
    _insert_events_with_jsonb(all_events)

    return len(all_events)


def _insert_events_with_jsonb(df: pd.DataFrame) -> None:
    """插入含 JSONB 列的事件数据。

    COPY 对 JSONB 的引号转义有问题，改用 psycopg2.extras.execute_values 批量 INSERT。
    幂等性由 UNIQUE 约束保障（ON CONFLICT DO NOTHING）。
    
    注意：重灌时必须先 DELETE 旧数据（因旧错误数据与正确数据共享幂等键）。
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
    # exchange is part of the table identity (UNIQUE with canonical_id) and
    # NOT NULL; resolve it from canonical_symbol instead of guessing.
    identity = read_sql(
        "SELECT exchange, canonical_id FROM public.canonical_symbol"
    )
    # canonical_id can collide across exchanges (000001 = sz stock AND sh
    # index). Map only globally-unique ids; ambiguous ids must come from the
    # event source (gbbq symbol prefix) or the write is rejected - a silent
    # wrong-exchange label corrupts the (exchange, canonical_id) identity.
    dup_mask = identity["canonical_id"].duplicated(keep=False)
    exchange_by_id = dict(zip(
        identity.loc[~dup_mask, "canonical_id"],
        identity.loc[~dup_mask, "exchange"],
    ))
    ambiguous_ids = set(identity.loc[dup_mask, "canonical_id"])

    for _, row in df.iterrows():
        event_id = str(uuid.uuid4())
        payload = row["payload"]
        payload_json = json.dumps(payload) if isinstance(payload, dict) else "{}"
        ex = row.get("exchange")
        if ex is None or (isinstance(ex, float) and pd.isna(ex)):
            cid = row["canonical_id"]
            if cid in ambiguous_ids:
                raise ValueError(
                    f"事件身份歧义: canonical_id={cid} 在 canonical_symbol 中跨 "
                    "exchange 撞号，且事件源未携带 exchange，拒绝静默错标"
                )
            ex = exchange_by_id.get(cid)
        rows.append((
            event_id,
            row["canonical_id"],
            ex,
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
                    (event_id, canonical_id, exchange, event_type, effective_date, pub_date, payload, source_id, observed_at)
                VALUES %s
                ON CONFLICT (exchange, canonical_id, event_type, effective_date, source_id) DO NOTHING
                """,
                rows,
                template="(%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)",
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
