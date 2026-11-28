"""Factor Builder: 从 Raw 行情 + 公司行为直接构建版本化复权因子。

输入:
  - public.market_daily_bar (equity only) — Raw OHLCV
  - tong.gbbq category=1 — 除权除息事件
  - public.canonical_symbol — 证券身份校验

输出:
  - public.market_adjustment_factor_set — Factor Set 元数据（含完整 provenance）
  - public.market_daily_adjustment_factor — 每日累计复权因子

不写入 public.hfq_daily。不读取 public.hfq_daily。

公式（与 hfq.py 保持一致的数学定义）:
  P_ex  = (P_pre - c1/10 + c2*c4/10) / (1 + c3/10 + c4/10)
  factor_event = P_pre / P_ex
  factor_cum(t) = 上市起所有 effective_date <= t 的 factor_event 累乘
"""
import hashlib
import json
import logging
import subprocess
import uuid as uuid_mod
from typing import Optional

import numpy as np
import pandas as pd

from base.db import get_connection, read_sql

log = logging.getLogger(__name__)

_CHUNK_ROWS = 5_000_000
_ALGORITHM_VERSION = "v2(factor_builder-v1)"


def _get_git_sha() -> str:
    """获取 nooshare-base 当前 HEAD 的完整 Git SHA。"""
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%H"],
            capture_output=True, text=True,
            cwd="/home/travis/nooshare-workenv/repos/nooshare-base",
        )
        sha = result.stdout.strip()
        if len(sha) == 40 and all(c in "0123456789abcdef" for c in sha):
            return sha
    except Exception as e:
        log.warning("无法获取 Git SHA: %s", e)
    return "unavailable"


def _compute_stream_hash(conn, query: str, params=None) -> str:
    """流式计算查询结果的 SHA-256 哈希（不全部加载到内存）。"""
    h = hashlib.sha256()
    with conn.cursor() as cur:
        cur.execute(query, params)
        while True:
            rows = cur.fetchmany(50000)
            if not rows:
                break
            for row in rows:
                h.update("\t".join(str(v) if v is not None else "\\N" for v in row).encode())
    return h.hexdigest()


def load_prices(conn, symbols: Optional[list] = None) -> pd.DataFrame:
    """从 market_daily_bar 读取 equity 行情（Raw OHLCV）。"""
    sql = """
        SELECT symbol, date, open, high, low, close, volume, amount, source_id
        FROM public.market_daily_bar
        WHERE asset_family = 'equity'
    """
    params = None
    if symbols:
        sql += " AND symbol = ANY(%s)"
        params = (symbols,)
    sql += " ORDER BY symbol, date"
    df = pd.read_sql(sql, conn, params=params)
    return df


def load_events(conn, symbols: Optional[list] = None) -> pd.DataFrame:
    """从 tong.gbbq 读取除权除息事件（category=1）。"""
    sql = "SELECT symbol, date AS ex_date, c1, c2, c3, c4, source FROM tong.gbbq WHERE category = 1"
    params = None
    if symbols:
        sql += " AND symbol = ANY(%s)"
        params = (symbols,)
    sql += " ORDER BY symbol, ex_date"
    df = pd.read_sql(sql, conn, params=params)
    return df


def _event_factors(ev: pd.DataFrame, px: pd.DataFrame) -> tuple:
    """计算单只股票的事件复权因子。

    Returns:
        (DataFrame[ex_date, factor], n_skipped_no_price, n_skipped_invalid)
    """
    d = ev["c1"].fillna(0).to_numpy("float64") / 10.0
    s = ev["c3"].fillna(0).to_numpy("float64") / 10.0
    r = ev["c4"].fillna(0).to_numpy("float64") / 10.0
    pr = ev["c2"].fillna(0).to_numpy("float64")

    nonzero = (d != 0) | (s != 0) | (r != 0)
    if not nonzero.any():
        return pd.DataFrame(columns=["ex_date", "factor"]), 0, 0
    ev = ev.loc[nonzero].reset_index(drop=True)
    d, s, r, pr = d[nonzero], s[nonzero], r[nonzero], pr[nonzero]

    asof = pd.merge_asof(
        ev[["ex_date"]].sort_values("ex_date"),
        px[["date", "close"]].sort_values("date"),
        left_on="ex_date", right_on="date",
        direction="backward", allow_exact_matches=False,
    )
    p_pre = asof["close"].to_numpy("float64")
    ex_price = (p_pre - d + pr * r) / (1.0 + s + r)
    ok = np.isfinite(p_pre) & np.isfinite(ex_price) & (ex_price > 0)

    n_no_price = int(ok.size - ok.sum() - ((ex_price <= 0) & np.isfinite(p_pre)).sum())
    n_invalid = int(((ex_price <= 0) & np.isfinite(p_pre) & (p_pre > 0)).sum())

    res = pd.DataFrame(
        {"ex_date": ev["ex_date"].to_numpy()[ok], "factor": p_pre[ok] / ex_price[ok]}
    )
    return res, n_no_price, n_invalid


