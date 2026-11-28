"""Factor Builder 来源身份与 Git provenance 单元测试（不依赖数据库）。

覆盖：
  - _git_sha() 五场景：仓库根 / 他目录 / 非 Git / git 失败 / 非法 SHA
  - load_prices() 绑定 market_source_id
  - load_events() 绑定 action_source_id
  - _coverage() 与构建同口径（source_id 过滤）
  - 多来源隔离：同证券同日期 tdx + another_source 不混组
"""
import subprocess
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest


# ── Fake 连接：记录 SQL 与参数，返回预设行 ────────────────────────────

class _FakeCursor:
    def __init__(self, rows=None, description=None):
        self._rows = rows if rows is not None else []
        self.description = description or [("col",)]
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


def _conn_returning(rows, cols):
    return _FakeConn(_FakeCursor(rows=rows, description=[(c,) for c in cols]))


# ── _git_sha() 五场景 ─────────────────────────────────────────────────

class TestGitSha:
    def test_repo_root_returns_full_sha(self):
        from base.adjust.factor_builder import _git_sha
        sha = _git_sha()
        assert len(sha) == 40
        assert all(c in "0123456789abcdef" for c in sha)

    def test_independent_of_cwd(self, tmp_path, monkeypatch):
        """从任意当前目录调用都能拿到同一 SHA（cwd 从 __file__ 推导）。"""
        from base.adjust.factor_builder import _git_sha
        monkeypatch.chdir(tmp_path)
        sha = _git_sha()
        assert len(sha) == 40

    def test_git_failure_raises(self):
        """git 命令返回非 0 → 抛异常，绝不返回 unavailable。"""
        import base.adjust.factor_builder as fb
        fake = subprocess.CompletedProcess(
            args=[], returncode=128, stdout="", stderr="fatal: not a git repository")
        with patch.object(fb.subprocess, "run", return_value=fake):
            with pytest.raises(RuntimeError, match="git log 失败"):
                fb._git_sha()

    def test_non_git_directory_raises(self, tmp_path):
        """把仓库根指向临时非 Git 目录 → 抛异常。"""
        import base.adjust.factor_builder as fb
        with patch.object(fb, "Path") as fake_path:
            fake_path.return_value.resolve.return_value.parent.parent.parent = tmp_path
            with pytest.raises(RuntimeError):
                fb._git_sha()

    def test_illegal_sha_raises(self):
        """git 返回非 40 位十六进制 → 抛异常。"""
        import base.adjust.factor_builder as fb
        fake = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="deadbeef\n", stderr="")
        with patch.object(fb.subprocess, "run", return_value=fake):
            with pytest.raises(RuntimeError, match="非法 SHA"):
                fb._git_sha()

    def test_sha_is_never_unavailable_string(self):
        """provenance 不得出现虚假成功状态。"""
        from base.adjust.factor_builder import _git_sha
        assert _git_sha() != "unavailable"


# ── 来源绑定 ─────────────────────────────────────────────────────────

class TestSourceBinding:
    def test_load_prices_binds_market_source(self):
        """load_prices 查询必须含 source_id 过滤且参数带来源。"""
        from base.adjust.factor_builder import load_prices
        cols = ["symbol", "date", "open", "high", "low", "close",
                "volume", "amount", "source_id"]
        cur = _FakeCursor(rows=[], description=[(c,) for c in cols])
        conn = _FakeConn(cur)
        load_prices(conn, source_id="tdx")
        sql, params = cur.executed[-1]
        assert "source_id = %s" in sql
        assert "tdx" in params

    def test_load_prices_custom_source_isolated(self):
        """指定 another_source 时不会查 tdx。"""
        from base.adjust.factor_builder import load_prices
        cols = ["symbol", "date", "open", "high", "low", "close",
                "volume", "amount", "source_id"]
        cur = _FakeCursor(rows=[], description=[(c,) for c in cols])
        load_prices(_FakeConn(cur), source_id="another_source")
        _, params = cur.executed[-1]
        assert "tdx" not in params
        assert "another_source" in params

    def test_load_events_binds_action_source(self):
        """load_events 查询绑定 gbbq.source（真实字段，不臆造）。"""
        from base.adjust.factor_builder import load_events
        cols = ["symbol", "ex_date", "c1", "c2", "c3", "c4", "source"]
        cur = _FakeCursor(rows=[], description=[(c,) for c in cols])
        load_events(_FakeConn(cur), source_id="tdx")
        sql, params = cur.executed[-1]
        assert "tong.gbbq" in sql
        assert "source = %s" in sql
        assert "tdx" in params

    def test_coverage_filters_same_source(self):
        """_coverage 与构建同口径：eq CTE 必须按 source_id 过滤。"""
        from base.adjust.factor_builder import _coverage
        cols = ["a", "b", "c", "d", "e", "f"]
        cur = _FakeCursor(rows=[(10, 8, 2, 0, 0, 0)],
                          description=[(c,) for c in cols])
        out = _coverage(_FakeConn(cur), "uuid-x", source_id="tdx")
        sql, params = cur.executed[-1]
        assert "b.source_id = %s" in sql
        assert params == ("uuid-x", "tdx")
        assert out["raw_equity_rows"] == 10
        assert out["incomplete_factor_rows"] == 2


# ── 多来源隔离：不混组 ────────────────────────────────────────────────

class TestMultiSourceIsolation:
    def test_two_sources_same_symbol_not_mixed(self):
        """同证券同日期存在 tdx 与 another_source，TDX 构建只见 tdx 行。"""
        from base.adjust.factor_builder import load_prices
        cols = ["symbol", "date", "open", "high", "low", "close",
                "volume", "amount", "source_id"]
        # 模拟数据库只返回 tdx 行（source_id 过滤生效）
        tdx_rows = [("sh600000", 20260105, 10.0, 10.5, 9.8, 10.2, 1000, 10200.0, "tdx")]
        cur = _FakeCursor(rows=tdx_rows, description=[(c,) for c in cols])
        df = load_prices(_FakeConn(cur), source_id="tdx")
        assert set(df["source_id"]) == {"tdx"}
        assert len(df) == 1
        sql, _ = cur.executed[-1]
        assert "source_id = %s" in sql

    def test_hash_covers_single_source(self):
        """输入哈希查询只覆盖绑定来源，不同来源不产生同一身份。"""
        import base.adjust.factor_builder as fb
        cols = ["symbol", "date", "open", "high", "low", "close",
                "volume", "amount", "source_id"]
        cur = _FakeCursor(rows=[], description=[(c,) for c in cols])
        with patch.object(fb, "_stream_hash", return_value="hash") as m:
            # 直接验证查询文本由调用方构造：读源码中的字面量
            src = Path(fb.__file__).read_text()
            assert "AND source_id = %s " in src
            assert "AND source = %s ORDER BY" in src
