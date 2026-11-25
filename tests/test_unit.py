"""纯函数单元测试（不依赖数据库）。

覆盖：
  - base.adjust.hfq 事件复权因子数学（_event_factors，字段语义按全市场标定：
    c1=每10股派息、c2=配股价、c3=每10股送转、c4=每10股配股数）
  - base.events.ingest GBBQ_CATEGORY_MAP 语义
  - base.config DSN 构造

运行：uv run pytest（无需数据库，CI unit job 与 integration job 均执行）。
"""
import math

import pandas as pd


def _px_frame(rows):
    return pd.DataFrame(rows, columns=["symbol", "date", "close"])


def test_gbbq_category_map_semantics():
    from base.events.ingest import GBBQ_CATEGORY_MAP

    assert GBBQ_CATEGORY_MAP[2] == "rights_issue"
    for cat in (3, 4, 5):
        assert GBBQ_CATEGORY_MAP[cat] == "shares_change"


class TestEventFactors:
    """_event_factors：除权除息因子 = P_pre / P_ex。

    公式（除权除息日）：
      P_ex = (P_pre - c1/10 + c2*c4/10) / (1 + c3/10 + c4/10)
      factor = P_pre / P_ex
    P_pre 为严格早于除权日的最后一个交易日收盘价。
    """

    EV_COLS = ["ex_date", "c1", "c2", "c3", "c4"]

    def test_dividend_only(self):
        from base.adjust.hfq import _event_factors

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
        from base.adjust.hfq import _event_factors

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
        from base.adjust.hfq import _event_factors

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
        from base.adjust.hfq import _event_factors

        ev = pd.DataFrame([dict(zip(self.EV_COLS, [20240104, 0.0, 0.0, 0.0, 0.0]))])
        px = _px_frame([("sh600000", 20240103, 10.30)])
        res, n_no_price, n_invalid = _event_factors(ev, px)
        assert len(res) == 0

    def test_event_before_first_price_skipped(self):
        """事件早于全部价格：无 P_pre 基准 → 因子按 1 跳过并计数。"""
        from base.adjust.hfq import _event_factors

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