def _compute_symbol(px: pd.DataFrame, ev: Optional[pd.DataFrame]) -> tuple:
    """单只股票：事件因子 → 逐日累计因子。

    Returns:
        (DataFrame[symbol, date, factor_cum, factor_status], skip 统计)
    """
    px = px.sort_values("date").reset_index(drop=True)
    skip = {"no_price": 0, "invalid": 0, "all_zero": 0}
    symbol = px["symbol"].iloc[0]

    if ev is None or ev.empty:
        result = pd.DataFrame({
            "symbol": symbol,
            "date": px["date"].values,
            "factor_cum": np.ones(len(px), dtype="float64"),
            "factor_status": "valid",
        })
        return result, skip

    d = ev["c1"].fillna(0).to_numpy("float64") / 10.0
    s = ev["c3"].fillna(0).to_numpy("float64") / 10.0
    r = ev["c4"].fillna(0).to_numpy("float64") / 10.0
    if not ((d != 0) | (s != 0) | (r != 0)).any():
        result = pd.DataFrame({
            "symbol": symbol,
            "date": px["date"].values,
            "factor_cum": np.ones(len(px), dtype="float64"),
            "factor_status": "valid",
        })
        skip["all_zero"] = len(ev)
        return result, skip

    evf, n_no_price, n_invalid = _event_factors(ev, px)
    skip["no_price"] = n_no_price
    skip["invalid"] = n_invalid

    if evf.empty:
        factor_cum = np.ones(len(px), dtype="float64")
    else:
        merged = pd.merge_asof(
            px[["date"]].sort_values("date"),
            evf.sort_values("ex_date"),
            left_on="date", right_on="ex_date",
            direction="backward",
        )
        new_event = merged["ex_date"].ne(merged["ex_date"].shift()) & merged["factor"].notna()
        factor_step = np.where(new_event, merged["factor"].values, 1.0)
        factor_cum = np.cumprod(factor_step)

    result = pd.DataFrame({
        "symbol": symbol,
        "date": px["date"].values,
        "factor_cum": factor_cum,
        "factor_status": "valid",
    })
    return result, skip


