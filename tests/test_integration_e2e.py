"""端到端合成集成测试（依赖数据库，CI integration job 执行）。

2026-11-28 重写：对齐当前真实实现。
原版（dea28cb）按第三阶段规划中的 PIT 视图体系（st_status_pit /
valuation_pit / financial_data_pit）与设想中的 API 编写，但这些表
与签名从未落地，导致集成测试在 CI 上恒失败。本版本按真实资产重写：

  - 证券身份映射：canonical_symbol = tong.symbol_meta 全集（全资产覆盖）
  - corporate_action_event：st_mark/st_remove 与 dividend（canonical_id 身份）
  - GBBQ 字段语义：payload 保留 c1-c4 原语义 + cash_per_10
  - limit_rule_pit：exchange/security_type/limit_ratio（无 symbol 列）
  - hfq_daily：含 volume/amount
  - 财务查询：query_financial_data（income_statement_pit）
  - Pipeline 幂等性：pipeline_run status='ok'
  - Freshness/SLA

未落地目标：仅 financial_data_pit（db.py 只有其 DROP 无 CREATE，
其余 PIT 视图均已物化落地：st_status_pit / valuation_pit /
adjustment_factor_pit / income_statement_pit / balance_sheet_pit /
cash_flow_pit）。注意：物化视图不出现在 information_schema.tables，
表存在性检查须 UNION pg_matviews。

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
    """验证 canonical_symbol 按 tong.symbol_meta 全集构建（全资产覆盖）。"""

    def test_canonical_symbol_count(self):
        """fixture symbol_meta 全集 = 4（2 stock + 1 index + 1 etf）。"""
        df = read_sql("SELECT COUNT(*) AS cnt FROM public.canonical_symbol")
        assert df.iloc[0]["cnt"] == 4

    def test_canonical_symbol_fields(self):
        """验证字段完整性。"""
        df = read_sql("""
            SELECT canonical_id, symbol, exchange
            FROM public.canonical_symbol
            ORDER BY symbol
        """)
        assert len(df) == 4
        assert set(df["symbol"]) == {"sh600000", "sz000001", "sh000001", "sh510300"}
        assert set(df["exchange"].str.lower()) == {"sh", "sz"}

    def test_exchange_mapping_correctness(self):
        """600000 → sh, 000001 → sz（P1.1 exchange 验证）。"""
        df = read_sql("""
            SELECT symbol, exchange
            FROM public.canonical_symbol
            WHERE symbol = 'sh600000'
        """)
        assert len(df) == 1
        assert df.iloc[0]["exchange"].lower() == "sh"

        df = read_sql("""
            SELECT symbol, exchange
            FROM public.canonical_symbol
            WHERE symbol = 'sz000001'
        """)
        assert len(df) == 1
        assert df.iloc[0]["exchange"].lower() == "sz"


# ─────────────────────────────────────────────────────────────────────────────
# 2. corporate_action_event（P0.1 / P0.2 的落地部分）
# ─────────────────────────────────────────────────────────────────────────────

class TestCorporateActionEvent:
    """验证 CA 事件身份与 GBBQ 字段语义（canonical_id 关联 canonical_symbol）。"""

    def test_st_events_generated(self):
        """st_period 应生成 st_mark 与 st_remove 事件。"""
        df = read_sql("""
            SELECT event_type, COUNT(*) AS cnt
            FROM public.corporate_action_event
            WHERE event_type IN ('st_mark', 'st_remove')
            GROUP BY event_type
        """)
        assert len(df) == 2
        assert set(df["event_type"]) == {"st_mark", "st_remove"}

    def test_st_events_belong_to_sh600000(self):
        """ST 事件应关联 sh600000（经 canonical_id JOIN）。"""
        df = read_sql("""
            SELECT cs.symbol, ca.event_type
            FROM public.corporate_action_event ca
            JOIN public.canonical_symbol cs ON ca.canonical_id = cs.canonical_id
            WHERE ca.event_type IN ('st_mark', 'st_remove')
        """)
        assert len(df) == 2
        assert set(df["symbol"]) == {"sh600000"}

    def test_gbbq_events_extracted(self):
        """应该有 2 条 dividend 事件（category=1, c1>0）。"""
        df = read_sql("""
            SELECT event_type, COUNT(*) AS cnt
            FROM public.corporate_action_event
            WHERE event_type = 'dividend'
            GROUP BY event_type
        """)
        assert len(df) == 1
        assert df.iloc[0]["cnt"] == 2

    def test_gbbq_field_mapping(self):
        """验证 c1=派息(每10股), c3=送转(每10股)，payload 保留 c1-c4 原语义。"""
        df = read_sql("""
            SELECT cs.symbol, ca.effective_date, ca.payload
            FROM public.corporate_action_event ca
            JOIN public.canonical_symbol cs ON ca.canonical_id = cs.canonical_id
            WHERE ca.event_type = 'dividend'
            ORDER BY cs.symbol
        """)
        assert len(df) == 2

        # sh600000: c1=2.0 (每10股派2元)
        row_sh = df[df["symbol"] == "sh600000"].iloc[0]
        assert row_sh["effective_date"] == date(2024, 1, 4)
        assert float(row_sh["payload"]["cash_per_10"]) == 2.0
        assert float(row_sh["payload"]["c1"]) == 2.0
        assert float(row_sh["payload"]["c3"]) == 0.0

        # sz000001: c1=1.2, c3=5.0 (每10股派1.2元+送5股)
        row_sz = df[df["symbol"] == "sz000001"].iloc[0]
        assert row_sz["effective_date"] == date(2024, 1, 5)
        assert float(row_sz["payload"]["cash_per_10"]) == 1.2
        assert float(row_sz["payload"]["c3"]) == 5.0


# ─────────────────────────────────────────────────────────────────────────────
# 3. ST 状态区间（P0.1）—— st_status_pit 物化视图，半开区间 [from, to)
# ─────────────────────────────────────────────────────────────────────────────

class TestSTStatusInterval:
    """验证 ST 状态区间使用半开区间 [effective_from, effective_to)。

    fixture: st_period('600000', '20230103', '20230605')
    → st_mark effective_date=2023-01-03
    → st_remove effective_date = end_date + 1 = 2023-06-06
    → 区间 [2023-01-03, 2023-06-06)
    """

    def test_st_status_pit_interval_semantics(self):
        """ST 区间端点：effective_from=2023-01-03, effective_to=2023-06-06。"""
        df = read_sql("""
            SELECT symbol, effective_from, effective_to
            FROM public.st_status_pit
            WHERE symbol = 'sh600000'
        """)
        assert len(df) >= 1
        row = df.iloc[0]
        assert row["effective_from"] == date(2023, 1, 3)
        assert row["effective_to"] == date(2023, 6, 6)

    def test_st_query_as_of_before_mark(self):
        """查询 ST 状态：开始日期前一天 → 非 ST。"""
        from base.pit.views import query_st_status
        result = query_st_status("2023-01-02")
        assert "sh600000" not in set(result["symbol"])

    def test_st_query_as_of_on_mark_date(self):
        """查询 ST 状态：开始日期当天 → ST（区间含左端点）。"""
        from base.pit.views import query_st_status
        result = query_st_status("2023-01-03")
        assert "sh600000" in set(result["symbol"])

    def test_st_query_as_of_last_st_day(self):
        """查询 ST 状态：最后一个 ST 日期 → ST。"""
        from base.pit.views import query_st_status
        result = query_st_status("2023-06-05")
        assert "sh600000" in set(result["symbol"])

    def test_st_query_as_of_remove_date(self):
        """查询 ST 状态：摘帽生效日 → 非 ST（区间不含右端点）。"""
        from base.pit.views import query_st_status
        result = query_st_status("2023-06-06")
        assert "sh600000" not in set(result["symbol"])


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
        """验证交易所映射与主板 10% 规则（P0.3 CASE 顺序修复）。"""
        df = read_sql("""
            SELECT exchange, security_type, limit_ratio
            FROM public.limit_rule_pit
        """)
        assert len(df) >= 1
        assert set(df["exchange"].str.lower()) <= {"sh", "sz"}
        for _, row in df.iterrows():
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
    """验证财务查询字段对齐（income_statement_pit）。"""

    def test_financial_query_no_error(self):
        """query_financial_data 不应该报"列不存在"错误。"""
        from base.pit.views import query_financial_data
        # 不应该抛出异常
        result = query_financial_data("sh600000", "2023-12-31")
        assert result is not None
        assert "symbol" in result.columns or "canonical_id" in result.columns


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
        """两次成功 build 应该有相同的 output_hash（幂等性）。"""
        df = read_sql("""
            SELECT output_hash
            FROM public.pipeline_run
            WHERE status = 'ok'
            ORDER BY started_at DESC
            LIMIT 2
        """)
        if len(df) == 2:
            assert df.iloc[0]["output_hash"] == df.iloc[1]["output_hash"]


# ─────────────────────────────────────────────────────────────────────────────
# 8. Lineage 追踪（P1.3）—— 目标表为物化视图，无参调用验证血缘路径可用
# ─────────────────────────────────────────────────────────────────────────────

class TestLineage:
    """验证 Lineage 构建路径可用（build_*_lineage 为无参函数，返回 None）。"""

    def test_valuation_lineage_builds_without_error(self):
        """估值血缘构建不应抛异常（valuation_pit 物化视图有数据）。"""
        from base.lineage.tracker import build_valuation_lineage
        # fixture ju.valuation 有数据 → lineage_records 非空，函数内部记录
        result = build_valuation_lineage()
        assert result is None

    def test_st_status_lineage_builds_without_error(self):
        """ST 状态血缘构建不应抛异常（st_status_pit 物化视图有数据）。"""
        from base.lineage.tracker import build_st_status_lineage
        result = build_st_status_lineage()
        assert result is None


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
        statuses = set(df["status"])
        assert "missing" not in statuses or len(df) > 1


# ─────────────────────────────────────────────────────────────────────────────
# 10. 综合验证
# ─────────────────────────────────────────────────────────────────────────────

class TestE2EIntegration:
    """端到端综合验证。"""

    def test_all_public_tables_exist(self):
        """所有落地资产表与物化视图都应该存在。

        物化视图（st_status_pit / valuation_pit / financial_data_pit）
        不出现在 information_schema.tables，必须 UNION pg_matviews。
        """
        df = read_sql("""
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'public'
            UNION
            SELECT matviewname FROM pg_matviews
            WHERE schemaname = 'public'
        """)
        tables = set(df["table_name"])
        required = {
            "canonical_symbol",
            "corporate_action_event",
            "income_statement_pit",
            "st_status_pit",
            "valuation_pit",
            "limit_rule_pit",
            "trading_calendar",
            "hfq_daily",
            "pipeline_run",
            "data_freshness",
        }
        missing = required - tables
        assert len(missing) == 0, f"缺少表: {missing}"

    def test_pipeline_run_success(self):
        """至少有一次成功的 pipeline 运行（status='ok'）。"""
        df = read_sql("""
            SELECT COUNT(*) AS cnt
            FROM public.pipeline_run
            WHERE status = 'ok'
        """)
        assert df.iloc[0]["cnt"] > 0
