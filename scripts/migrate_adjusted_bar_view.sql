-- P4: 指定 factor_set_id 的 HFQ 派生行情接口
-- Raw 行情(market_daily_bar) × 累计因子(market_daily_adjustment_factor) → HFQ 价格
-- View 暴露 factor_set_id 列，消费者用 WHERE factor_set_id = <uuid> 固定版本。
-- 仅 equity 资产有股票式复权因子；index/etf 不在此 view 内（无伪造 factor）。

CREATE OR REPLACE VIEW public.market_daily_adjusted_bar AS
SELECT
    b.symbol,
    b.date,
    b.source_id AS market_source_id,
    f.factor_set_id,
    b.open, b.high, b.low, b.close,       -- Raw OHLC (执行口径)
    b.volume, b.amount,                    -- 原始成交量额
    (b.open   * f.factor_cum) AS open_hfq,
    (b.high   * f.factor_cum) AS high_hfq,
    (b.low    * f.factor_cum) AS low_hfq,
    (b.close  * f.factor_cum) AS close_hfq,
    f.factor_cum
FROM public.market_daily_bar b
JOIN public.market_daily_adjustment_factor f
    ON f.symbol = b.symbol AND f.date = b.date
WHERE b.asset_family = 'equity';

COMMENT ON VIEW public.market_daily_adjusted_bar IS
    '指定 factor_set_id 的 HFQ 派生行情。消费者必须 WHERE factor_set_id=<uuid>。HFQ由Base计算，Strategy不自行复权。';

-- 验证：抽样比对 v1 factor set 的 adjusted bar 与 hfq_daily（避免全表JOIN超时）
SELECT count(*) AS mismatch FROM (
    SELECT a.symbol, a.date, a.open_hfq, a.close_hfq
    FROM public.market_daily_adjusted_bar a
    WHERE a.factor_set_id = '11111111-1111-4111-8111-111111111111'
    LIMIT 10000
) a
JOIN public.hfq_daily h ON h.symbol=a.symbol AND h.date=a.date
WHERE a.open_hfq <> h.open_hfq OR a.close_hfq <> h.close_hfq;
