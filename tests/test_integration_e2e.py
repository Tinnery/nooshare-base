"""端到端合成集成测试（依赖数据库，CI integration job 执行）。

验证 P0/P1 修复后的完整数据产品：
  - 证券身份映射（P1.1）
  - ST 状态区间半开区间语义（P0.1）
  - GBBQ 字段语义统一（P0.2）
  - limit_rule_pit 正确性（P0.3）
  - hfq_daily 含 volume/amount（P0.5）
  - 财务查询字段对齐（P0.6）
  - Pipeline 幂等性（P1.2）
  - Lineage 追踪（P1.3）

运行：uv run pytest tests/test_integration_e2e.py -v
前提：CI fixture 已加载，schema 已初始化，pipeline 已 build。
"""
import pytest
from datetime import date

from base.db import read_sql


# ─────────────────────────────────────────────────────────────────────────────
# 1. 证券身份映射（P1.1）
# ─────────────────────────────────────────────────────────────────────────────

class TestSymbolIdentity:
    """验证 canonical_symbol 正确映射。"""

    def test_canonical_symbol_count(self):
        """应该有 2 只合成股票。"""
        df = read_sql("SELECT COUNT(*) AS cnt FROM public.canonical_symbol")
        assert df.iloc[0]["cnt"] == 2

    def test_canonical_symbol_fields(self):
        """验证字段完整性。"""
        df = read_sql("""
            SELECT canonical_id, symbol, exchange
            FROM public.canonical_symbol
            ORDER BY symbol
        """)
        assert len(df) == 2
        assert set(df["symbol"]) == {"sh600000", "sz000001"}
        assert set(df["exchange"]) == {"sh", "sz"}

    def test_exchange_mapping_correctness(self):
        """600000 → sh, 000001 → sz（P1.1 exchange 验证）。"""
        df = read_sql("""
            SELECT symbol, exchange
            FROM public.canonical_symbol
            WHERE symbol = 'sh600000'
        """)
        assert len(df) == 1
        assert df.iloc[0]["exchange"] == "sh"

        df = read_sql("""
            SELECT symbol, exchange
            FROM public.canonical_symbol
            WHERE symbol = 'sz000001'
        """)
        assert len(df) == 1
        assert df.iloc[0]["exchange"] == "sz"


# ─────────────────────────────────────────────────────────────────────────────
# 2. ST 状态区间（P0.1）
# ─────────────────────────────────────────────────────────────────────────────

class TestSTStatusInterval:
    """验证 ST 状态区间使用半开区间 [effective_from, effective_to)。"""

    def test_st_events_generated(self):
        """应该有 st_mark 和 st_remove 事件。"""
        df = read_sql("""
            SELECT event_type, COUNT(*) AS cnt
            FROM public.corporate_action_event
            WHERE event_type IN ('st_mark', 'st_remove')
            GROUP BY event_type
        """)
        assert len(df) == 2
        assert set(df["event_type"]) == {"st_mark", "st_remove"}

    def test_st_status_pit_interval_semantics(self):
        """ST 区间应该是半开区间 [mark_date, remove_date)。"""
        df = read_sql("""
            SELECT symbol, effective_from, effective_to, st_type
            FROM public.st_status_pit
            WHERE symbol = 'sh600000'
        """)
        # CI fixture: st_period('600000', '20230103', '20230605')
        # 期望：effective_from=2023-01-03, effective_to=2023-06-06（st_remove = end_date + 1）
        assert len(df) >= 1
        row = df.iloc[0]
        assert row["effective_from"] == date(2023, 1, 3)
        assert row["effective_to"] == date(2023, 6, 6)

    def test_st_query_as_of_before_mark(self):
        """查询 ST 状态：开始日期前一天 → 非 ST。"""
        from base.pit.views import query_st_status
        # 2023-01-02 应该不是 ST
        result = query_st_status("sh600000", "2023-01-02")
        assert result["is_st"] is False

    def test_st_query_as_of_on_mark_date(self):
        """查询 ST 状态：开始日期当天 → ST。"""
        from base.pit.views import query_st_status
        # 2023-01-03 应该是 ST
        result = query_st_status("sh600000", "2023-01-03")
        assert result["is_st"] is True

    def test_st_query_as_of_last_st_day(self):
        """查询 ST 状态：最后一个 ST 日期 → ST。"""
        from base.pit.views import query_st_status
        # 2023-06-05 应该是 ST（区间的最后一天）
        result = query_st_status("sh600000", "2023-06-05")
        assert result["is_st"] is True

    def test_st_query_as_of_remove_date(self):
        """查询 ST 状态：摘帽生效日 → 非 ST。"""
        from base.pit.views import query_st_status
        # 2023-06-06 应该不是 ST（摘帽生效日）
        result = query_st_status("sh600000", "2023-06-06")
        assert result["is_st"] is False


