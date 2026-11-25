"""P3 扩展：后复权价格序列 public.hfq_daily。

数据源（base 层允许直读源 namespaces，见分层铁律）：
  - tong.gbbq        category=1 除权除息事件。字段语义经全市场检验标定
                     （2026-11-19，除权日 open 落于参考价 ±11% 的通过率）：
                     解释 B（c2=配股价, c3=送转股数）99.92%；
                     解释 A（c2=送转, c3=配股价）仅 88.97% —— 已弃用。
                       c1 = 每 10 股派息（元）
                       c2 = 配股价（元/股）
                       c3 = 每 10 股送转股数
                       c4 = 每 10 股配股数
  - tong.stock_daily 不复权日线（numeric 价格、integer date YYYYMMDD）

公式（除权除息日，P_pre 为除权日前最后一个交易日收盘价）：
  P_ex  = (P_pre - c1/10 + c2*c4/10) / (1 + c3/10 + c4/10)
  factor_event = P_pre / P_ex
  factor_cum(t) = 上市起所有 effective_date <= t 的 factor_event 累乘
  hfq_price(t)  = raw_price(t) * factor_cum(t)

已知偏差（只记录不修正，遵循 base 对账铁律）：
  - tong.stock_daily 自 2008-01 起，更早事件缺 P_pre 基准 → 该事件因子按 1 跳过。
    影响 hfq 绝对价格水平（早期因子缺失），不影响逐日收益率口径。
  - public.corporate_action_event 的 dividend 投影（1,139 条）与 tong.gbbq
    category=1（69,398 条）存在覆盖差，属 P2/P4 对账发现，另行处置；
    本模块复权计算直接以 tong.gbbq 为准。
"""
import logging
from typing import Optional

import numpy as np
import pandas as pd

from base.db import read_sql, copy_upsert

log = logging.getLogger(__name__)

# 写库分块行数（控制 to_csv 内存峰值）
_CHUNK_ROWS = 5_000_000


def load_prices(symbols: Optional[list] = None) -> pd.DataFrame:
    """读不复权日线（全市场或指定 symbol）。"""
    sql = "SELECT symbol, date, open, high, low, close, volume, amount FROM tong.stock_daily"
    params = None
    if symbols:
        sql += " WHERE symbol = ANY(%s)"
        params = (symbols,)
    sql += " ORDER BY symbol, date"
    df = read_sql(sql, params=params)
    return df


def load_events(symbols: Optional[list] = None) -> pd.DataFrame:
    """读除权除息事件（category=1），含全零事件（计算时跳过但计数）。"""
    sql = "SELECT symbol, date AS ex_date, c1, c2, c3, c4 FROM tong.gbbq WHERE category = 1"
    params = None
    if symbols:
        sql += " AND symbol = ANY(%s)"
        params = (symbols,)
    sql += " ORDER BY symbol, ex_date"
    df = read_sql(sql, params=params)
    return df


def _event_factors(ev: pd.DataFrame, px: pd.DataFrame) -> tuple[pd.DataFrame, int, int]:
    """计算单只股票的事件复权因子。

    Returns:
        (DataFrame[ex_date, factor], n_skipped_no_price, n_skipped_invalid)
        仅含有效事件；无 P_pre 或 P_ex <= 0 的事件按因子 1 跳过并计数。
    """
    d = ev["c1"].fillna(0).to_numpy("float64") / 10.0  # 每股派息
    s = ev["c3"].fillna(0).to_numpy("float64") / 10.0  # 每股送转（c3，全市场标定）
    r = ev["c4"].fillna(0).to_numpy("float64") / 10.0  # 每股配股
    pr = ev["c2"].fillna(0).to_numpy("float64")        # 配股价（c2）

    # 全零事件无价格影响
    nonzero = (d != 0) | (s != 0) | (r != 0)
    if not nonzero.any():
        return pd.DataFrame(columns=["ex_date", "factor"]), 0, 0
    ev = ev.loc[nonzero].reset_index(drop=True)
    d, s, r, pr = d[nonzero], s[nonzero], r[nonzero], pr[nonzero]

    # 除权日前最后一个交易日收盘价（严格 < ex_date）
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


