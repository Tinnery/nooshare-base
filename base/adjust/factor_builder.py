"""Factor Builder: 从 Raw 行情 + 公司行为直接构建版本化复权因子。

输入:
  - public.market_daily_bar (asset_family=equity) — Raw OHLCV, 唯一行情来源
  - tong.gbbq category=1 — 除权除息事件
  - public.canonical_symbol — 证券身份校验（不靠代码前缀猜交易所）

输出:
  - public.market_adjustment_factor_set — Factor Set 元数据（完整 provenance）
  - public.market_daily_adjustment_factor — 每日累计复权因子

不读取 public.hfq_daily，不写入 public.hfq_daily。

数学定义与旧 hfq.py 一致（P_pre 为除权日前最后交易日收盘价）:
  P_ex  = (P_pre - c1/10 + c2*c4/10) / (1 + c3/10 + c4/10)
  factor_event = P_pre / P_ex
  factor_cum(t) = 上市起所有 ex_date <= t 的 factor_event 累乘

已知偏差（只记录不遮盖）:
  - tong.stock_daily 自 2008-01 起，更早事件缺 P_pre 基准 → 该事件跳过，
    该证券对应行标记 factor_status=incomplete，可审计，不静默填 1。
  - gbbq 无 pub_date → PIT_UNPROVEN，记录在 known_at_cutoff。
"""
import hashlib
import json
import logging
import subprocess
import uuid as uuid_mod
from io import StringIO
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from base.db import get_connection

log = logging.getLogger(__name__)

_CHUNK_ROWS = 3_000_000
_ALGORITHM_VERSION = "v2(factor_builder-v1)"
_BASE_DATE_POLICY = "listing_date=1.0"
_KNOWN_AT_CUTOFF = "ex_date_based_no_pub_date(PIT_UNPROVEN)"
_PRE_BASE_POLICY = "pre_2008_events_skipped_factor=1"


def _git_sha() -> str:
    """nooshare-base 当前 HEAD 完整 SHA；取不到则抛出异常。
    
    从 Path(__file__) 推导仓库根目录，不依赖硬编码路径。
    正式构建必须获取有效 Git SHA，否则拒绝发布。
    """
    # 从当前文件推导仓库根：base/adjust/factor_builder.py -> 仓库根
    repo_root = Path(__file__).resolve().parent.parent.parent
    r = subprocess.run(
        ["git", "log", "-1", "--format=%H"],
        capture_output=True, text=True,
        cwd=str(repo_root),
    )
    if r.returncode != 0:
        raise RuntimeError(f"git log 失败: {r.stderr.strip()}")
    sha = r.stdout.strip()
    if len(sha) == 40 and all(c in "0123456789abcdef" for c in sha):
        return sha
    raise RuntimeError(f"非法 SHA: {sha!r}")


def _stream_hash(conn, query: str, params=None) -> str:
    """流式 SHA-256（不把全表读进内存）。"""
    h = hashlib.sha256()
    with conn.cursor() as cur:
        cur.execute(query, params)
        while True:
            rows = cur.fetchmany(100_000)
            if not rows:
                break
            for row in rows:
                h.update("\t".join("\\N" if v is None else str(v) for v in row).encode())
                h.update(b"\n")
    return h.hexdigest()


def load_prices(conn, symbols: Optional[list] = None, source_id: str = "tdx") -> pd.DataFrame:
    """读 equity Raw 行情（market_daily_bar），按 source_id 过滤。
    
    Args:
        conn: 数据库连接
        symbols: 可选证券代码列表
        source_id: 行情来源 ID，默认 "tdx"。必须与 Factor Set 注册时一致。
    """
    sql = """
        SELECT symbol, date, open, high, low, close, volume, amount, source_id
        FROM public.market_daily_bar
        WHERE asset_family = 'equity' AND source_id = %s
    """
    params = [source_id]
    if symbols:
        sql += " AND symbol = ANY(%s)"
        params.append(symbols)
    sql += " ORDER BY symbol, date"
    with conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
    return pd.DataFrame(rows, columns=cols)


def load_events(conn, symbols: Optional[list] = None, source_id: str = "tdx") -> pd.DataFrame:
    """读除权除息事件（tong.gbbq category=1），按 source_id 过滤。
    
    Args:
        conn: 数据库连接
        symbols: 可选证券代码列表
        source_id: 公司行为来源 ID，默认 "tdx"。必须与 Factor Set 注册时一致。
    """
    sql = ("SELECT symbol, date AS ex_date, c1, c2, c3, c4, source "
           "FROM tong.gbbq WHERE category = 1 AND source = %s")
    params = [source_id]
    if symbols:
        sql += " AND symbol = ANY(%s)"
        params.append(symbols)
    sql += " ORDER BY symbol, ex_date"
    with conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
    return pd.DataFrame(rows, columns=cols)


