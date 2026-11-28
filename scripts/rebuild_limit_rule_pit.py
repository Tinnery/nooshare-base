#!/usr/bin/env python3
"""
重建 limit_rule_pit 表，消除冗余数据

策略：
1. 清空现有 limit_rule_pit
2. 按 (security_type, exchange) 聚合，找出规则变化点
3. 每个变化点插入一行，而不是每个交易日都插入
"""

from base.db import get_connection


def rebuild_limit_rule_pit(conn):
    """重建涨跌停规则 PIT 表"""
    with conn.cursor() as cur:
        # 1. 清空现有数据
        cur.execute("TRUNCATE TABLE public.limit_rule_pit RESTART IDENTITY")
        print("✓ Cleared existing limit_rule_pit")
        
        # 2. 重新填充：检测真实的规则变化点
        # 修复：1) CASE 顺序（先判断更具体的 3 位前缀）2) limit_ratio 显式类型转换
        #       3) 保留所有真实变化点，而不是只保留多数值
        cur.execute("""
            WITH stock_type_mapping AS (
                SELECT 
                    stock_code,
                    CASE 
                        WHEN LEFT(stock_code, 3) IN ('688', '689') THEN 'star_market'
                        WHEN LEFT(stock_code, 3) IN ('300', '301') THEN 'chi_next'
                        WHEN LEFT(stock_code, 1) = '6' THEN 'main_board'
                        WHEN LEFT(stock_code, 1) = '0' THEN 'main_board'
                        WHEN LEFT(stock_code, 1) = '6' AND LEFT(stock_code, 3) = '688' THEN 'star_market'
                        WHEN LEFT(stock_code, 1) = '6' THEN 'main_board'
                        WHEN LEFT(stock_code, 1) IN ('8', '4') THEN 'bse'
                        ELSE 'unknown'
                    END AS security_type,
                    CASE 
                        WHEN LEFT(stock_code, 1) IN ('0', '3') THEN 'sz'
                        WHEN LEFT(stock_code, 1) = '6' THEN 'sh'
                        WHEN LEFT(stock_code, 1) IN ('8', '4') THEN 'bj'
                        ELSE 'unknown'
                    END AS exchange,
                    start_date,
                    CAST(limit_ratio AS NUMERIC) AS limit_ratio
                FROM ju.limit_ratio
                WHERE limit_ratio IS NOT NULL AND start_date IS NOT NULL
            ),
            -- 按 (security_type, exchange) 分组，按日期排序，检测规则变化
            ordered_rules AS (
                SELECT 
                    security_type,
                    exchange,
                    start_date,
                    limit_ratio,
                    LAG(limit_ratio) OVER (
                        PARTITION BY security_type, exchange 
                        ORDER BY start_date
                    ) AS prev_ratio
                FROM stock_type_mapping
                WHERE security_type != 'unknown'
            ),
            -- 只保留规则发生变化的点（第一次出现或与前一次不同）
            rule_changes AS (
                SELECT 
                    security_type,
                    exchange,
                    limit_ratio,
                    start_date AS effective_from
                FROM ordered_rules
                WHERE prev_ratio IS NULL OR prev_ratio != limit_ratio
            )
            INSERT INTO public.limit_rule_pit (security_type, exchange, limit_ratio, effective_from, source)
            SELECT 
                security_type,
                exchange,
                limit_ratio / 100.0,
                effective_from,
                'ju.limit_ratio_v3'
            FROM rule_changes
        """)
        
        conn.commit()
        print(f"✓ limit_rule_pit rebuilt: {cur.rowcount} rows inserted")


def main():
    with get_connection() as conn:
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


if __name__ == "__main__":
    main()