def _compute_symbol(px: pd.DataFrame, ev: Optional[pd.DataFrame]) -> tuple[pd.DataFrame, dict]:
    """单只股票：事件因子 → 逐日累计因子 → hfq 列 → hfq_return。

    Returns:
        (px 加列结果, skip 统计 dict)
    """
    px = px.sort_values("date").reset_index(drop=True)
    skip = {"no_price": 0, "invalid": 0, "all_zero": 0}

    if ev is None or ev.empty:
        px["factor_cum"] = 1.0
        return px, skip

    d = ev["c1"].fillna(0).to_numpy("float64") / 10.0
    s = ev["c3"].fillna(0).to_numpy("float64") / 10.0
    r = ev["c4"].fillna(0).to_numpy("float64") / 10.0
    if not ((d != 0) | (s != 0) | (r != 0)).any():
        px["factor_cum"] = 1.0
        skip["all_zero"] = len(ev)
        return px, skip

    evf, n_no_price, n_invalid = _event_factors(ev, px)
    skip["no_price"] = n_no_price
    skip["invalid"] = n_invalid

    if evf.empty:
        px["factor_cum"] = 1.0
    else:
        px = pd.merge_asof(
            px, evf.sort_values("ex_date"),
            left_on="date", right_on="ex_date",
            direction="backward",
        )
        # merge_asof 后每行携带"最近事件"的 factor；同一段落内 factor 重复，
        # 必须只在事件生效首行乘入，否则无事件日也被逐日累乘（factor_cum 会爆炸）。
        new_event = px["ex_date"].ne(px["ex_date"].shift()) & px["factor"].notna()
        px["factor_step"] = np.where(new_event, px["factor"], 1.0)
        px["factor_cum"] = px["factor_step"].cumprod()
        px = px.drop(columns=["ex_date", "factor", "factor_step"])

    for col in ("open", "high", "low", "close"):
        px[f"{col}_hfq"] = px[col] * px["factor_cum"]
    px["hfq_return"] = px["close_hfq"] / px["close_hfq"].shift(1) - 1.0
    return px, skip


def run(symbols: Optional[list] = None) -> dict:
    """计算并写入 public.hfq_daily，返回统计与自检结果。"""
    prices = load_prices(symbols)
    n_syms = prices["symbol"].nunique()
    log.info("行情 %d 行 / %d 股", len(prices), n_syms)

    events = load_events(symbols)
    ev_by_sym = {k: v for k, v in events.groupby("symbol", sort=False)}
    log.info("除权除息事件 %d 条 / %d 股", len(events), len(ev_by_sym))

    parts = []
    skip_total = {"no_price": 0, "invalid": 0, "all_zero": 0}
    for sym, px in prices.groupby("symbol", sort=False):
        part, skip = _compute_symbol(px, ev_by_sym.get(sym))
        parts.append(part)
        for k in skip_total:
            skip_total[k] += skip[k]
    out = pd.concat(parts, ignore_index=True)
    out["factor_cum"] = out["factor_cum"].astype("float64")

    # ── 自检 ─────────────────────────────────────────────────────────────
    raw_ret = out.groupby("symbol", sort=False)["close"].pct_change()
    diff = (out["hfq_return"] - raw_ret).abs()
    n_nonzero_diff = int((diff > 1e-9).sum())  # 差异行只应出现在除权日
    ex_ret_2019 = out.loc[(out["date"] >= 20190101) & (out["hfq_return"].abs() > 0.211)]
    stats = {
        "rows": int(len(out)),
        "symbols": int(n_syms),
        "events_input": int(len(events)),
        "rows_with_return_jump": n_nonzero_diff,  # hfq_return≠raw_return 行数，应≈有效事件覆盖行情日数
        "events_skipped_no_price": skip_total["no_price"],
        "events_skipped_invalid": skip_total["invalid"],
        "events_skipped_all_zero": skip_total["all_zero"],
        "extreme_hfq_return_rows_2019plus": int(len(ex_ret_2019)),
        "extreme_examples": ex_ret_2019.head(5)[["symbol", "date", "hfq_return"]].to_dict("records"),
    }

    # ── 写库（分块 COPY upsert）─────────────────────────────────────────
    cols = ["symbol", "date", "open", "high", "low", "close",
            "volume", "amount",
            "open_hfq", "high_hfq", "low_hfq", "close_hfq",
            "factor_cum", "hfq_return"]
    payload = out[cols]
    
    # 将 volume 从浮点数转换为整数（tong.stock_daily 的 numeric 类型被 pandas 读为 float）
    if "volume" in payload.columns:
        payload["volume"] = payload["volume"].round().astype("int64")
    
    for i in range(0, len(payload), _CHUNK_ROWS):
        chunk = payload.iloc[i:i + _CHUNK_ROWS]
        copy_upsert(chunk.reset_index(drop=True), "public", "hfq_daily")
        log.info("写入 %d / %d", min(i + _CHUNK_ROWS, len(payload)), len(payload))

    stats["rows_written"] = int(len(payload))
    log.info("hfq_daily 完成: %s", {k: v for k, v in stats.items() if k != "extreme_examples"})
    return stats
