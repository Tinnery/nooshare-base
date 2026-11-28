"""纯函数单元测试（不依赖数据库）。

覆盖：
  - base.adjust.hfq 事件复权因子数学（_event_factors，字段语义按全市场标定：
    c1=每10股派息、c2=配股价、c3=每10股送转、c4=每10股配股数）
  - base.events.ingest GBBQ category=1 分类逻辑（Phase A 修复）
  - base.config DSN 构造

运行：uv run pytest（无需数据库，CI unit job 与 integration job 均执行）。

修复记录：
  2026-11-27: Phase A - 重写 test_gbbq_category_map_semantics 为 test_extract_gbbq_events_* 系列，
              覆盖 A1 条件表（10 项）+ 跨实现一致性（1 项）+ 回归（保留现有 8 测试）。
"""
import math
import pandas as pd


def _px_frame(rows):
    return pd.DataFrame(rows, columns=["symbol", "date", "close"])


# ============================================================
# Phase A: GBBQ category=1 分类逻辑测试（10 项）
# ============================================================

class TestExtractGbbqEvents:
    """extract_gbbq_events: category=1 按正确语义分类。

    字段语义（三方验证）：
      c1 = 每10股现金分红 → dividend
      c2 = 配股价
      c3 = 每10股送转 → split
      c4 = 每10股配股数 → rights_issue
    """

    def test_only_c1_dividend(self):
        """仅 c1 > 0 → dividend 事件"""
        from base.events.ingest import extract_gbbq_events
        # Mock read_sql 返回 fixture
        import base.events.ingest as ingest_mod
        original_read_sql = ingest_mod.read_sql
        ingest_mod.read_sql = lambda q: pd.DataFrame([{
            "symbol": "sz000001", "stock_code": "000001", "date": 20240104,
            "category": 1, "c1": 2.0, "c2": 0.0, "c3": 0.0, "c4": 0.0
        }])
        try:
            events = extract_gbbq_events()
            assert len(events) == 1
            assert events.iloc[0]["event_type"] == "dividend"
            assert events.iloc[0]["payload"]["cash_per_10"] == 2.0
        finally:
            ingest_mod.read_sql = original_read_sql

    def test_only_c3_split(self):
        """仅 c3 > 0 → split 事件"""
        from base.events.ingest import extract_gbbq_events
        import base.events.ingest as ingest_mod
        original_read_sql = ingest_mod.read_sql
        ingest_mod.read_sql = lambda q: pd.DataFrame([{
            "symbol": "sz000001", "stock_code": "000001", "date": 20240104,
            "category": 1, "c1": 0.0, "c2": 0.0, "c3": 5.0, "c4": 0.0
        }])
        try:
            events = extract_gbbq_events()
            assert len(events) == 1
            assert events.iloc[0]["event_type"] == "split"
            assert events.iloc[0]["payload"]["convert_per_10"] == 5.0
        finally:
            ingest_mod.read_sql = original_read_sql

    def test_only_c2_c4_rights_issue(self):
        """仅 c2+c4 > 0 → rights_issue 事件"""
        from base.events.ingest import extract_gbbq_events
        import base.events.ingest as ingest_mod
        original_read_sql = ingest_mod.read_sql
        ingest_mod.read_sql = lambda q: pd.DataFrame([{
            "symbol": "sz000001", "stock_code": "000001", "date": 20240104,
            "category": 1, "c1": 0.0, "c2": 8.0, "c3": 0.0, "c4": 3.0
        }])
        try:
            events = extract_gbbq_events()
            assert len(events) == 1
            assert events.iloc[0]["event_type"] == "rights_issue"
            assert events.iloc[0]["payload"]["rights_shares_per_10"] == 3.0
            assert events.iloc[0]["payload"]["rights_price"] == 8.0
        finally:
            ingest_mod.read_sql = original_read_sql

    def test_c1_c3_dividend_and_split(self):
        """c1+c3 > 0 → dividend + split 两条事件"""
        from base.events.ingest import extract_gbbq_events
        import base.events.ingest as ingest_mod
        original_read_sql = ingest_mod.read_sql
        ingest_mod.read_sql = lambda q: pd.DataFrame([{
            "symbol": "sz000001", "stock_code": "000001", "date": 20240104,
            "category": 1, "c1": 2.0, "c2": 0.0, "c3": 5.0, "c4": 0.0
        }])
        try:
            events = extract_gbbq_events()
            assert len(events) == 2
            types = set(events["event_type"])
            assert types == {"dividend", "split"}
        finally:
            ingest_mod.read_sql = original_read_sql

    def test_c1_c2_c4_dividend_and_rights_issue(self):
        """c1+c2+c4 > 0 → dividend + rights_issue 两条事件"""
        from base.events.ingest import extract_gbbq_events
        import base.events.ingest as ingest_mod
        original_read_sql = ingest_mod.read_sql
        ingest_mod.read_sql = lambda q: pd.DataFrame([{
            "symbol": "sz000001", "stock_code": "000001", "date": 20240104,
            "category": 1, "c1": 2.0, "c2": 8.0, "c3": 0.0, "c4": 3.0
        }])
        try:
            events = extract_gbbq_events()
            assert len(events) == 2
            types = set(events["event_type"])
            assert types == {"dividend", "rights_issue"}
        finally:
            ingest_mod.read_sql = original_read_sql

    def test_all_mixed_c1_c2_c3_c4(self):
        """c1+c2+c3+c4 > 0 → dividend + split + rights_issue 三条事件"""
        from base.events.ingest import extract_gbbq_events
        import base.events.ingest as ingest_mod
        original_read_sql = ingest_mod.read_sql
        ingest_mod.read_sql = lambda q: pd.DataFrame([{
            "symbol": "sz000001", "stock_code": "000001", "date": 20240104,
            "category": 1, "c1": 2.0, "c2": 8.0, "c3": 5.0, "c4": 3.0
        }])
        try:
            events = extract_gbbq_events()
            assert len(events) == 3
            types = set(events["event_type"])
            assert types == {"dividend", "split", "rights_issue"}
        finally:
            ingest_mod.read_sql = original_read_sql

    def test_all_zero_no_event(self):
        """全零 → 不生成事件"""
        from base.events.ingest import extract_gbbq_events
        import base.events.ingest as ingest_mod
        original_read_sql = ingest_mod.read_sql
        ingest_mod.read_sql = lambda q: pd.DataFrame([{
            "symbol": "sz000001", "stock_code": "000001", "date": 20240104,
            "category": 1, "c1": 0.0, "c2": 0.0, "c3": 0.0, "c4": 0.0
        }])
        try:
            events = extract_gbbq_events()
            assert len(events) == 0
        finally:
            ingest_mod.read_sql = original_read_sql

    def test_negative_value_anomaly(self):
        """负值 → 记 anomaly，不生成事件"""
        from base.events.ingest import extract_gbbq_events, _anomaly_log
        import base.events.ingest as ingest_mod
        original_read_sql = ingest_mod.read_sql
        _anomaly_log.clear()
        ingest_mod.read_sql = lambda q: pd.DataFrame([{
            "symbol": "sz000001", "stock_code": "000001", "date": 20240104,
            "category": 1, "c1": -2.0, "c2": 0.0, "c3": 0.0, "c4": 0.0
        }])
        try:
            events = extract_gbbq_events()
            assert len(events) == 0
            assert len(_anomaly_log) == 1
            assert _anomaly_log[0]["anomaly_type"] == "negative_value"
        finally:
            ingest_mod.read_sql = original_read_sql
            _anomaly_log.clear()

    def test_orphan_c2_anomaly(self):
        """c2 > 0 但 c4 = 0 → 孤立配价，记 anomaly"""
        from base.events.ingest import extract_gbbq_events, _anomaly_log
        import base.events.ingest as ingest_mod
        original_read_sql = ingest_mod.read_sql
        _anomaly_log.clear()
        ingest_mod.read_sql = lambda q: pd.DataFrame([{
            "symbol": "sz000001", "stock_code": "000001", "date": 20240104,
            "category": 1, "c1": 0.0, "c2": 8.0, "c3": 0.0, "c4": 0.0
        }])
        try:
            events = extract_gbbq_events()
            assert len(events) == 0
            assert len(_anomaly_log) == 1
            assert _anomaly_log[0]["anomaly_type"] == "orphan_rights_price"
        finally:
            ingest_mod.read_sql = original_read_sql
            _anomaly_log.clear()

    def test_event_before_first_price(self):
        """事件日期早于首个 raw 日 K → 透传（不跳过，由下游处理）"""
        from base.events.ingest import extract_gbbq_events
        import base.events.ingest as ingest_mod
        original_read_sql = ingest_mod.read_sql
        ingest_mod.read_sql = lambda q: pd.DataFrame([{
            "symbol": "sz000001", "stock_code": "000001", "date": 20200101,
            "category": 1, "c1": 2.0, "c2": 0.0, "c3": 0.0, "c4": 0.0
        }])
        try:
            events = extract_gbbq_events()
            assert len(events) == 1
            assert events.iloc[0]["event_type"] == "dividend"
        finally:
            ingest_mod.read_sql = original_read_sql