class TestCanonicalSymbolExchangeResolution:
    """exchange 字段解析回归测试（修复 KeyError: 'exchange'）。
    
    场景：ju.stocks ⋈ tong.symbol_meta 后，exchange 列变成 exchange_ju / exchange_tong。
    规则：
      - ju.stocks.exchange 为权威源（sh/sz/bj 小写）
      - tong.symbol_meta 推导的 exchange 为交叉验证（XSHG/XSHE/BSE 大写 → 映射为小写）
      - ju 优先；ju 缺失时回退 tong；两源均缺失或不可识别 → 返回 None
      - 冲突时记录警告但不静默丢弃
      - 未知交易所不能默认归入 sz
    """

    def test_ju_authority_sh(self):
        """ju 有值 sh → 输出 sh"""
        from base.symbol.master import build_canonical_symbol
        import base.symbol.master as mod
        
        # Mock 数据
        ju_df = pd.DataFrame({
            'stock_code': ['600000'],
            'display_name': ['浦发银行'],
            'exchange': ['sh'],
            'start_date': [20200101],
            'end_date': [None],
        })
        tong_df = pd.DataFrame({
            'symbol': ['sh600000'],
            'class': ['stock'],
        })
        
        # Mock DB 调用
        original_read_sql = mod.read_sql
        call_count = [0]
        def mock_read_sql(sql):
            call_count[0] += 1
            if call_count[0] == 1:
                return ju_df
            return tong_df
        
        mod.read_sql = mock_read_sql
        
        # Mock copy_upsert 捕获结果
        captured = {}
        def mock_upsert(df, schema, table):
            captured['df'] = df.copy()
        mod.copy_upsert = mock_upsert
        
        try:
            build_canonical_symbol()
            result = captured['df']
            assert len(result) == 1
            assert result.iloc[0]['exchange'] == 'sh'
            assert result.iloc[0]['symbol'] == 'sh600000'
            assert result.iloc[0]['canonical_id'] == '600000'
        finally:
            mod.read_sql = original_read_sql
            mod.copy_upsert = lambda *a, **k: None

    def test_ju_authority_sz(self):
        """ju 有值 sz → 输出 sz"""
        from base.symbol.master import build_canonical_symbol
        import base.symbol.master as mod
        
        ju_df = pd.DataFrame({
            'stock_code': ['000001'],
            'display_name': ['平安银行'],
            'exchange': ['sz'],
            'start_date': [20200101],
            'end_date': [None],
        })
        tong_df = pd.DataFrame({
            'symbol': ['sz000001'],
            'class': ['stock'],
        })
        
        original_read_sql = mod.read_sql
        call_count = [0]
        def mock_read_sql(sql):
            call_count[0] += 1
            if call_count[0] == 1:
                return ju_df
            return tong_df
        mod.read_sql = mock_read_sql
        
        captured = {}
        def mock_upsert(df, schema, table):
            captured['df'] = df.copy()
        mod.copy_upsert = mock_upsert
        
        try:
            build_canonical_symbol()
            result = captured['df']
            assert result.iloc[0]['exchange'] == 'sz'
            assert result.iloc[0]['symbol'] == 'sz000001'
        finally:
            mod.read_sql = original_read_sql
            mod.copy_upsert = lambda *a, **k: None

    def test_ju_authority_bj(self):
        """ju 有值 bj → 输出 bj"""
        from base.symbol.master import build_canonical_symbol
        import base.symbol.master as mod
        
        ju_df = pd.DataFrame({
            'stock_code': ['830799'],
            'display_name': ['北交所股票'],
            'exchange': ['bj'],
            'start_date': [20200101],
            'end_date': [None],
        })
        tong_df = pd.DataFrame({
            'symbol': ['bj830799'],
            'class': ['stock'],
        })
        
        original_read_sql = mod.read_sql
        call_count = [0]
        def mock_read_sql(sql):
            call_count[0] += 1
            if call_count[0] == 1:
                return ju_df
            return tong_df
        mod.read_sql = mock_read_sql
        
        captured = {}
        def mock_upsert(df, schema, table):
            captured['df'] = df.copy()
        mod.copy_upsert = mock_upsert
        
        try:
            build_canonical_symbol()
            result = captured['df']
            assert result.iloc[0]['exchange'] == 'bj'
            assert result.iloc[0]['symbol'] == 'bj830799'
        finally:
            mod.read_sql = original_read_sql
            mod.copy_upsert = lambda *a, **k: None

    def test_ju_missing_fallback_to_tong(self):
        """ju 缺失 → 回退 tong 推导"""
        from base.symbol.master import build_canonical_symbol
        import base.symbol.master as mod
        
        ju_df = pd.DataFrame({
            'stock_code': ['600001'],
            'display_name': ['测试股票'],
            'exchange': [None],  # ju 缺失
            'start_date': [20200101],
            'end_date': [None],
        })
        tong_df = pd.DataFrame({
            'symbol': ['sh600001'],  # tong 推导 XSHG → sh
            'class': ['stock'],
        })
        
        original_read_sql = mod.read_sql
        call_count = [0]
        def mock_read_sql(sql):
            call_count[0] += 1
            if call_count[0] == 1:
                return ju_df
            return tong_df
        mod.read_sql = mock_read_sql
        
        captured = {}
        def mock_upsert(df, schema, table):
            captured['df'] = df.copy()
        mod.copy_upsert = mock_upsert
        
        try:
            build_canonical_symbol()
            result = captured['df']
            assert len(result) == 1
            assert result.iloc[0]['exchange'] == 'sh'  # 从 tong 回退
            assert result.iloc[0]['symbol'] == 'sh600001'
        finally:
            mod.read_sql = original_read_sql
            mod.copy_upsert = lambda *a, **k: None

    def test_both_missing_drops_row(self):
        """两源均缺失 → 丢弃该行，不默认归入 sz"""
        from base.symbol.master import build_canonical_symbol
        import base.symbol.master as mod
        
        ju_df = pd.DataFrame({
            'stock_code': ['999999'],
            'display_name': ['未知股票'],
            'exchange': [None],
            'start_date': [20200101],
            'end_date': [None],
        })
        tong_df = pd.DataFrame({
            'symbol': pd.Series([], dtype='object'),  # 显式指定 dtype
            'class': pd.Series([], dtype='object'),
        })
        
        original_read_sql = mod.read_sql
        call_count = [0]
        def mock_read_sql(sql):
            call_count[0] += 1
            if call_count[0] == 1:
                return ju_df
            return tong_df
        mod.read_sql = mock_read_sql
        
        captured = {}
        def mock_upsert(df, schema, table):
            captured['df'] = df.copy()
        mod.copy_upsert = mock_upsert
        
        try:
            build_canonical_symbol()
            result = captured['df']
            # 应该被丢弃
            assert len(result) == 0
        finally:
            mod.read_sql = original_read_sql
            mod.copy_upsert = lambda *a, **k: None

    def test_unknown_exchange_not_silently_mapped(self):
        """未知交易所值 → 丢弃，不默认归入 sz"""
        from base.symbol.master import build_canonical_symbol
        import base.symbol.master as mod
        
        ju_df = pd.DataFrame({
            'stock_code': ['888888'],
            'display_name': ['异常股票'],
            'exchange': ['UNKNOWN'],  # 不可识别
            'start_date': [20200101],
            'end_date': [None],
        })
        tong_df = pd.DataFrame({
            'symbol': pd.Series([], dtype='object'),
            'class': pd.Series([], dtype='object'),
        })
        
        original_read_sql = mod.read_sql
        call_count = [0]
        def mock_read_sql(sql):
            call_count[0] += 1
            if call_count[0] == 1:
                return ju_df
            return tong_df
        mod.read_sql = mock_read_sql
        
        captured = {}
        def mock_upsert(df, schema, table):
            captured['df'] = df.copy()
        mod.copy_upsert = mock_upsert
        
        try:
            build_canonical_symbol()
            result = captured['df']
            # UNKNOWN 不可识别，应被丢弃
            assert len(result) == 0
        finally:
            mod.read_sql = original_read_sql
            mod.copy_upsert = lambda *a, **k: None

    def test_exchange_conflict_logged_but_ju_wins(self):
        """ju 和 tong 冲突 → ju 优先，冲突被记录"""
        from base.symbol.master import build_canonical_symbol
        import base.symbol.master as mod
        
        ju_df = pd.DataFrame({
            'stock_code': ['600000'],
            'display_name': ['浦发银行'],
            'exchange': ['sh'],  # ju 说 sh
            'start_date': [20200101],
            'end_date': [None],
        })
        tong_df = pd.DataFrame({
            'symbol': ['sz600000'],  # tong 说 sz（冲突）
            'class': ['stock'],
        })
        
        original_read_sql = mod.read_sql
        call_count = [0]
        def mock_read_sql(sql):
            call_count[0] += 1
            if call_count[0] == 1:
                return ju_df
            return tong_df
        mod.read_sql = mock_read_sql
        
        captured = {}
        def mock_upsert(df, schema, table):
            captured['df'] = df.copy()
        mod.copy_upsert = mock_upsert
        
        try:
            build_canonical_symbol()
            result = captured['df']
            # ju 优先，输出 sh
            assert len(result) == 1
            assert result.iloc[0]['exchange'] == 'sh'
            assert result.iloc[0]['symbol'] == 'sh600000'
        finally:
            mod.read_sql = original_read_sql
            mod.copy_upsert = lambda *a, **k: None