# ─────────────────────────────────────────────────────────────────────────────
# 3. GBBQ 字段语义（P0.2）
# ─────────────────────────────────────────────────────────────────────────────

class TestGBBQSemantics:
    """验证 GBBQ 字段语义统一为解释 B。"""

    def test_gbbq_events_extracted(self):
        """应该有 dividend 事件（category=1, c1>0）。"""
        df = read_sql("""
            SELECT event_type, COUNT(*) AS cnt
            FROM public.corporate_action_event
            WHERE event_type = 'dividend'
            GROUP BY event_type
        """)
        # CI fixture: 2 条 dividend 事件
        assert len(df) == 1
        assert df.iloc[0]["cnt"] == 2

    def test_gbbq_field_mapping(self):
        """验证 c1=派息, c2=配股价, c3=送转, c4=配股数量。"""
        df = read_sql("""
            SELECT symbol, effective_date, payload
            FROM public.corporate_action_event
            WHERE event_type = 'dividend'
            ORDER BY symbol
        """)
        assert len(df) == 2

        # sh600000: c1=2.0 (每10股派2元)
        row_sh = df[df["symbol"] == "sh600000"].iloc[0]
        assert row_sh["effective_date"] == date(2024, 1, 4)
        assert float(row_sh["payload"]["dividend_per_10_shares"]) == 2.0

        # sz000001: c1=1.2, c3=5.0 (每10股派1.2元+送5股)
        row_sz = df[df["symbol"] == "sz000001"].iloc[0]
        assert row_sz["effective_date"] == date(2024, 1, 5)
        assert float(row_sz["payload"]["dividend_per_10_shares"]) == 1.2
        assert float(row_sz["payload"]["split_per_10_shares"]) == 5.0


# ─────────────────────────────────────────────────────────────────────────────
# 4. limit_rule_pit（P0.3）
# ─────────────────────────────────────────────────────────────────────────────

class TestLimitRulePIT:
    """验证涨跌停规则 PIT 正确性。"""

    def test_limit_rule_pit_exists(self):
        """limit_rule_pit 表应该有数据。"""
        df = read_sql("SELECT COUNT(*) AS cnt FROM public.limit_rule_pit")
        assert df.iloc[0]["cnt"] > 0

    def test_limit_rule_exchange_mapping(self):
        """验证交易所映射正确（P0.3 CASE 顺序修复）。"""
        df = read_sql("""
            SELECT symbol, exchange, limit_ratio
            FROM public.limit_rule_pit
            ORDER BY symbol
        """)
        # sh600000 → 10%, sz000001 → 10%（主板）
        for _, row in df.iterrows():
            if row["symbol"] in ["sh600000", "sz000001"]:
                assert float(row["limit_ratio"]) == 0.10


# ─────────────────────────────────────────────────────────────────────────────
# 5. hfq_daily（P0.5）
# ─────────────────────────────────────────────────────────────────────────────

class TestHFQDaily:
    """验证后复权日线含 volume/amount。"""

    def test_hfq_daily_has_volume_amount(self):
        """hfq_daily 表应该有 volume 和 amount 列。"""
        df = read_sql("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'hfq_daily'
        """)
        columns = set(df["column_name"])
        assert "volume" in columns
        assert "amount" in columns

    def test_hfq_daily_data(self):
        """hfq_daily 应该有数据。"""
        df = read_sql("SELECT COUNT(*) AS cnt FROM public.hfq_daily")
        assert df.iloc[0]["cnt"] > 0

    def test_hfq_daily_volume_amount_not_null(self):
        """volume 和 amount 不应该为 NULL。"""
        df = read_sql("""
            SELECT symbol, date, volume, amount
            FROM public.hfq_daily
            WHERE volume IS NULL OR amount IS NULL
        """)
        assert len(df) == 0


# ─────────────────────────────────────────────────────────────────────────────
# 6. 财务查询（P0.6）
# ─────────────────────────────────────────────────────────────────────────────

class TestFinancialQuery:
    """验证财务查询字段对齐。"""

    def test_financial_query_no_error(self):
        """query_financial_data 不应该报"列不存在"错误。"""
        from base.pit.views import query_financial_data
        # 不应该抛出异常
        result = query_financial_data("sh600000", "2023-12-31")
        assert result is not None
        assert "symbol" in result or "code" in result


# ─────────────────────────────────────────────────────────────────────────────
# 7. Pipeline 幂等性（P1.2）
# ─────────────────────────────────────────────────────────────────────────────

class TestPipelineIdempotency:
    """验证 Pipeline 幂等性。"""

    def test_pipeline_run_has_output_hash(self):
        """pipeline_run 表应该有 output_hash 列。"""
        df = read_sql("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'pipeline_run'
        """)
        columns = set(df["column_name"])
        assert "output_hash" in columns

    def test_two_builds_have_same_output_hash(self):
        """两次 build 应该有相同的 output_hash（幂等性）。"""
        df = read_sql("""
            SELECT output_hash
            FROM public.pipeline_run
            WHERE status = 'success'
            ORDER BY started_at DESC
            LIMIT 2
        """)
        if len(df) == 2:
            # 两次 build 的 output_hash 应该相同
            assert df.iloc[0]["output_hash"] == df.iloc[1]["output_hash"]