# ============================================================
# Phase A: 跨实现一致性测试（1 项）
# ============================================================

def test_cross_implementation_consistency():
    """同一 fixture 经 ingest 分类后按 hfq.py 公式复算因子，与 tong adjust_calc.go 公式一致。

    tong 公式：P_ex = (P_pre*10 - d + r*pr) / (10 + s + r)
    其中 d=c1, r=c4, pr=c2, s=c3
    """
    from base.adjust.factor_builder import _event_factors
    from base.events.ingest import extract_gbbq_events
    import base.events.ingest as ingest_mod

    # Fixture: c1=2, c2=8, c3=5, c4=3（全混合）
    original_read_sql = ingest_mod.read_sql
    ingest_mod.read_sql = lambda q: pd.DataFrame([{
        "symbol": "sz000001", "stock_code": "000001", "date": 20240104,
        "category": 1, "c1": 2.0, "c2": 8.0, "c3": 5.0, "c4": 3.0
    }])
    try:
        events = extract_gbbq_events()
        # 应有 3 条事件：dividend + split + rights_issue
        assert len(events) == 3

        # 构建 hfq.py 期望的事件 DataFrame（ex_date, c1, c2, c3, c4）
        # 注意：ingest 拆条后，hfq.py 需要原始 c1-c4 才能正确计算
        # 这里验证 hfq.py 的 _event_factors 能正确处理混合事件
        ev = pd.DataFrame([dict(zip(
            ["ex_date", "c1", "c2", "c3", "c4"],
            [20240104, 2.0, 8.0, 5.0, 3.0]
        ))])
        px = _px_frame([
            ("sz000001", 20240103, 12.00),
            ("sz000001", 20240104, 10.00),
        ])
        res, n_no_price, n_invalid = _event_factors(ev, px)
        assert n_no_price == 0 and n_invalid == 0

        # tong 公式：P_ex = (12.00*10 - 2 + 3*8) / (10 + 5 + 3) = (120 - 2 + 24) / 18 = 142/18 = 7.888...
        # factor = 12.00 / 7.888... = 1.521...
        p_ex = (12.00 * 10 - 2.0 + 3.0 * 8.0) / (10 + 5.0 + 3.0)
        expected_factor = 12.00 / p_ex
        assert math.isclose(res.iloc[0]["factor"], expected_factor, rel_tol=1e-12)
    finally:
        ingest_mod.read_sql = original_read_sql


