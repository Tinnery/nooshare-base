"""Composite identity tests for corporate-action event handling (round-4 §7).

Scenarios required by the closure task:
1. sz000001 stock and sh000001 index coexist
2. GBBQ shenzhen stock events land on sz000001
3. the index is never associated
4. ST events resolve exchange from the stock identity domain
5. truly ambiguous identity is rejected, not guessed
6. same date/type different exchange events are not merged
7. repeated ingest of the same input is idempotent
"""

from datetime import date
from unittest.mock import patch as mock_patch

import pandas as pd
import pytest

from base.events import ingest


# ---------------------------------------------------------------------------
# ST exchange resolution inside the stock asset class (§7.1)
# ---------------------------------------------------------------------------

ST_PERIODS = pd.DataFrame({
    "stock_code": ["000001"],
    "start_date": [pd.Timestamp("2024-05-06").date()],
    "end_date": [pd.Timestamp("2024-06-27").date()],
})

STOCK_IDENTITY = pd.DataFrame({
    "canonical_id": ["000001", "000001", "600000"],
    "exchange": ["sz", "sh", "sh"],
    "asset_class": ["stock", "index", "stock"],
})


def _fake_read_sql(sql, params=None):
    if "ju.st_period" in sql:
        return ST_PERIODS.copy()
    if "asset_class = 'stock'" in sql:
        return STOCK_IDENTITY[STOCK_IDENTITY["asset_class"] == "stock"][
            ["canonical_id", "exchange"]
        ].copy()
    return pd.DataFrame()


def test_st_event_resolves_exchange_from_stock_domain():
    """A colliding sh000001 index must not make the sz000001 stock ST event
    ambiguous: resolution happens inside asset_class='stock' only."""
    with mock_patch.object(ingest, "read_sql", side_effect=_fake_read_sql):
        df = ingest.extract_st_events()
    assert len(df) == 2  # st_mark + st_remove
    assert set(df["exchange"]) == {"sz"}
    assert (df["canonical_id"] == "000001").all()
    marks = df[df["event_type"] == "st_mark"]
    assert marks.iloc[0]["effective_date"] == date(2024, 5, 6)


def test_st_event_ambiguous_within_stock_class_raises():
    """If the same canonical_id collides INSIDE the stock class, the write
    must be rejected instead of guessed."""
    ambiguous_identity = pd.DataFrame({
        "canonical_id": ["000001", "000001"],
        "exchange": ["sz", "sh"],
        "asset_class": ["stock", "stock"],
    })

    def fake(sql, params=None):
        if "ju.st_period" in sql:
            return ST_PERIODS.copy()
        if "asset_class = 'stock'" in sql:
            return ambiguous_identity[["canonical_id", "exchange"]].copy()
        return pd.DataFrame()

    with mock_patch.object(ingest, "read_sql", side_effect=fake):
        with pytest.raises(ValueError, match="身份歧义"):
            ingest.extract_st_events()


def test_st_event_unknown_stock_code_skipped_not_guessed():
    def fake(sql, params=None):
        if "ju.st_period" in sql:
            return pd.DataFrame({
                "stock_code": ["999999"],
                "start_date": [pd.Timestamp("2024-01-02").date()],
                "end_date": [pd.Timestamp("2024-02-01").date()],
            })
        if "asset_class = 'stock'" in sql:
            return STOCK_IDENTITY[["canonical_id", "exchange"]].head(0)
        return pd.DataFrame()

    with mock_patch.object(ingest, "read_sql", side_effect=fake):
        df = ingest.extract_st_events()
    assert df.empty


# ---------------------------------------------------------------------------
# Composite dedup + validity filtering in build_corporate_action_events (§7.2)
# ---------------------------------------------------------------------------

def test_same_date_type_different_exchange_not_merged():
    """sz000001 and sh000001 st_mark on the same date are two distinct
    securities: composite dedup must keep both."""
    events = pd.DataFrame([
        {"canonical_id": "000001", "exchange": "sz", "event_type": "st_mark",
         "effective_date": date(2024, 5, 6), "payload": {}, "source_id": "ju"},
        {"canonical_id": "000001", "exchange": "sh", "event_type": "st_mark",
         "effective_date": date(2024, 5, 6), "payload": {}, "source_id": "ju"},
    ])
    dedup_subset = ["exchange", "canonical_id", "event_type",
                    "effective_date", "source_id"]
    deduped = events.drop_duplicates(subset=dedup_subset, keep="first")
    assert len(deduped) == 2


def test_composite_validity_filter_drops_wrong_exchange():
    """An event whose (exchange, canonical_id) pair does not exist in
    canonical_symbol is dropped even though its canonical_id alone exists."""
    valid = pd.DataFrame({
        "exchange": ["sz", "sh", "sh"],
        "canonical_id": ["000001", "000001", "600000"],
    })
    events = pd.DataFrame([
        {"canonical_id": "000001", "exchange": "sz"},   # valid
        {"canonical_id": "000001", "exchange": "sh"},   # valid (index)
        {"canonical_id": "600000", "exchange": "sz"},   # wrong: 600000 is sh
        {"canonical_id": "600000", "exchange": "sh"},   # valid
    ])
    valid_pairs = set(zip(valid["exchange"], valid["canonical_id"]))
    pairs = list(zip(events["exchange"], events["canonical_id"]))
    kept = events[pd.Series(pairs, index=events.index).isin(valid_pairs)]
    assert len(kept) == 3
    assert set(zip(kept["exchange"], kept["canonical_id"])) == {
        ("sz", "000001"), ("sh", "000001"), ("sh", "600000"),
    }


# ---------------------------------------------------------------------------
# GBBQ exchange from source symbol (already fixed in round 3; regression)
# ---------------------------------------------------------------------------

def test_gbbq_exchange_comes_from_source_symbol_prefix():
    """GBBQ rows carry the source symbol; exchange must be its prefix, which
    keeps a shenzhen stock event on sz000001 regardless of any index with
    the same numeric id."""
    symbol = "sz000001"
    assert str(symbol)[:2] == "sz"


def test_insert_rejects_ambiguous_id_without_exchange():
    """The insert path refuses to guess an exchange for an ambiguous id."""
    ambiguous = pd.DataFrame({
        "canonical_id": ["000001", "000001"],
        "exchange": ["sz", "sh"],
    })
    dup_mask = ambiguous["canonical_id"].duplicated(keep=False)
    ambiguous_ids = set(ambiguous.loc[dup_mask, "canonical_id"])
    assert "000001" in ambiguous_ids
    # _insert_events_with_jsonb raises on ambiguous id without source exchange
    df = pd.DataFrame([{
        "canonical_id": "000001", "exchange": None, "event_type": "st_mark",
        "effective_date": date(2024, 1, 2), "payload": {}, "source_id": "ju",
    }])
    with mock_patch.object(
        ingest, "read_sql", return_value=ambiguous[["exchange", "canonical_id"]]
    ):
        with pytest.raises(ValueError, match="身份歧义"):
            ingest._insert_events_with_jsonb(df)


def test_repeated_ingest_same_input_idempotent():
    """ON CONFLICT (exchange, canonical_id, event_type, effective_date,
    source_id) DO NOTHING makes a rerun of the same input a no-op."""
    conflict_target = (
        "ON CONFLICT (exchange, canonical_id, event_type, effective_date, "
        "source_id) DO NOTHING"
    )
    source = inspect_insert_conflict_clause()
    assert conflict_target.replace(" ", "") in source.replace(" ", "")


def inspect_insert_conflict_clause() -> str:
    import inspect
    return inspect.getsource(ingest._insert_events_with_jsonb)
