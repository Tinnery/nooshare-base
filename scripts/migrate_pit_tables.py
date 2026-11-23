#!/usr/bin/env python3
"""
T2.2: 创建 public.trading_calendar 和 public.limit_rule_pit（PIT 表）

从 ju.trading_day 和 ju.limit_ratio 回填数据。
"""

import psycopg2
from psycopg2 import sql

DB_CONFIG = {
    "host": "localhost",
    "port": 5432,
    "dbname": "nooshare_dev",
    "user": "postgres",
}


def create_trading_calendar(conn):
    """创建交易日历 PIT 表"""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS public.trading_calendar (
                cal_date DATE NOT NULL,
                market TEXT NOT NULL DEFAULT 'CN',
                is_open BOOLEAN NOT NULL,
                effective_from TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                effective_to TIMESTAMPTZ,
                PRIMARY KEY (cal_date, market, effective_from)
            );
            
            COMMENT ON TABLE public.trading_calendar IS '交易日历（PIT）：记录每日是否开盘';
            COMMENT ON COLUMN public.trading_calendar.cal_date IS '日历日期';
            COMMENT ON COLUMN public.trading_calendar.market IS '市场标识（CN=A股）';
            COMMENT ON COLUMN public.trading_calendar.is_open IS '是否开盘';
            COMMENT ON COLUMN public.trading_calendar.effective_from IS '版本生效时间';
            COMMENT ON COLUMN public.trading_calendar.effective_to IS '版本失效时间（NULL=当前版本）';
        """)
        
        # 回填数据
        cur.execute("""
            INSERT INTO public.trading_calendar (cal_date, market, is_open, effective_from)
            SELECT trade_date, 'CN', is_trading_day, NOW()
            FROM ju.trading_day
            ON CONFLICT DO NOTHING
        """)
        
        conn.commit()
        print(f"✓ trading_calendar: {cur.rowcount} rows inserted")


def create_limit_rule_pit(conn):
    """创建涨跌停规则 PIT 表"""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS public.limit_rule_pit (
                rule_id SERIAL PRIMARY KEY,
                security_type TEXT NOT NULL,
                exchange TEXT NOT NULL,
                limit_ratio NUMERIC(5,4) NOT NULL,
                effective_from DATE NOT NULL,
                effective_to DATE,
                source TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (security_type, exchange, effective_from)
            );
            
            COMMENT ON TABLE public.limit_rule_pit IS '涨跌停规则（PIT）：按证券类型和交易所记录涨跌停比例';
            COMMENT ON COLUMN public.limit_rule_pit.security_type IS '证券类型：main_board/chi_next/star_market/bse/st';
            COMMENT ON COLUMN public.limit_rule_pit.exchange IS '交易所：sh/sz/bj';
            COMMENT ON COLUMN public.limit_rule_pit.limit_ratio IS '涨跌停比例（如 0.1000 = 10%）';
            COMMENT ON COLUMN public.limit_rule_pit.effective_from IS '规则生效日期';
            COMMENT ON COLUMN public.limit_rule_pit.effective_to IS '规则失效日期（NULL=当前有效）';
            COMMENT ON COLUMN public.limit_rule_pit.source IS '数据来源：ju.limit_ratio/manual';
        """)
        
        # 回填数据：按 (security_type, exchange, start_date) 聚合
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
            aggregated_rules AS (
                SELECT 
                    security_type,
                    exchange,
                    start_date,
                    COALESCE(
                        MODE() WITHIN GROUP (ORDER BY limit_ratio) FILTER (WHERE limit_ratio != 5.0),
                        MODE() WITHIN GROUP (ORDER BY limit_ratio)
                    ) AS final_ratio,
                    COUNT(*) AS stock_count
                FROM stock_type_mapping
                WHERE security_type != 'unknown'
                GROUP BY security_type, exchange, start_date
            )
            INSERT INTO public.limit_rule_pit (security_type, exchange, limit_ratio, effective_from, source)
            SELECT 
                security_type,
                exchange,
                final_ratio / 100.0,
                start_date,
                'ju.limit_ratio_v1'
            FROM aggregated_rules
            ON CONFLICT DO NOTHING
        """)
        
        conn.commit()
        print(f"✓ limit_rule_pit: {cur.rowcount} rows inserted")


def main():
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        print("Creating PIT tables (T2.2)...")
        create_trading_calendar(conn)
        create_limit_rule_pit(conn)
        print("✓ T2.2 migration complete")
    except Exception as e:
        conn.rollback()
        print(f"✗ Migration failed: {e}")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