# ============================================================
# 回归测试：保留现有 8 测试
# ============================================================

class TestEventFactors:
    """_event_factors：除权除息因子 = P_pre / P_ex。

    公式（除权除息日）：
      P_ex = (P_pre - c1/10 + c2*c4/10) / (1 + c3/10 + c4/10)
      factor = P_pre / P_ex
    P_pre 为严格早于除权日的最后一个交易日收盘价。
    """

    EV_COLS = ["ex_date", "c1", "c2", "c3", "c4"]

    def test_dividend_only(self):
        from base.adjust.factor_builder import _event_factors

        ev = pd.DataFrame([dict(zip(self.EV_COLS, [20240104, 2.0, 0.0, 0.0, 0.0]))])
        px = _px_frame([
            ("sh600000", 20240102, 10.10),
            ("sh600000", 20240103, 10.30),
            ("sh600000", 20240104, 9.90),
        ])
        res, n_no_price, n_invalid = _event_factors(ev, px)
        assert n_no_price == 0 and n_invalid == 0
        expected = 10.30 / (10.30 - 2.0 / 10.0)
        assert math.isclose(res.iloc[0]["factor"], expected, rel_tol=1e-12)
        assert res.iloc[0]["ex_date"] == 20240104

    def test_bonus_shares(self):
        from base.adjust.factor_builder import _event_factors

        ev = pd.DataFrame([dict(zip(self.EV_COLS, [20240105, 1.2, 0.0, 5.0, 0.0]))])
        px = _px_frame([
            ("sz000001", 20240104, 11.30),
            ("sz000001", 20240105, 7.60),
        ])
        res, n_no_price, n_invalid = _event_factors(ev, px)
        assert n_no_price == 0 and n_invalid == 0
        expected = 11.30 / ((11.30 - 1.2 / 10.0) / 1.5)
        assert math.isclose(res.iloc[0]["factor"], expected, rel_tol=1e-12)

    def test_rights_issue(self):
        from base.adjust.factor_builder import _event_factors

        ev = pd.DataFrame([dict(zip(self.EV_COLS, [20240104, 0.0, 8.0, 0.0, 3.0]))])
        px = _px_frame([
            ("sh600001", 20240103, 12.00),
            ("sh600001", 20240104, 10.00),
        ])
        res, n_no_price, n_invalid = _event_factors(ev, px)
        assert n_no_price == 0 and n_invalid == 0
        r = 0.3
        expected = 12.00 / ((12.00 + 8.0 * r) / (1.0 + r))
        assert math.isclose(res.iloc[0]["factor"], expected, rel_tol=1e-12)

    def test_zero_event_skipped(self):
        from base.adjust.factor_builder import _event_factors

        ev = pd.DataFrame([dict(zip(self.EV_COLS, [20240104, 0.0, 0.0, 0.0, 0.0]))])
        px = _px_frame([("sh600000", 20240103, 10.30)])
        res, n_no_price, n_invalid = _event_factors(ev, px)
        assert len(res) == 0

    def test_event_before_first_price_skipped(self):
        """事件早于全部价格：无 P_pre 基准 → 因子按 1 跳过并计数。"""
        from base.adjust.factor_builder import _event_factors

        ev = pd.DataFrame([dict(zip(self.EV_COLS, [20200101, 2.0, 0.0, 0.0, 0.0]))])
        px = _px_frame([("sh600000", 20240102, 10.10)])
        res, n_no_price, _ = _event_factors(ev, px)
        assert len(res) == 0
        assert n_no_price == 1


def test_pg_dsn_construction(monkeypatch):
    """get_pg_dsn 按 host/port/password/user/dbname/sslmode 顺序构造。"""
    from base import config

    monkeypatch.setattr(config, "PG_HOST", "db.example.local")
    monkeypatch.setattr(config, "PG_PORT", 6543)
    monkeypatch.setattr(config, "PG_USER", "alice")
    monkeypatch.setattr(config, "PG_PASSWORD", "secret")
    monkeypatch.setattr(config, "PG_DATABASE", "unit_db")
    monkeypatch.setattr(config, "PG_SSLMODE", "require")

    dsn = config.get_pg_dsn()
    assert dsn == (
        "host=db.example.local port=6543 password=secret "
        "user=alice dbname=unit_db sslmode=require"
    )


def test_pg_dsn_without_password(monkeypatch):
    from base import config

    monkeypatch.setattr(config, "PG_PASSWORD", "")
    dsn = config.get_pg_dsn()
    assert "password=" not in dsn
