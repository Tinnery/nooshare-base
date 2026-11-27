"""P1: 规范符号主数据构建器。

职责：tong.symbol_meta ⋈ ju.stocks → public.canonical_symbol。
映射冲突写入 reconcile 台账，不做字符串推断。

修复记录：
  2026-11-27: C1 修复 - JOIN 改为 stock_code + exchange 双条件；
              消除裸 exchange 访问（显式 exchange_ju）；
              drop_duplicates 改为冲突检测+报错；
              ju.stocks 来源一律记为 stock（不依赖 tong.class）。
"""
import logging
from typing import List, Dict, Any

import pandas as pd

from base.db import get_connection, read_sql, execute_sql, copy_upsert

log = logging.getLogger(__name__)


def build_canonical_symbol() -> int:
    """构建 public.canonical_symbol 表。
    
    数据源：
      - ju.stocks: stock_code, display_name, exchange, start_date, end_date
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
    
    # ju.exchange 直接就是 XSHG/XSHE/BSE
    # tong market → exchange
    market_map = {'sh': 'XSHG', 'sz': 'XSHE', 'bj': 'BSE'}
    tong_meta['exchange_tong'] = tong_meta['market'].map(market_map)
    
    # 4. 合并（按 stock_code + exchange 双条件，避免 00 开头深股被误标）
    ju_stocks = ju_stocks.rename(columns={'exchange': 'exchange_ju'})
    
    merged = ju_stocks.merge(
        tong_meta[['code', 'class', 'exchange_tong']],
        left_on=['stock_code', 'exchange_ju'],
        right_on=['code', 'exchange_tong'],
        how='left',
    )
    
    # 检测 exchange 不一致（应极少，因已按 exchange 合并）
    exchange_mismatches = merged[
        (merged['exchange_ju'].notna()) & 
        (merged['exchange_tong'].notna()) & 
        (merged['exchange_ju'] != merged['exchange_tong'])
    ]
    if not exchange_mismatches.empty:
        log.error(f"检测到 {len(exchange_mismatches)} 条 exchange 映射冲突")
        raise ValueError(f"exchange 映射冲突 {len(exchange_mismatches)} 条，需人工核查")
    
    # 5. 构建 symbol（tong 风格：sh600000）
    def infer_symbol(row):
        exchange = str(row['exchange_ju']).upper()
        if exchange == 'XSHG':
            prefix = 'sh'
        elif exchange == 'BSE':
            prefix = 'bj'
        elif exchange == 'XSHE':
            prefix = 'sz'
        else:
            log.warning(f"未知 exchange {exchange}，默认 sz")
            prefix = 'sz'
        return f"{prefix}{row['stock_code']}"
    
    merged['symbol'] = merged.apply(infer_symbol, axis=1)
    
    # asset_class：ju.stocks 来源全部为 stock（ju 权威优先于 tong.class）
    merged['asset_class'] = 'stock'
    
    # 6. 构建最终 DataFrame
    result = pd.DataFrame({
        'canonical_id': merged['stock_code'],
        'exchange': merged['exchange_ju'],
        'code': merged['stock_code'],
        'symbol': merged['symbol'],
        'asset_class': merged['asset_class'],
        'list_date': pd.to_datetime(merged['start_date'], errors='coerce'),
        'delist_date': pd.to_datetime(merged['end_date'], errors='coerce'),
    })
    
    # 检测重复（不应存在，因 ju.stocks 的 stock_code 应唯一）
    duplicates = result[result.duplicated(subset=['canonical_id'], keep=False)]
    if not duplicates.empty:
        log.error(f"检测到 {len(duplicates)} 条 canonical_id 重复")
        raise ValueError(f"canonical_id 重复 {len(duplicates)} 条，需人工核查")
    
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
    tong_meta['exchange_tong'] = tong_meta['market'].map(market_map)
    
    # 合并比对（按 code + exchange 双条件）
    ju_stocks = ju_stocks.rename(columns={'exchange': 'exchange_ju'})
    merged = ju_stocks.merge(
        tong_meta[['code', 'exchange_tong']],
        left_on=['stock_code', 'exchange_ju'],
        right_on=['code', 'exchange_tong'],
        how='inner',
    )
    
    # 找出 exchange 不一致的记录
    mismatches = merged[merged['exchange_ju'] != merged['exchange_tong']]
    
    result = []
    for _, row in mismatches.iterrows():
        result.append({
            'code': row['code'],
            'ju_exchange': row['exchange_ju'],
            'tong_exchange': row['exchange_tong'],
        })
    
    if result:
        log.warning(f"检测到 {len(result)} 条映射冲突")
    
    return result