def build_factor_set(
    symbols: Optional[list] = None,
    factor_set_name: Optional[str] = None,
    dry_run: bool = False,
) -> dict:
    """构建新的 Factor Set。

    流程:
    1. 注册 Factor Set（status=building）
    2. 读取 Raw 行情 + 公司行为
    3. 计算输入哈希
    4. 逐股票计算 factor_cum
    5. 写入 market_daily_adjustment_factor
    6. 验证
    7. 标记 validated

    Returns:
        dict: 构建统计与验证结果
    """
    factor_set_id = uuid_mod.uuid4()
    git_sha = _get_git_sha()

    if factor_set_name is None:
        factor_set_name = f"hfq_v2_{factor_set_id.hex[:8]}"

    log.info("=== Factor Builder 启动 ===")
    log.info("factor_set_id: %s", factor_set_id)
    log.info("factor_set_name: %s", factor_set_name)
    log.info("algorithm_version: %s", _ALGORITHM_VERSION)
    log.info("code_commit: %s", git_sha)

    with get_connection() as conn:
        conn.autocommit = False

        # ── Step 1: 注册 Factor Set（building）──────────────────────────
        if not dry_run:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO public.market_adjustment_factor_set (
                        factor_set_id, factor_set_name, market_source_id, action_source_id,
                        adjustment_method, algorithm_version, base_date_policy,
                        code_commit, status, notes
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    factor_set_id, factor_set_name, "tdx", "tdx",
                    "HFQ", _ALGORITHM_VERSION, "listing_date=1.0",
                    git_sha, "building",
                    f"Factor Builder v2: direct from market_daily_bar + tong.gbbq"
                ))
            conn.commit()
            log.info("Factor Set 已注册（building）")

        # ── Step 2: 读取输入数据 ────────────────────────────────────────
        log.info("读取 Raw 行情...")
        prices = load_prices(conn, symbols)
        n_syms = prices["symbol"].nunique()
        log.info("行情 %d 行 / %d 股", len(prices), n_syms)

        log.info("读取公司行为事件...")
        events = load_events(conn, symbols)
        log.info("事件 %d 条 / %d 股", len(events), events["symbol"].nunique())

        # ── Step 3: 计算输入哈希 ────────────────────────────────────────
        log.info("计算输入哈希...")
        bar_hash = _compute_stream_hash(
            conn,
            "SELECT symbol, date, close FROM public.market_daily_bar "
            "WHERE asset_family = 'equity' ORDER BY symbol, date"
        )
        action_hash = _compute_stream_hash(
            conn,
            "SELECT symbol, date, c1, c2, c3, c4 FROM tong.gbbq "
            "WHERE category = 1 ORDER BY symbol, date"
        )
        log.info("input_bar_hash: %s", bar_hash)
        log.info("input_action_hash: %s", action_hash)

        # ── Step 4: 逐股票计算 factor_cum ──────────────────────────────
        log.info("计算因子...")
        ev_by_sym = {k: v for k, v in events.groupby("symbol", sort=False)}
        parts = []
        skip_total = {"no_price": 0, "invalid": 0, "all_zero": 0}

        for sym, px in prices.groupby("symbol", sort=False):
            part, skip = _compute_symbol(px, ev_by_sym.get(sym))
            parts.append(part)
            for k in skip_total:
                skip_total[k] += skip[k]

        factors = pd.concat(parts, ignore_index=True)
        log.info("因子计算完成: %d 行 / %d 股", len(factors), factors["symbol"].nunique())

        # ── Step 5: 构建 manifest ──────────────────────────────────────
        min_date = int(prices["date"].min())
        max_date = int(prices["date"].max())
        manifest = {
            "bar_row_count": len(prices),
            "bar_symbol_count": int(n_syms),
            "bar_min_date": min_date,
            "bar_max_date": max_date,
            "action_row_count": len(events),
            "action_symbol_count": int(events["symbol"].nunique()),
            "action_min_date": int(events["date"].min()) if len(events) > 0 else None,
            "action_max_date": int(events["date"].max()) if len(events) > 0 else None,
            "factor_row_count": len(factors),
            "factor_symbol_count": int(factors["symbol"].nunique()),
        }

        # ── Step 6: 写入因子表 ─────────────────────────────────────────
        if not dry_run:
            log.info("写入因子表...")
            for i in range(0, len(factors), _CHUNK_ROWS):
                chunk = factors.iloc[i:i + _CHUNK_ROWS].copy()
                chunk["factor_set_id"] = factor_set_id
                # 使用 INSERT ... ON CONFLICT 确保幂等
                cols = ["factor_set_id", "symbol", "date", "factor_cum", "factor_status"]
                col_str = ", ".join(cols)
                with conn.cursor() as cur:
                    # 使用临时表 + COPY 模式
                    temp_table = "_temp_factor_insert"
                    cur.execute(f"DROP TABLE IF EXISTS {temp_table}")
                    cur.execute(f"""
                        CREATE TEMP TABLE {temp_table} (
                            factor_set_id UUID NOT NULL,
                            symbol TEXT NOT NULL,
                            date INTEGER NOT NULL,
                            factor_cum DOUBLE PRECISION NOT NULL,
                            factor_status TEXT NOT NULL DEFAULT 'valid'
                        )
                    """)
                    from io import StringIO
                    buf = StringIO()
                    chunk[cols].to_csv(buf, sep="\t", header=False, index=False, na_rep="\\N")
                    buf.seek(0)
                    cur.copy_from(buf, temp_table, null="\\N")
                    cur.execute(f"""
                        INSERT INTO public.market_daily_adjustment_factor
                            ({col_str})
                        SELECT {col_str} FROM {temp_table}
                        ON CONFLICT (factor_set_id, symbol, date) DO NOTHING
                    """)
                    cur.execute(f"DROP TABLE IF EXISTS {temp_table}")
                conn.commit()
                log.info("写入 %d / %d", min(i + _CHUNK_ROWS, len(factors)), len(factors))

        # ── Step 7: 验证 ───────────────────────────────────────────────
        log.info("验证因子...")
        validation = {}

        if not dry_run:
            with conn.cursor() as cur:
                # 行数验证
                cur.execute(
                    "SELECT COUNT(*) FROM public.market_daily_adjustment_factor WHERE factor_set_id = %s",
                    (factor_set_id,)
                )
                db_row_count = cur.fetchone()[0]
                validation["db_row_count"] = db_row_count
                validation["expected_row_count"] = len(factors)
                validation["row_count_match"] = db_row_count == len(factors)

                # 证券数验证
                cur.execute(
                    "SELECT COUNT(DISTINCT symbol) FROM public.market_daily_adjustment_factor WHERE factor_set_id = %s",
                    (factor_set_id,)
                )
                db_sym_count = cur.fetchone()[0]
                validation["db_symbol_count"] = db_sym_count

                # NULL 检查
                cur.execute(
                    "SELECT COUNT(*) FROM public.market_daily_adjustment_factor "
                    "WHERE factor_set_id = %s AND (factor_cum IS NULL OR factor_status IS NULL)",
                    (factor_set_id,)
                )
                null_count = cur.fetchone()[0]
                validation["null_count"] = null_count

                # 日期范围
                cur.execute(
                    "SELECT MIN(date), MAX(date) FROM public.market_daily_adjustment_factor WHERE factor_set_id = %s",
                    (factor_set_id,)
                )
                min_d, max_d = cur.fetchone()
                validation["min_date"] = min_d
                validation["max_date"] = max_d

        # ── Step 8: 更新 Factor Set 元数据 ─────────────────────────────
        all_valid = (
            validation.get("row_count_match", False)
            and validation.get("null_count", 999) == 0
        )

        if not dry_run:
            new_status = "validated" if all_valid else "building"
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE public.market_adjustment_factor_set SET
                        input_bar_hash = %s,
                        input_action_hash = %s,
                        input_manifest = %s,
                        known_at_cutoff = %s,
                        pre_base_policy = %s,
                        row_count = %s,
                        validated_at = CASE WHEN %s THEN now() ELSE NULL END,
                        status = %s,
                        notes = %s
                    WHERE factor_set_id = %s
                """, (
                    bar_hash,
                    action_hash,
                    json.dumps(manifest),
                    f"max_bar_date={max_date}",
                    "pre_2008_events_skipped_factor=1",
                    len(factors),
                    all_valid,
                    new_status,
                    f"Factor Builder v2: {len(factors)} rows, {factors['symbol'].nunique()} symbols, "
                    f"skip(no_price={skip_total['no_price']}, invalid={skip_total['invalid']}, "
                    f"all_zero={skip_total['all_zero']})",
                    factor_set_id,
                ))
            conn.commit()
            log.info("Factor Set 状态: %s", new_status)

        result = {
            "factor_set_id": str(factor_set_id),
            "factor_set_name": factor_set_name,
            "algorithm_version": _ALGORITHM_VERSION,
            "code_commit": git_sha,
            "input_bar_hash": bar_hash,
            "input_action_hash": action_hash,
            "manifest": manifest,
            "skip": skip_total,
            "validation": validation,
            "status": "validated" if all_valid else "building",
            "dry_run": dry_run,
        }

        log.info("=== Factor Builder 完成 ===")
        log.info("结果: %s", json.dumps({k: v for k, v in result.items() if k not in ("manifest",)}, ensure_ascii=False))
        return result
