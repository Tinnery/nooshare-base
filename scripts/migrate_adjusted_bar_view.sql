-- P4/v2: 指定 factor_set_id 的 HFQ 派生行情接口
-- Raw 行情(market_daily_bar) × 累计因子(market_daily_adjustment_factor) → HFQ 价格
--
-- 消费者必须 WHERE factor_set_id = <uuid> 固定版本，禁止依赖 latest 指针。
--
-- Grain: (symbol, date, market_source_id, factor_set_id)
--   同一 Factor Set 只绑定一个 market_source_id，故该 Grain 唯一。
--
-- 来源身份约束（本轮新增）:
--   JOIN market_adjustment_factor_set ON factor_set_id 且
--        fs.market_source_id = b.source_id
--   确保因子只应用于其绑定的行情来源，不会跨 source 错误复用。
--   同一 symbol/date 存在多个 source 时，每个 source 只匹配自己的 factor set。
--
-- 仅 equity 资产有股票式复权因子；index/etf 不在此 view 内（无伪造 factor）。

CREATE OR REPLACE VIEW public.market_daily_adjusted_bar AS
SELECT
    b.symbol,
    b.date,
    b.source_id AS market_source_id,
    f.factor_set_id,
    b.open, b.high, b.low, b.close,        -- Raw OHLC（执行口径）
    b.volume, b.amount,                    -- 原始成交量额（不复权）
    (b.open   * f.factor_cum) AS open_hfq,
    (b.high   * f.factor_cum) AS high_hfq,
    (b.low    * f.factor_cum) AS low_hfq,
    (b.close  * f.factor_cum) AS close_hfq,
    f.factor_cum,
    f.factor_status
FROM public.market_daily_bar b
JOIN public.market_daily_adjustment_factor f
    ON f.symbol = b.symbol
   AND f.date   = b.date
JOIN public.market_adjustment_factor_set fs
    ON fs.factor_set_id       = f.factor_set_id
   AND fs.market_source_id    = b.source_id
WHERE b.asset_family = 'equity';

COMMENT ON VIEW public.market_daily_adjusted_bar IS
    '指定 factor_set_id 的 HFQ 派生行情。Grain=(symbol,date,market_source_id,factor_set_id)。消费者必须 WHERE factor_set_id=<uuid>。HFQ由Base计算，Strategy不自行复权。';