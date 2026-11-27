"""P1: 规范符号主数据构建器。

职责：以 tong.symbol_meta 为唯一身份权威，ju 表补日期 → public.canonical_symbol。

身份语义（2026-11-25 裁决）：完整身份 = (exchange, canonical_id)；
canonical_id = 6 位纯数字本地代码；symbol = exchange+canonical_id（tong 风格）。

方案 A（2026-11-27 用户裁决）：覆盖全部身份（含退市无行情证券），
解决 202 只 00 开头深股误标与 200 个跨类同码碰撞（复合 PK 前提）。

修复记录：
  2026-11-27 C1: JOIN 改双条件，消除裸 exchange，冲突检测报错。
  2026-11-27 A:  身份权威改 tong.symbol_meta，覆盖全资产。
"""
import logging
from typing import List, Dict, Any

import pandas as pd

from base.db import get_connection, read_sql, execute_sql, copy_upsert

log = logging.getLogger(__name__)

# tong.symbol_meta.class -> canonical asset_class
# 实测：block=sh880xxx 板块指数归 index；bstock=sh900xxx B股归 stock
CLASS_MAP = {
    'stock': 'stock',
    'bstock': 'stock',
    'index': 'index',
    'block': 'index',
    'etf': 'etf',
}
# 日 K 三源作为身份覆盖校验子集
DAILY_SOURCES = ('tong.stock_daily', 'tong.index_daily', 'tong.etf_daily')


def build_canonical_symbol() -> int:
    """构建 public.canonical_symbol（全资产覆盖）。

    身份权威：tong.symbol_meta 全集（含退市无行情证券，如 sz000787）。
    日期补充：ju.stocks / ju.indices / ju.funds（按 canonical_id + asset_class 匹配）。
    日 K 全集作为校验子集，必须 100% 被身份表覆盖。

    Returns:
        写入行数
    """
    log.info("开始构建 canonical_symbol (scheme A)...")

    # 1. 身份源：tong.symbol_meta，解析 exchange/code
    meta = read_sql("""
        SELECT symbol, class
        FROM tong.symbol_meta
        WHERE symbol IS NOT NULL AND length(symbol) > 2
    """)
    meta = meta[meta['class'].isin(CLASS_MAP.keys())].copy()
    meta['exchange'] = meta['symbol'].str[:2]
    meta['canonical_id'] = meta['symbol'].str[2:]
    meta['asset_class'] = meta['class'].map(CLASS_MAP)
    log.info(f"tong.symbol_meta 可用身份: {len(meta)} 行")

    # 2. 日 K 全集作为校验子集：身份表覆盖 symbol_meta 全集（含退市证券），
    #    但每个日 K symbol 必须能被解析，否则是权威缺口。
    daily_parts = [read_sql(f"SELECT DISTINCT symbol FROM {src}") for src in DAILY_SOURCES]
    daily = pd.concat(daily_parts, ignore_index=True).drop_duplicates('symbol')
    uncovered = daily[~daily['symbol'].isin(meta['symbol'])]
    if not uncovered.empty:
        raise ValueError(f"日 K 有 {len(uncovered)} symbol 未被 symbol_meta 覆盖，需人工核查")
    log.info(f"身份全集 {len(meta)} 行，日 K 校验 {len(daily)} 行全覆盖")

    # 3. 碰撞检测：同一 (exchange, canonical_id) 必须唯一
    dup = meta[meta.duplicated(subset=['exchange', 'canonical_id'], keep=False)]
    if not dup.empty:
        raise ValueError(f"身份重复 (exchange,canonical_id) {len(dup)} 条，需人工核查")

    # 4. ju 日期补充（按 canonical_id + asset_class 匹配，ju 自身不含 exchange）
    ju = read_sql("""
        SELECT stock_code AS canonical_id, 'stock' AS asset_class, start_date, end_date
        FROM ju.stocks
        UNION ALL
        SELECT index_code, 'index', start_date, end_date FROM ju.indices
        UNION ALL
        SELECT fund_code, 'etf', start_date, end_date FROM ju.funds WHERE type = 'etf'
    """)
    ju = ju.drop_duplicates(subset=['canonical_id', 'asset_class'])
    meta = meta.merge(
        ju[['canonical_id', 'asset_class', 'start_date', 'end_date']],
        on=['canonical_id', 'asset_class'],
        how='left',
    )

    # 5. 构建最终 DataFrame
    result = pd.DataFrame({
        'canonical_id': meta['canonical_id'],
        'exchange': meta['exchange'],
        'code': meta['canonical_id'],
        'symbol': meta['symbol'],
        'asset_class': meta['asset_class'],
        'list_date': pd.to_datetime(meta.get('start_date'), errors='coerce'),
        'delist_date': pd.to_datetime(meta.get('end_date'), errors='coerce'),
    })
    log.info(f"canonical_symbol 构建完成: {len(result)} 行")

    copy_upsert(result, 'public', 'canonical_symbol')
    return len(result)


def detect_symbol_mismatches() -> List[Dict[str, Any]]:
    """检测 ju 表同一 code 跨资产类别重复（只读）。"""
    ju = read_sql("""
        SELECT stock_code AS code, 'stock' AS cls FROM ju.stocks
        UNION ALL SELECT index_code, 'index' FROM ju.indices
        UNION ALL SELECT fund_code, 'etf' FROM ju.funds WHERE type = 'etf'
    """)
    dup = ju[ju.duplicated(subset=['code'], keep=False)]
    return [
        {'code': r['code'], 'classes': dup[dup['code'] == r['code']]['cls'].tolist()}
        for _, r in dup.iterrows()
    ]
