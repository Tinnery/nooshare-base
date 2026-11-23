#!/usr/bin/env python3
"""CI 集成测试 fixture：构建合成源 schema 与最小数据集（幂等）。

用途：在**隔离的** PostgreSQL（本地一次性容器或 CI service 容器）上创建
ju / tong / finance 三个源 namespace 的最小表结构与合成数据，
使 P1–P5 管线与 V9–V12 门禁可以端到端运行。

约束：
  - 数据全部为合成数据，不含任何真实行情、财务或两融数据；
  - 列类型贴近上游真实模式（ju.* 以 TEXT 为主，tong 日期为 INTEGER YYYYMMDD）；
  - 只在隔离库运行。脚本不 DROP 任何已存在的 schema/表，避免误伤真实环境。

管线覆盖对照（合成数据 → 管线行为）：
  - P1 canonical_symbol: ju.stocks ⋈ tong.symbol_meta → 2 只股票
  - P2 corporate_action_event: ju.st_period（1 段 ST 区间 → mark+remove）
    + tong.gbbq（2 条 category=1 除权除息 → dividend）
  - P3 物化视图: ju.valuation / finance 三表 → 4 个 PIT 视图有数据
  - P4 对账: 两源标识一致、日历无周末、事件无异常
  - P5 新鲜度: 全部扫描表有数据（合成数据日期较早，会记 stale，属预期）
  - V9: tong.stock_daily 连续 4 日正价格（可加性数学恒等）
  - V10: 全部 effective_from <= max(trading_day)
  - V11: 需配合"两次 build"产生幂等对（CI workflow 已编排）
  - V12: canonical_id / (exchange, code) 唯一

用法：
    uv run python scripts/ci_fixture.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from base.db import execute_sql, read_sql  # noqa: E402

DDL_AND_DATA = """
-- ========== 源 schema（CI 合成环境，非生产结构） ==========
CREATE SCHEMA IF NOT EXISTS ju;
CREATE SCHEMA IF NOT EXISTS tong;
CREATE SCHEMA IF NOT EXISTS finance;

-- ju.*（上游真实类型以 TEXT/BOOLEAN 为主）
CREATE TABLE IF NOT EXISTS ju.stocks (
    stock_code TEXT, display_name TEXT, exchange TEXT,
    start_date TEXT, end_date TEXT
);
CREATE TABLE IF NOT EXISTS ju.trading_day (trade_date TEXT);
CREATE TABLE IF NOT EXISTS ju.pub_date (stock_code TEXT, pub_date TEXT);
CREATE TABLE IF NOT EXISTS ju.st_period (stock_code TEXT, start_date TEXT, end_date TEXT);
CREATE TABLE IF NOT EXISTS ju.valuation (
    stock_code TEXT, start_date TEXT, end_date TEXT,
    capitalization TEXT, circulating_cap TEXT, refreshed_at TEXT
);

-- tong.*（日期为 INTEGER YYYYMMDD，价格 numeric）
CREATE TABLE IF NOT EXISTS tong.symbol_meta (symbol TEXT, class TEXT);
CREATE TABLE IF NOT EXISTS tong.stock_daily (
    symbol TEXT, date INTEGER,
    open NUMERIC, high NUMERIC, low NUMERIC, close NUMERIC
);
CREATE TABLE IF NOT EXISTS tong.index_daily (symbol TEXT, date INTEGER, close NUMERIC);
CREATE TABLE IF NOT EXISTS tong.etf_daily (symbol TEXT, date INTEGER, close NUMERIC);
CREATE TABLE IF NOT EXISTS tong.gbbq (
    symbol TEXT, stock_code TEXT, date INTEGER, category INTEGER,
    c1 NUMERIC, c2 NUMERIC, c3 NUMERIC, c4 NUMERIC
);

