"""V10 validation semantics tests (round-4 closure, §8).

Contract: only a genuinely-passing check yields PASS. Missing relations,
missing columns and broken queries all preserve the real failure state and
FAIL the gate — nothing may degrade to a passing NOT_APPLICABLE.
"""

from unittest.mock import patch as mock_patch

import pandas as pd
import pytest
from psycopg2.errors import UndefinedColumn, UndefinedTable

from base.validate.suite import validate_no_lookahead_bias


def _ok_read_sql(sql, params=None):
    return pd.DataFrame({"cnt": [0]})


def test_v10_pass_when_no_violations():
    with mock_patch.object(
        __import__("base.validate.suite", fromlist=["read_sql"]),
        "read_sql",
        side_effect=_ok_read_sql,
    ):
        result = validate_no_lookahead_bias(as_of="20260101")
    assert result["status"] == "PASS"
    assert result["passed"] is True


def test_v10_missing_relation_fails_not_applicable():
    """A missing required materialized view FAILS the gate instead of
    degrading to a passing NOT_APPLICABLE."""
    calls = {"n": 0}

    def side_effect(sql, params=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise UndefinedTable(
                "relation public.st_status_pit does not exist"
            )
        return pd.DataFrame({"cnt": [0]})

    suite_mod = __import__("base.validate.suite", fromlist=["read_sql"])
    with mock_patch.object(suite_mod, "read_sql", side_effect=side_effect):
        result = validate_no_lookahead_bias(as_of="20260101")
    assert result["status"] == "FAIL"
    assert result["passed"] is False
    assert result["violation_count"] >= 1
    assert result["evidence"]["checks"]["st_status_pit"]["error"] != ""


def test_v10_missing_column_fails():
    """UndefinedColumn (42703) must NOT be treated as a missing relation /
    NOT_APPLICABLE: a broken column is a schema defect."""
    suite_mod = __import__("base.validate.suite", fromlist=["read_sql"])
    with mock_patch.object(
        suite_mod, "read_sql",
        side_effect=UndefinedColumn,
    ):
        result = validate_no_lookahead_bias(as_of="20260101")
    assert result["status"] == "FAIL"
    assert result["passed"] is False
    assert result["evidence"]["checks"]["st_status_pit"]["sqlstate_missing_relation"] is False


def test_v10_real_violations_fail():
    def side_effect(sql, params=None):
        return pd.DataFrame({"cnt": [7]})

    suite_mod = __import__("base.validate.suite", fromlist=["read_sql"])
    with mock_patch.object(suite_mod, "read_sql", side_effect=side_effect):
        result = validate_no_lookahead_bias(as_of="20260101")
    assert result["status"] == "FAIL"
    assert result["violation_count"] == 14  # 7 + 7 from both checks


def test_v10_one_check_fails_one_succeeds_fails():
    """A half-broken gate must not pass: any failed check blocks PASS."""
    calls = {"n": 0}

    def side_effect(sql, params=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return pd.DataFrame({"cnt": [0]})  # st check ok
        raise UndefinedTable(
            "relation public.valuation_pit does not exist"
        )  # valuation missing

    suite_mod = __import__("base.validate.suite", fromlist=["read_sql"])
    with mock_patch.object(suite_mod, "read_sql", side_effect=side_effect):
        result = validate_no_lookahead_bias(as_of="20260101")
    assert result["status"] == "FAIL"
    assert result["passed"] is False


def test_v10_no_not_applicable_status_possible():
    """The NOT_APPLICABLE escape hatch is gone from V10: no status assignment
    and no check-entry can produce it anymore."""
    source = inspect_v10_source()
    assert 'status = "NOT_APPLICABLE"' not in source
    assert '{"status": "NOT_APPLICABLE"' not in source


def inspect_v10_source() -> str:
    import inspect
    return inspect.getsource(validate_no_lookahead_bias)
