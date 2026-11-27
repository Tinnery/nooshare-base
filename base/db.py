"""Database connection and operations. 沿用 indicators 模式。"""
import logging
from contextlib import contextmanager
from io import StringIO
from typing import Optional

import pandas as pd
import psycopg2

from . import config

log = logging.getLogger(__name__)


@contextmanager
def get_connection():
    """Get database connection context manager."""
    conn = psycopg2.connect(config.get_pg_dsn())
    try:
        yield conn
    finally:
        conn.close()


def execute_sql(sql: str, params: Optional[tuple] = None, fetch: bool = False):
    """Execute SQL statement."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            if fetch:
                return cur.fetchall()
            conn.commit()


def read_sql(query: str, params: Optional[tuple] = None) -> pd.DataFrame:
    """Read SQL query into DataFrame."""
    with get_connection() as conn:
        return pd.read_sql(query, conn, params=params)


def init_schema():
    """Initialize public schema and tables."""
    execute_sql("CREATE SCHEMA IF NOT EXISTS public;")

    tables_sql = """
    -- P1: 规范符号主数据
    CREATE TABLE IF NOT EXISTS public.canonical_symbol (
        canonical_id TEXT PRIMARY KEY,
        exchange TEXT NOT NULL,
        code TEXT NOT NULL,
        symbol TEXT NOT NULL,
        asset_class TEXT,
        list_date DATE,
        delist_date DATE,
        created_at TIMESTAMPTZ DEFAULT now(),
        updated_at TIMESTAMPTZ DEFAULT now()
    );

    -- P2: 公司行为事件溯源
    CREATE TABLE IF NOT EXISTS public.corporate_action_event (
        event_id TEXT PRIMARY KEY,
        canonical_id TEXT NOT NULL REFERENCES public.canonical_symbol(canonical_id),
        event_type TEXT NOT NULL,
        effective_date DATE NOT NULL,
        pub_date DATE,
        observed_at TIMESTAMPTZ DEFAULT now(),
        payload JSONB,
        source_id TEXT,
        created_at TIMESTAMPTZ DEFAULT now(),
        UNIQUE (canonical_id, event_type, effective_date, source_id)
    );

    -- P4: 跨源对账状态
    CREATE TABLE IF NOT EXISTS public.reconcile_status (
        namespace TEXT NOT NULL,
        table_name TEXT NOT NULL,
        check_type TEXT NOT NULL,
        check_date DATE NOT NULL,
        status TEXT NOT NULL,
        details JSONB,
        checked_at TIMESTAMPTZ DEFAULT now(),
        PRIMARY KEY (namespace, table_name, check_type, check_date)
    );

    -- P5: 数据新鲜度
    CREATE TABLE IF NOT EXISTS public.data_freshness (
        namespace TEXT NOT NULL,
        table_name TEXT NOT NULL,
        max_date DATE,
        row_count BIGINT,
        refreshed_at TIMESTAMPTZ DEFAULT now(),
        status TEXT,
        PRIMARY KEY (namespace, table_name)
    );

    -- P7: 幂等管线运行记录
    CREATE TABLE IF NOT EXISTS public.pipeline_run (
        run_id TEXT PRIMARY KEY,
        started_at TIMESTAMPTZ NOT NULL,
        completed_at TIMESTAMPTZ,
        code_commit TEXT,
        input_hash TEXT,
        output_hash TEXT,
        status TEXT,
        products JSONB
    );

    -- P3 扩展: 后复权价格序列（base hfq 命令计算写入，源 tong.gbbq category=1）
    CREATE TABLE IF NOT EXISTS public.hfq_daily (
        symbol TEXT NOT NULL,
        date INTEGER NOT NULL,
        open DOUBLE PRECISION,
        high DOUBLE PRECISION,
        low DOUBLE PRECISION,
        close DOUBLE PRECISION,
        volume BIGINT,
        amount DOUBLE PRECISION,
        open_hfq DOUBLE PRECISION,
        high_hfq DOUBLE PRECISION,
        low_hfq DOUBLE PRECISION,
        close_hfq DOUBLE PRECISION,
        factor_cum DOUBLE PRECISION NOT NULL,
        hfq_return DOUBLE PRECISION,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (symbol, date)
    );

    -- 交易日历（PIT）：记录每日是否开盘
    CREATE TABLE IF NOT EXISTS public.trading_calendar (
        cal_date DATE NOT NULL,
        market TEXT NOT NULL DEFAULT 'CN',
        is_open BOOLEAN NOT NULL,
        effective_from TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        effective_to TIMESTAMPTZ,
        PRIMARY KEY (cal_date, market, effective_from)
    );

    -- 涨跌停规则（PIT）：按证券类型和交易所记录涨跌停比例
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

    -- 索引
    CREATE INDEX IF NOT EXISTS idx_canonical_symbol_symbol ON public.canonical_symbol(symbol);
    CREATE INDEX IF NOT EXISTS idx_corporate_action_event_canonical ON public.corporate_action_event(canonical_id, effective_date);
    CREATE INDEX IF NOT EXISTS idx_pipeline_run_started ON public.pipeline_run(started_at DESC);
    CREATE INDEX IF NOT EXISTS idx_hfq_daily_date ON public.hfq_daily(date);
    CREATE INDEX IF NOT EXISTS idx_trading_calendar_date ON public.trading_calendar(cal_date);
    CREATE INDEX IF NOT EXISTS idx_limit_rule_pit_type_exchange ON public.limit_rule_pit(security_type, exchange, effective_from);
    """
    execute_sql(tables_sql)

    # v1 → v2 迁移：为旧表添加 volume 和 amount 列（如果不存在）
    # 这确保旧数据库也能升级到契约 v2，而不仅限于新建数据库
    migration_sql = """
    ALTER TABLE public.hfq_daily
        ADD COLUMN IF NOT EXISTS volume BIGINT;
    
    ALTER TABLE public.hfq_daily
        ADD COLUMN IF NOT EXISTS amount DOUBLE PRECISION;
    """
    execute_sql(migration_sql)
    log.info("hfq_daily v1→v2 迁移完成：volume/amount 列已添加（如不存在）")

    # P3: 物化 PIT 视图
    # 先删除旧视图（如果存在）
    execute_sql("DROP VIEW IF EXISTS public.canonical_symbol_pit CASCADE;")
    execute_sql("DROP MATERIALIZED VIEW IF EXISTS public.st_status_pit CASCADE;")
    execute_sql("DROP MATERIALIZED VIEW IF EXISTS public.financial_data_pit CASCADE;")
    
    # 创建物化视图
    mv_sql = """
    -- ST 状态物化视图：只从 st_mark 生成区间，用下一个 st_remove 标记结束
    -- 区间语义：[effective_from, effective_to)，即 effective_from <= as_of < effective_to
    CREATE MATERIALIZED VIEW IF NOT EXISTS public.st_status_pit AS
    SELECT
        mark.canonical_id,
        c.symbol,
        mark.effective_date AS effective_from,
        remove.effective_date AS effective_to,
        mark.pub_date,
        mark.payload->>'st_type' AS st_type,
        mark.observed_at
    FROM public.corporate_action_event mark
    LEFT JOIN LATERAL (
        SELECT e.effective_date
        FROM public.corporate_action_event e
        WHERE e.canonical_id = mark.canonical_id
          AND e.event_type = 'st_remove'
          AND e.effective_date > mark.effective_date
        ORDER BY e.effective_date
        LIMIT 1
    ) remove ON true
    JOIN public.canonical_symbol c ON c.canonical_id = mark.canonical_id
    WHERE mark.event_type = 'st_mark'
    ORDER BY mark.canonical_id, mark.effective_date;

    -- 估值数据物化视图：从 ju.valuation 投影，关联 canonical_id
    CREATE MATERIALIZED VIEW IF NOT EXISTS public.valuation_pit AS
    SELECT
        c.canonical_id,
        c.symbol,
        v.start_date AS effective_from,
        v.end_date AS effective_to,
        v.capitalization,
        v.circulating_cap,
        v.refreshed_at AS observed_at
    FROM ju.valuation v
    JOIN public.canonical_symbol c ON c.code = v.stock_code
    ORDER BY c.canonical_id, v.start_date;

    -- 复权因子物化视图：从 dividend/split/rights_issue 事件构建
    -- Phase A 修复：category=1 的配股分量（c4）拆为 rights_issue，必须纳入投影
    CREATE MATERIALIZED VIEW IF NOT EXISTS public.adjustment_factor_pit AS
    SELECT
        e.canonical_id,
        c.symbol,
        e.effective_date,
        e.event_type,
        e.payload,
        e.observed_at
    FROM public.corporate_action_event e
    JOIN public.canonical_symbol c ON c.canonical_id = e.canonical_id
    WHERE e.event_type IN ('dividend', 'split', 'rights_issue')
    ORDER BY e.canonical_id, e.effective_date;

    -- 财务报表 PIT 视图：利润表
    CREATE MATERIALIZED VIEW IF NOT EXISTS public.income_statement_pit AS
    SELECT
        c.canonical_id,
        c.symbol,
        i.report_date,
        i.pub_date,
        i.revenue,
        i.operating_revenue,
        i.net_profit,
        i.net_profit_parent,
        i.eps,
        i.created_at AS observed_at
    FROM finance.income_statement i
    JOIN public.canonical_symbol c ON c.code = i.code
    ORDER BY c.canonical_id, i.report_date;

    -- 财务报表 PIT 视图：资产负债表
    CREATE MATERIALIZED VIEW IF NOT EXISTS public.balance_sheet_pit AS
    SELECT
        c.canonical_id,
        c.symbol,
        b.report_date,
        b.pub_date,
        b.total_assets,
        b.current_assets,
        b.total_liabilities,
        b.shareholders_equity,
        b.created_at AS observed_at
    FROM finance.balance_sheet b
    JOIN public.canonical_symbol c ON c.code = b.code
    ORDER BY c.canonical_id, b.report_date;

    -- 财务报表 PIT 视图：现金流量表
    CREATE MATERIALIZED VIEW IF NOT EXISTS public.cash_flow_pit AS
    SELECT
        c.canonical_id,
        c.symbol,
        cf.report_date,
        cf.pub_date,
        cf.operating_cash_flow,
        cf.investing_cash_flow,
        cf.financing_cash_flow,
        cf.net_cash_flow,
        cf.created_at AS observed_at
    FROM finance.cash_flow cf
    JOIN public.canonical_symbol c ON c.code = cf.code
    ORDER BY c.canonical_id, cf.report_date;

    -- 物化视图索引
    CREATE INDEX IF NOT EXISTS idx_st_status_pit_canonical ON public.st_status_pit(canonical_id, effective_from);
    CREATE INDEX IF NOT EXISTS idx_st_status_pit_symbol ON public.st_status_pit(symbol, effective_from);
    CREATE INDEX IF NOT EXISTS idx_valuation_pit_canonical ON public.valuation_pit(canonical_id, effective_from);
    CREATE INDEX IF NOT EXISTS idx_valuation_pit_symbol ON public.valuation_pit(symbol, effective_from);
    CREATE INDEX IF NOT EXISTS idx_adjustment_factor_pit_canonical ON public.adjustment_factor_pit(canonical_id, effective_date);
    CREATE INDEX IF NOT EXISTS idx_adjustment_factor_pit_symbol ON public.adjustment_factor_pit(symbol, effective_date);
    CREATE INDEX IF NOT EXISTS idx_income_statement_pit_canonical ON public.income_statement_pit(canonical_id, report_date);
    CREATE INDEX IF NOT EXISTS idx_income_statement_pit_symbol ON public.income_statement_pit(symbol, report_date);
    CREATE INDEX IF NOT EXISTS idx_balance_sheet_pit_canonical ON public.balance_sheet_pit(canonical_id, report_date);
    CREATE INDEX IF NOT EXISTS idx_balance_sheet_pit_symbol ON public.balance_sheet_pit(symbol, report_date);
    CREATE INDEX IF NOT EXISTS idx_cash_flow_pit_canonical ON public.cash_flow_pit(canonical_id, report_date);
    CREATE INDEX IF NOT EXISTS idx_cash_flow_pit_symbol ON public.cash_flow_pit(symbol, report_date);
    """
    execute_sql(mv_sql)


def refresh_materialized_views():
    """刷新所有物化 PIT 视图。在 pipeline 构建完成后调用。"""
    views = [
        "public.st_status_pit",
        "public.valuation_pit", 
        "public.adjustment_factor_pit",
        "public.income_statement_pit",
        "public.balance_sheet_pit",
        "public.cash_flow_pit",
    ]
    for view in views:
        log.info(f"刷新物化视图: {view}")
        execute_sql(f"REFRESH MATERIALIZED VIEW {view}")
    log.info(f"已刷新 {len(views)} 个物化视图")


def copy_upsert(df: pd.DataFrame, schema: str, table: str):
    """COPY + temp table upsert pattern. 沿用 indicators 模式。

    只 COPY DataFrame 中有的列；DEFAULT 列（如 created_at）由数据库自动填充。
    """
    if df.empty:
        return
    temp_table = f"_temp_{table}"
    columns = list(df.columns)
    col_str = ", ".join(columns)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS {temp_table}")
            # 临时表只包含 DataFrame 有的列，不带 DEFAULT
            cur.execute(f"""
                CREATE TEMP TABLE {temp_table} AS
                SELECT {col_str} FROM {schema}.{table} WHERE FALSE
            """)
            buf = StringIO()
            df.to_csv(buf, sep="\t", header=False, index=False, na_rep="\\N")
            buf.seek(0)
            cur.copy_from(buf, temp_table, null="\\N")

            # 查询目标表的主键列
            cur.execute(
                "SELECT a.attname FROM pg_index i "
                "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
                "WHERE i.indrelid = '%s.%s'::regclass AND i.indisprimary" % (schema, table)
            )
            pk_cols = [row[0] for row in cur.fetchall()]
            pk_str = ", ".join(pk_cols)
            update_str = ", ".join(
                f"{c} = EXCLUDED.{c}" for c in columns if c not in pk_cols
            )
            cur.execute(
                f"INSERT INTO {schema}.{table} ({col_str}) "
                f"SELECT {col_str} FROM {temp_table} "
                f"ON CONFLICT ({pk_str}) DO UPDATE SET {update_str}"
            )
            cur.execute(f"DROP TABLE IF EXISTS {temp_table}")
        conn.commit()