def stock_identity(conn, symbols: Optional[list] = None) -> set:
    """canonical 权威中的股票身份集合（asset_class=stock）。"""
    sql = "SELECT symbol FROM public.canonical_symbol WHERE asset_class = 'stock'"
    params = None
    if symbols:
        sql += " AND symbol = ANY(%s)"
        params = (symbols,)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return {r[0] for r in cur.fetchall()}


def _event_factors(ev: pd.DataFrame, px: pd.DataFrame):
    """单股事件因子。返回 (DataFrame[ex_date,factor], n_no_price, n_invalid)。"""
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

    finite_pre = np.isfinite(p_pre)
    ok = finite_pre & np.isfinite(ex_price) & (ex_price > 0)
    n_invalid = int((finite_pre & (p_pre > 0) & ~(ex_price > 0)).sum())
    n_no_price = int((~finite_pre).sum())

    res = pd.DataFrame({
        "ex_date": ev["ex_date"].to_numpy()[ok],
        "factor": p_pre[ok] / ex_price[ok],
    })
    return res, n_no_price, n_invalid


def _compute_symbol(px: pd.DataFrame, ev: Optional[pd.DataFrame]):
    """单股：事件因子 → 逐日累计因子。返回 (DataFrame, skip dict)。"""
    px = px.sort_values("date").reset_index(drop=True)
    symbol = px["symbol"].iloc[0]
    dates = px["date"].to_numpy()
    skip = {"no_price": 0, "invalid": 0, "all_zero": 0}

    def _flat(status: str) -> pd.DataFrame:
        return pd.DataFrame({
            "symbol": symbol, "date": dates,
            "factor_cum": np.ones(len(dates), dtype="float64"),
            "factor_status": status,
        })

    if ev is None or ev.empty:
        # 无公司行为事件：因子确实恒为 1，属 valid 而非缺失
        return _flat("valid"), skip

    d = ev["c1"].fillna(0).to_numpy("float64") / 10.0
    s = ev["c3"].fillna(0).to_numpy("float64") / 10.0
    r = ev["c4"].fillna(0).to_numpy("float64") / 10.0
    if not ((d != 0) | (s != 0) | (r != 0)).any():
        skip["all_zero"] = len(ev)
        return _flat("valid"), skip

    evf, n_no_price, n_invalid = _event_factors(ev, px)
    skip["no_price"] = n_no_price
    skip["invalid"] = n_invalid

    if evf.empty:
        # 有事件但全部无法计算 → 不假装 valid
        return _flat("incomplete"), skip

    merged = pd.merge_asof(
        px[["date"]].sort_values("date"),
        evf.sort_values("ex_date"),
        left_on="date", right_on="ex_date",
        direction="backward",
    )
    new_event = merged["ex_date"].ne(merged["ex_date"].shift()) & merged["factor"].notna()
    step = np.where(new_event, merged["factor"].to_numpy(), 1.0)
    factor_cum = np.cumprod(step)

    status = "incomplete" if (n_no_price + n_invalid) > 0 else "valid"
    return pd.DataFrame({
        "symbol": symbol, "date": dates,
        "factor_cum": factor_cum, "factor_status": status,
    }), skip


def _coverage(conn, factor_set_id: str, symbols=None, source_id: str = "tdx") -> dict:
    """因子覆盖率审计（六项计数）。

    覆盖分母限定为本 Factor Set 自身的证券集合：部分构建时不应把
    未参与构建的证券计为 missing。全市场构建时两者等价。
    
    Args:
        conn: 数据库连接
        factor_set_id: Factor Set UUID
        symbols: 可选证券代码列表（未使用，保留兼容）
        source_id: 行情来源 ID，默认 "tdx"。必须与构建时一致。
    """
    q = """
    WITH f AS (
        SELECT symbol, date, factor_status
        FROM public.market_daily_adjustment_factor
        WHERE factor_set_id = %s
    ), eq AS (
        SELECT b.symbol, b.date
        FROM public.market_daily_bar b
        WHERE b.asset_family = 'equity'
          AND b.source_id = %s
          AND b.symbol IN (SELECT DISTINCT symbol FROM f)
    )
    SELECT
      (SELECT COUNT(*) FROM eq),
      (SELECT COUNT(*) FROM f WHERE factor_status = 'valid'),
      (SELECT COUNT(*) FROM f WHERE factor_status = 'incomplete'),
      (SELECT COUNT(*) FROM f WHERE factor_status = 'unavailable'),
      (SELECT COUNT(*) FROM eq e
         LEFT JOIN f ON f.symbol = e.symbol AND f.date = e.date
        WHERE f.symbol IS NULL),
      (SELECT COUNT(*) FROM f
         LEFT JOIN eq e ON e.symbol = f.symbol AND e.date = f.date
        WHERE e.symbol IS NULL)
    """
    with conn.cursor() as cur:
        cur.execute(q, (factor_set_id, source_id))
        row = cur.fetchone()
    keys = ["raw_equity_rows", "valid_factor_rows", "incomplete_factor_rows",
            "unavailable_factor_rows", "missing_factor_rows", "unexpected_factor_rows"]
    return dict(zip(keys, (int(x) for x in row)))


