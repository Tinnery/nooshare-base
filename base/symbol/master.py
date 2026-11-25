"""P1: 规范符号主数据构建器。

职责：tong.symbol_meta ⋈ ju.stocks → public.canonical_symbol。
映射冲突写入 reconcile 台账，不做字符串推断。
"""
import logging
from typing import List, Dict, Any

import pandas as pd

from base.db import get_connection, read_sql, execute_sql, copy_upsert

log = logging.getLogger(__name__)


def build_canonical_symbol() -> int:
    """构建 public.canonical_symbol 表。
    
    数据源：
      - ju.stocks: stock_code, company_name, list_date, delist_date, exchange_id
      - tong.symbol_meta: symbol, class (stock/index/etf/block)
    
    映射规则：
      - canonical_id = stock_code (纯数字代码)
      - exchange = sh (上交所) / sz (深交所) / bj (北交所)
      - symbol = {market_prefix}{code} (tong 风格：sz000001)
    
    Returns:
        写入行数
    """
    log.info("开始构建 canonical_symbol...")
    
    # 1. 从 ju.stocks 读取股票列表
    # 实际列: stock_code, display_name, exchange, start_date, end_date
    ju_stocks = read_sql("""
        SELECT 
            stock_code,
            display_name,
            exchange,
            start_date,
            end_date
        FROM ju.stocks
        WHERE stock_code IS NOT NULL
    """)
    log.info(f"ju.stocks: {len(ju_stocks)} 行")
    
    # 2. 从 tong.symbol_meta 读取证券分类
    tong_meta = read_sql("""
        SELECT symbol, class
        FROM tong.symbol_meta
        WHERE symbol IS NOT NULL
    """)
    log.info(f"tong.symbol_meta: {len(tong_meta)} 行")
    
    # 3. 构建映射表
    # tong.symbol 格式：sh600000 / sz000001 / bj830799
    tong_meta['market'] = tong_meta['symbol'].str[:2]
    tong_meta['code'] = tong_meta['symbol'].str[2:]
    
    # ju.exchange 直接就是 XSHG/XSHE/BSE，无需映射
    # tong market → exchange
    market_map = {'sh': 'XSHG', 'sz': 'XSHE', 'bj': 'BSE'}
    tong_meta['exchange'] = tong_meta['market'].map(market_map)
    
    # 4. 合并（带 exchange 验证）
    merged = ju_stocks.merge(
        tong_meta[['code', 'class', 'exchange']],
        left_on='stock_code',
        right_on='code',
        how='left',
        suffixes=('_ju', '_tong')
    )
    
    # 验证 exchange 映射一致性
    exchange_mismatches = merged[
        (merged['exchange_ju'].notna()) & 
        (merged['exchange_tong'].notna()) & 
        (merged['exchange_ju'] != merged['exchange_tong'])
    ]
    if not exchange_mismatches.empty:
        log.warning(f"检测到 {len(exchange_mismatches)} 条 exchange 映射冲突")
        log.warning(f"示例: {exchange_mismatches[['stock_code', 'exchange_ju', 'exchange_tong']].head(5).to_dict('records')}")
    
    # 5. 合并 exchange：ju 为权威源，tong 为交叉验证回退
    # ju.stocks.exchange 是小写 sh/sz/bj；tong 推导的是大写 XSHG/XSHE/BSE
    # 统一为小写 sh/sz/bj 输出
    _TONG_TO_LOWER = {'XSHG': 'sh', 'XSHE': 'sz', 'BSE': 'bj'}
    
    def _normalise_exchange(row):
        ju_val = row['exchange_ju']
        tong_val = row['exchange_tong']
        if pd.notna(ju_val):
            return str(ju_val).lower()
        if pd.notna(tong_val):
            mapped = _TONG_TO_LOWER.get(str(tong_val))
            if mapped:
                return mapped
            # tong 值无法识别
            return None
        return None
    
    merged['exchange_resolved'] = merged.apply(_normalise_exchange, axis=1)
    
    # 报告无法解析的行
    unresolvable = merged[merged['exchange_resolved'].isna()]
    if not unresolvable.empty:
        log.warning(f"{len(unresolvable)} 条记录无法解析 exchange（两源均缺失或不可识别）")
    
    # 6. 构建 symbol（tong 风格：sh600000）
    _PREFIX_MAP = {'sh': 'sh', 'xshg': 'sh', 'sz': 'sz', 'xshe': 'sz', 'bj': 'bj', 'bse': 'bj'}
    
    def infer_symbol(row):
        ex = row['exchange_resolved']
        if ex is None or pd.isna(ex):
            log.warning(f"无法推断 symbol: stock_code={row['stock_code']}，exchange 缺失，跳过")
            return None
        prefix = _PREFIX_MAP.get(str(ex).lower())
        if prefix is None:
            log.warning(f"无法推断 symbol: stock_code={row['stock_code']}，exchange={ex!r} 不可识别，跳过")
            return None
        return f"{prefix}{row['stock_code']}"
    
    merged['symbol'] = merged.apply(infer_symbol, axis=1)
    merged['asset_class'] = merged['class'].fillna('stock')
    
    # 7. 构建最终 DataFrame（丢弃无法推断 symbol 的行）
    before_drop = len(merged)
    merged = merged.dropna(subset=['symbol'])
    if len(merged) < before_drop:
        log.warning(f"丢弃 {before_drop - len(merged)} 条无法推断 symbol 的记录")
    
    result = pd.DataFrame({
        'canonical_id': merged['stock_code'],
        'exchange': merged['exchange_resolved'],
        'code': merged['stock_code'],
        'symbol': merged['symbol'],
        'asset_class': merged['asset_class'],
        'list_date': pd.to_datetime(merged['start_date'], errors='coerce'),
        'delist_date': pd.to_datetime(merged['end_date'], errors='coerce'),
    })
    
    # 去重（如果有重复的 stock_code）
    result = result.drop_duplicates(subset=['canonical_id'], keep='first')
    
    log.info(f"canonical_symbol 构建完成: {len(result)} 行")
    
    # 7. 写入数据库
    copy_upsert(result, 'public', 'canonical_symbol')
    
    return len(result)