-- finance.*（freshness 扫描 cash_flow_statement；PIT 视图用 cash_flow）
CREATE TABLE IF NOT EXISTS finance.income_statement (
    code TEXT, report_date TEXT, pub_date TEXT,
    revenue NUMERIC, operating_revenue NUMERIC, net_profit NUMERIC,
    net_profit_parent NUMERIC, eps NUMERIC, created_at TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE IF NOT EXISTS finance.balance_sheet (
    code TEXT, report_date TEXT, pub_date TEXT,
    total_assets NUMERIC, current_assets NUMERIC, total_liabilities NUMERIC,
    shareholders_equity NUMERIC, created_at TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE IF NOT EXISTS finance.cash_flow (
    code TEXT, report_date TEXT, pub_date TEXT,
    operating_cash_flow NUMERIC, investing_cash_flow NUMERIC,
    financing_cash_flow NUMERIC, net_cash_flow NUMERIC,
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE IF NOT EXISTS finance.cash_flow_statement (
    code TEXT, report_date TEXT, pub_date TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- ========== 合成数据（幂等：TRUNCATE + INSERT） ==========
TRUNCATE ju.stocks, ju.trading_day, ju.pub_date, ju.st_period, ju.valuation,
         tong.symbol_meta, tong.stock_daily, tong.index_daily, tong.etf_daily, tong.gbbq,
         finance.income_statement, finance.balance_sheet, finance.cash_flow,
         finance.cash_flow_statement;

INSERT INTO ju.stocks VALUES
    ('600000', '浦发银行(合成)', 'sh', '19991110', NULL),
    ('000001', '平安银行(合成)', 'sz', '19910403', NULL);

INSERT INTO tong.symbol_meta VALUES
    ('sh600000', 'stock'),
    ('sz000001', 'stock');

INSERT INTO ju.trading_day VALUES
    ('20240102'), ('20240103'), ('20240104'), ('20240105');

INSERT INTO ju.pub_date VALUES
    ('600000', '20240420'), ('000001', '20240420');

-- 一段 ST 区间 → st_mark(20230103) + st_remove(20230606)
INSERT INTO ju.st_period VALUES
    ('600000', '20230103', '20230605');

INSERT INTO ju.valuation VALUES
    ('600000', '20230101', '20231231', '19777700000', '14800000000', '2024-01-01 00:00:00'),
    ('000001', '20230101', '20231231', '29000000000', '21000000000', '2024-01-01 00:00:00');

-- 4 个连续交易日日线（V9 需要每股 >= 3 行正价格）
INSERT INTO tong.stock_daily VALUES
    ('sh600000', 20240102, 10.00, 10.20,  9.90, 10.10),
    ('sh600000', 20240103, 10.10, 10.40, 10.00, 10.30),
    ('sh600000', 20240104,  9.95, 10.10,  9.80,  9.90),
    ('sh600000', 20240105,  9.90, 10.05,  9.85, 10.00),
    ('sz000001', 20240102, 11.00, 11.20, 10.90, 11.00),
    ('sz000001', 20240103, 11.00, 11.30, 10.95, 11.20),
    ('sz000001', 20240104, 11.20, 11.40, 11.00, 11.30),
    ('sz000001', 20240105,  7.55,  7.70,  7.40,  7.60);

INSERT INTO tong.index_daily VALUES
    ('sh000001', 20240102, 2960.00), ('sh000001', 20240103, 2970.00);

INSERT INTO tong.etf_daily VALUES
    ('sh510300', 20240102, 3.850), ('sh510300', 20240103, 3.860);

-- category=1 除权除息：
--   sh600000 20240104 每10股派2元   → P_ex = (10.30 - 0.2) / 1.0
--   sz000001 20240105 每10股派1.2元+送5股 → P_ex = (11.30 - 0.12) / 1.5
INSERT INTO tong.gbbq VALUES
    ('sh600000', '600000', 20240104, 1, 2.0, 0, 0,   0),
    ('sz000001', '000001', 20240105, 1, 1.2, 0, 5.0, 0);

INSERT INTO finance.income_statement VALUES
    ('600000', '20231231', '20240420', 173000000000, 143000000000, 42000000000, 41000000000, 1.39),
    ('000001', '20231231', '20240420', 165000000000, 150000000000, 31000000000, 30000000000, 1.58);

INSERT INTO finance.balance_sheet VALUES
    ('600000', '20231231', '20240420', 7500000000000, 3200000000000, 6900000000000, 600000000000),
    ('000001', '20231231', '20240420', 3400000000000, 1800000000000, 3100000000000, 300000000000);

INSERT INTO finance.cash_flow VALUES
    ('600000', '20231231', '20240420', 45000000000, -12000000000, -8000000000, 25000000000),
    ('000001', '20231231', '20240420', 38000000000, -9000000000,  -6000000000, 23000000000);

INSERT INTO finance.cash_flow_statement VALUES
    ('600000', '20231231', '20240420'),
    ('000001', '20231231', '20240420');
"""


def main() -> int:
    execute_sql(DDL_AND_DATA)
    counts = read_sql("""
        SELECT
            (SELECT COUNT(*) FROM ju.stocks)          AS ju_stocks,
            (SELECT COUNT(*) FROM ju.trading_day)     AS ju_trading_day,
            (SELECT COUNT(*) FROM tong.stock_daily)   AS tong_stock_daily,
            (SELECT COUNT(*) FROM tong.gbbq)          AS tong_gbbq,
            (SELECT COUNT(*) FROM finance.income_statement) AS fin_income
    """)
    print(counts.to_string(index=False))
    print("CI fixture ready (synthetic data only).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