def build_factor_set(
    symbols: Optional[list] = None,
    factor_set_name: Optional[str] = None,
    market_source_id: str = "tdx",
    action_source_id: str = "tdx",
    dry_run: bool = False,
) -> dict:
    """构建新 Factor Set。

    Args:
        symbols: 可选证券代码列表
        factor_set_name: Factor Set 名称
        market_source_id: 行情来源 ID，默认 "tdx"
        action_source_id: 公司行为来源 ID，默认 "tdx"
        dry_run: 是否只计算不写入

    building → 计算 → 写因子 → 验证 → validated。失败停在 building，绝不半发布。
    """
    factor_set_id = str(uuid_mod.uuid4())
    git_sha = _git_sha()
    if factor_set_name is None:
        factor_set_name = "hfq_v2_" + factor_set_id.replace("-", "")[:8]

    log.info("=== Factor Builder 启动 ===")
    log.info("factor_set_id=%s name=%s", factor_set_id, factor_set_name)
    log.info("algorithm_version=%s code_commit=%s", _ALGORITHM_VERSION, git_sha)

    with get_connection() as conn:
        conn.autocommit = False

        # ── 1. 注册 building ────────────────────────────────────────────
        if not dry_run:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO public.market_adjustment_factor_set ("
                    " factor_set_id, factor_set_name, market_source_id, action_source_id,"
                    " adjustment_method, algorithm_version, base_date_policy,"
                    " code_commit, status, notes"
                    ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        factor_set_id, factor_set_name, market_source_id, action_source_id,
                        "HFQ", _ALGORITHM_VERSION, _BASE_DATE_POLICY,
                        git_sha, "building",
                        "Factor Builder v2: raw view + gbbq, no hfq_daily",
                    ),
                )
            conn.commit()

        # ── 2. 读输入 ──────────────────────────────────────────────────
        prices = load_prices(conn, symbols, source_id=market_source_id)
        if prices.empty:
            raise RuntimeError("market_daily_bar 无 equity 行情，拒绝构建空 Factor Set")
        stocks = stock_identity(conn, symbols)
        prices = prices[prices["symbol"].isin(stocks)].reset_index(drop=True)
        n_syms = int(prices["symbol"].nunique())
        log.info("行情 %d 行 / %d 股（已按 canonical stock 过滤）", len(prices), n_syms)

        events = load_events(conn, list(prices["symbol"].unique()), source_id=action_source_id)
        log.info("事件 %d 条", len(events))

        # ── 3. 输入哈希（真实流式 SHA-256）──────────────────────────────
        bar_hash = _stream_hash(
            conn,
            "SELECT symbol, date, open, high, low, close, volume, amount, source_id "
            "FROM public.market_daily_bar WHERE asset_family = 'equity' AND source_id = %s "
            "ORDER BY symbol, date",
            (market_source_id,),
        )
        action_hash = _stream_hash(
            conn,
            "SELECT symbol, date, c1, c2, c3, c4, source FROM tong.gbbq WHERE category = 1 "
            "AND source = %s ORDER BY symbol, date",
            (action_source_id,),
        )
        log.info("bar_hash=%s action_hash=%s", bar_hash, action_hash)

        # ── 4. 计算因子 ────────────────────────────────────────────────
        ev_by_sym = {k: v for k, v in events.groupby("symbol", sort=False)} if len(events) else {}
        parts, skip_total = [], {"no_price": 0, "invalid": 0, "all_zero": 0}
        for _sym, px in prices.groupby("symbol", sort=False):
            part, skip = _compute_symbol(px, ev_by_sym.get(_sym))
            parts.append(part)
            for k in skip_total:
                skip_total[k] += skip[k]
        factors = pd.concat(parts, ignore_index=True)
        log.info("因子 %d 行 / %d 股", len(factors), factors["symbol"].nunique())

        min_date = int(prices["date"].min())
        max_date = int(prices["date"].max())
        manifest = {
            "market_source_id": market_source_id,
            "action_source_id": action_source_id,
            "bar_rows": int(len(prices)),
            "bar_symbols": n_syms,
            "bar_min_date": min_date,
            "bar_max_date": max_date,
            "action_rows": int(len(events)),
            "action_symbols": int(events["symbol"].nunique()) if len(events) else 0,
            "action_min_date": int(events["ex_date"].min()) if len(events) else None,
            "action_max_date": int(events["ex_date"].max()) if len(events) else None,
            "factor_rows": int(len(factors)),
            "factor_symbols": int(factors["symbol"].nunique()),
            "status_counts": {str(k): int(v) for k, v in
                              factors["factor_status"].value_counts().items()},
        }

        # ── 5. 写因子 ──────────────────────────────────────────────────
        if not dry_run:
            cols = ["factor_set_id", "symbol", "date", "factor_cum", "factor_status"]
            total = len(factors)
            for i in range(0, total, _CHUNK_ROWS):
                chunk = factors.iloc[i:i + _CHUNK_ROWS].copy()
                chunk.insert(0, "factor_set_id", factor_set_id)
                with conn.cursor() as cur:
                    cur.execute("DROP TABLE IF EXISTS _temp_factor_builder")
                    cur.execute(
                        "CREATE TEMP TABLE _temp_factor_builder ("
                        " factor_set_id UUID NOT NULL,"
                        " symbol TEXT NOT NULL,"
                        " date INTEGER NOT NULL,"
                        " factor_cum DOUBLE PRECISION NOT NULL,"
                        " factor_status TEXT NOT NULL)"
                    )
                    buf = StringIO()
                    chunk[cols].to_csv(buf, sep="\t", header=False, index=False, na_rep="\\N")
                    buf.seek(0)
                    cur.copy_from(buf, "_temp_factor_builder", null="\\N")
                    cur.execute(
                        "INSERT INTO public.market_daily_adjustment_factor"
                        " (factor_set_id, symbol, date, factor_cum, factor_status)"
                        " SELECT factor_set_id, symbol, date, factor_cum, factor_status"
                        " FROM _temp_factor_builder"
                        " ON CONFLICT (factor_set_id, symbol, date) DO NOTHING"
                    )
                    cur.execute("DROP TABLE IF EXISTS _temp_factor_builder")
                conn.commit()
                log.info("写入 %d / %d", min(i + _CHUNK_ROWS, total), total)

        # ── 6. 验证 ────────────────────────────────────────────────────
        coverage = _coverage(conn, factor_set_id, symbols, source_id=market_source_id) if not dry_run else {}
        validation = {"coverage": coverage}
        ok = True
        if not dry_run:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM public.market_daily_adjustment_factor "
                    "WHERE factor_set_id = %s", (factor_set_id,))
                db_rows = int(cur.fetchone()[0])
                cur.execute(
                    "SELECT COUNT(*) FROM public.market_daily_adjustment_factor "
                    "WHERE factor_set_id = %s AND (factor_cum IS NULL OR factor_status IS NULL)",
                    (factor_set_id,))
                nulls = int(cur.fetchone()[0])
                cur.execute(
                    "SELECT MIN(date), MAX(date) FROM public.market_daily_adjustment_factor "
                    "WHERE factor_set_id = %s", (factor_set_id,))
                dmin, dmax = cur.fetchone()
            validation.update({
                "db_rows": db_rows, "expected_rows": int(len(factors)),
                "row_match": db_rows == len(factors), "nulls": nulls,
                "db_min_date": dmin, "db_max_date": dmax,
            })
            ok = (
                db_rows == len(factors)
                and nulls == 0
                and coverage.get("missing_factor_rows", 1) == 0
                and coverage.get("unexpected_factor_rows", 1) == 0
            )

        # ── 7. 定稿 ────────────────────────────────────────────────────
        if not dry_run:
            status = "validated" if ok else "building"
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE public.market_adjustment_factor_set SET"
                    " input_bar_hash = %s, input_action_hash = %s,"
                    " input_manifest = %s, known_at_cutoff = %s,"
                    " pre_base_policy = %s, row_count = %s,"
                    " validated_at = CASE WHEN %s THEN now() ELSE NULL END,"
                    " status = %s, notes = %s"
                    " WHERE factor_set_id = %s",
                    (
                        bar_hash, action_hash, json.dumps(manifest, default=str),
                        _KNOWN_AT_CUTOFF, _PRE_BASE_POLICY, int(len(factors)),
                        ok, status,
                        "Factor Builder v2 rows=%d symbols=%d skip=%s"
                        % (len(factors), factors["symbol"].nunique(), json.dumps(skip_total)),
                        factor_set_id,
                    ),
                )
            conn.commit()
            log.info("Factor Set 状态=%s", status)
        else:
            status = "building"

        result = {
            "factor_set_id": factor_set_id,
            "factor_set_name": factor_set_name,
            "algorithm_version": _ALGORITHM_VERSION,
            "code_commit": git_sha,
            "input_bar_hash": bar_hash,
            "input_action_hash": action_hash,
            "manifest": manifest,
            "skip": skip_total,
            "validation": validation,
            "status": status,
            "dry_run": dry_run,
        }
        log.info("=== 完成: status=%s rows=%d ===", status, len(factors))
        return result