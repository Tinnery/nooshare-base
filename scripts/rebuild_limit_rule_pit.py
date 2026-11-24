#!/usr/bin/env python3
"""
重建 limit_rule_pit 表，消除冗余数据

策略：
1. 清空现有 limit_rule_pit
2. 按 (security_type, exchange) 聚合，找出规则变化点
3. 每个变化点插入一行，而不是每个交易日都插入
"""

import psycopg2

DB_CONFIG = {
    "host": "localhost",
    "port": 5432,
    "dbname": "nooshare_dev",
    "user": "travis",
}


def rebuild_limit_rule_pit(conn):
    """重建涨跌停规则 PIT 表"""
    with conn.cursor() as cur:
        # 1. 清空现有数据
        cur.execute("TRUNCATE TABLE public.limit_rule_pit RESTART IDENTITY")
        print("✓ Cleared existing limit_rule_pit")
        
        # 2. 重新填充：按 (security_type, exchange) 聚合，检测规则变化点
        cur.execute("""
            WITH stock_type_mapping AS (
                SELECT 
                    stock_code,
                    CASE 
                        WHEN LEFT(stock_code, 3) IN ('688', '689') THEN 'star_market'
                        WHEN LEFT(stock_code, 1) = '6' THEN 'main_board'
                        WHEN LEFT(stock_code, 3) IN ('300', '301') THEN 'chi_next'
                        WHEN LEFT(stock_code, 1) = '0' THEN 'main_board'
                        WHEN LEFT(stock_code, 1) IN ('8', '4') THEN 'bse'
                        ELSE 'unknown'
                    END AS security_type,
                    CASE 
                        WHEN LEFT(stock_code, 1) IN ('6', '8') THEN 'sh'
                        WHEN LEFT(stock_code, 1) IN ('0', '3') THEN 'sz'
                        WHEN LEFT(stock_code, 1) IN ('8', '4') THEN 'bj'
                        ELSE 'unknown'
                    END AS exchange,
                    start_date,
                    limit_ratio
                FROM ju.limit_ratio
            ),
            -- 按 (security_type, exchange) 分组，取最常见的 limit_ratio
            dominant_rules AS (
                SELECT 
                    security_type,
                    exchange,
                    MODE() WITHIN GROUP (ORDER BY limit_ratio) AS final_ratio
                FROM stock_type_mapping
                WHERE security_type != 'unknown'
                GROUP BY security_type, exchange
            ),
            -- 检测规则变化点：找到每个 (security_type, exchange, limit_ratio) 组合的最早 start_date
            rule_changes AS (
                SELECT DISTINCT
                    stm.security_type,
                    stm.exchange,
                    stm.limit_ratio,
                    MIN(stm.start_date) AS effective_from
                FROM stock_type_mapping stm
                INNER JOIN dominant_rules dr 
                    ON stm.security_type = dr.security_type 
                    AND stm.exchange = dr.exchange
                    AND stm.limit_ratio = dr.final_ratio
                WHERE stm.security_type != 'unknown'
                GROUP BY stm.security_type, stm.exchange, stm.limit_ratio
            )
            INSERT INTO public.limit_rule_pit (security_type, exchange, limit_ratio, effective_from, source)
            SELECT 
                security_type,
                exchange,
                limit_ratio / 100.0,
                effective_from,
                'ju.limit_ratio_v2'
            FROM rule_changes
        """)
        
        conn.commit()
        print(f"✓ limit_rule_pit rebuilt: {cur.rowcount} rows inserted")


def main():
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        print("Rebuilding limit_rule_pit...")
        rebuild_limit_rule_pit(conn)
        print("✓ Rebuild complete")
        
        # 验证结果
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM public.limit_rule_pit")
            count = cur.fetchone()[0]
            print(f"✓ Verification: {count} rows in limit_rule_pit")
            
    except Exception as e:
        conn.rollback()
        print(f"✗ Rebuild failed: {e}")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