# ─────────────────────────────────────────────────────────────────────────────
# 8. Lineage 追踪（P1.3）
# ─────────────────────────────────────────────────────────────────────────────

class TestLineage:
    """验证 Lineage 追踪正确性。"""

    def test_valuation_lineage_has_symbol_constraint(self):
        """估值血缘应该有证券代码约束。"""
        from base.lineage.tracker import build_valuation_lineage
        from base.db import get_conn

        conn = get_conn()
        try:
            result = build_valuation_lineage(
                conn, symbol="sh600000", trade_date="2023-12-31"
            )
            assert result is not None
            assert "sources" in result
            # 应该有 ju.valuation 源
            assert any(s["source_table"] == "ju.valuation" for s in result["sources"])
        finally:
            conn.close()

    def test_st_status_lineage_uses_lateral_join(self):
        """ST 状态血缘应该描述 LATERAL JOIN 构造。"""
        from base.lineage.tracker import build_st_status_lineage
        from base.db import get_conn

        conn = get_conn()
        try:
            result = build_st_status_lineage(conn, symbol="sh600000")
            assert result is not None
            # 应该包含 interval_construction 描述
            for record in result.get("lineage_records", []):
                if record["target_table"] == "public.st_status_pit":
                    rule = record.get("transform_rule", {})
                    assert "interval_construction" in rule
                    # 应该是 LATERAL JOIN，不是 LEAD
                    assert "LATERAL" in rule["interval_construction"] or "lateral" in rule["interval_construction"].lower()
        finally:
            conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# 9. Freshness/SLA（P1.4）
# ─────────────────────────────────────────────────────────────────────────────

class TestFreshnessSLA:
    """验证 Freshness 和 SLA 修正。"""

    def test_data_freshness_has_data(self):
        """data_freshness 表应该有数据。"""
        df = read_sql("SELECT COUNT(*) AS cnt FROM public.data_freshness")
        assert df.iloc[0]["cnt"] > 0

    def test_freshness_status_not_all_missing(self):
        """不应该所有表都是 missing 状态。"""
        df = read_sql("""
            SELECT status, COUNT(*) AS cnt
            FROM public.data_freshness
            GROUP BY status
        """)
        # 至少有一些表是 fresh 或 stale
        statuses = set(df["status"])
        assert "missing" not in statuses or len(df) > 1


# ─────────────────────────────────────────────────────────────────────────────
# 10. 综合验证
# ─────────────────────────────────────────────────────────────────────────────

class TestE2EIntegration:
    """端到端综合验证。"""

    def test_all_public_tables_exist(self):
        """所有 public.* 表都应该存在。"""
        df = read_sql("""
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public'
        """)
        tables = set(df["table_name"])
        required = {
            "canonical_symbol",
            "corporate_action_event",
            "st_status_pit",
            "valuation_pit",
            "financial_data_pit",
            "limit_rule_pit",
            "trading_calendar",
            "hfq_daily",
            "pipeline_run",
            "data_freshness",
        }
        missing = required - tables
        assert len(missing) == 0, f"缺少表: {missing}"

    def test_pipeline_run_success(self):
        """至少有一次成功的 pipeline 运行。"""
        df = read_sql("""
            SELECT COUNT(*) AS cnt
            FROM public.pipeline_run
            WHERE status = 'success'
        """)
        assert df.iloc[0]["cnt"] > 0