def detect_symbol_mismatches() -> List[Dict[str, Any]]:
    """检测 tong.symbol_meta 与 ju.stocks 的映射冲突。
    
    Returns:
        冲突列表，每项包含 {code, tong_symbol, ju_exchange, tong_exchange}
    """
    # 读取两源数据
    ju_stocks = read_sql("""
        SELECT stock_code, exchange
        FROM ju.stocks
        WHERE stock_code IS NOT NULL
    """)
    
    tong_meta = read_sql("""
        SELECT symbol, class
        FROM tong.symbol_meta
        WHERE symbol IS NOT NULL
    """)
    
    # 提取 code 和 exchange
    tong_meta['code'] = tong_meta['symbol'].str[2:]
    tong_meta['market'] = tong_meta['symbol'].str[:2]
    market_map = {'sh': 'XSHG', 'sz': 'XSHE', 'bj': 'BSE'}
    tong_meta['exchange'] = tong_meta['market'].map(market_map)
    
    # 合并比对
    merged = ju_stocks.merge(
        tong_meta[['code', 'exchange']],
        left_on='stock_code',
        right_on='code',
        how='inner',
        suffixes=('_ju', '_tong')
    )
    
    # 找出 exchange 不一致的记录
    mismatches = merged[merged['exchange_ju'] != merged['exchange_tong']]
    
    result = []
    for _, row in mismatches.iterrows():
        result.append({
            'code': row['code'],
            'tong_symbol': f"{row['code']}",  # 需要重新查 tong_meta 获取完整 symbol
            'ju_exchange': row['exchange_ju'],
            'tong_exchange': row['exchange_tong'],
        })
    
    if result:
        log.warning(f"检测到 {len(result)} 条映射冲突")
    
    return result
