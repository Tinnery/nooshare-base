-- P3: 不可变版本化 HFQ 因子基础设施
-- factor_set 记录一次完整因子计算的身份与输入证据；一旦 validated 不可修改。
-- 每日累计因子表保存 factor_set × symbol × date 的累计因子。

BEGIN;

CREATE TABLE IF NOT EXISTS public.market_adjustment_factor_set (
    factor_set_id       UUID PRIMARY KEY,
    factor_set_name     TEXT NOT NULL UNIQUE,
    market_source_id    TEXT NOT NULL,          -- 行情来源（tdx）
    action_source_id    TEXT NOT NULL,          -- 公司行为来源（tdx）
    adjustment_method   TEXT NOT NULL,          -- HFQ（MVP 只做 HFQ）
    algorithm_version   TEXT NOT NULL,          -- 计算算法版本
    base_date_policy    TEXT NOT NULL,          -- 基准日策略
    input_bar_hash      TEXT,                   -- 输入行情快照哈希
    input_action_hash   TEXT,                   -- 输入公司行为快照哈希
    input_manifest      JSONB,                  -- 输入清单（行数/日期范围/源表）
    code_commit         TEXT,                   -- 计算代码提交
    known_at_cutoff     TEXT,                   -- 已知信息截止语义
    pre_base_policy     TEXT,                   -- 首个行情日前事件处理策略
    status              TEXT NOT NULL DEFAULT 'building', -- building/validated/retired
    row_count           BIGINT,
    created_at          TIMESTAMPTZ DEFAULT now(),
    validated_at        TIMESTAMPTZ,
    notes               TEXT
);
COMMENT ON TABLE public.market_adjustment_factor_set IS
    '不可变复权因子版本。status 不由消费者修改；当前选用版本由独立配置/指针决定。';

CREATE TABLE IF NOT EXISTS public.market_daily_adjustment_factor (
    factor_set_id   UUID NOT NULL REFERENCES public.market_adjustment_factor_set(factor_set_id),
    symbol          TEXT NOT NULL,
    date            INTEGER NOT NULL,
    factor_cum      DOUBLE PRECISION NOT NULL,
    factor_status   TEXT NOT NULL DEFAULT 'valid',  -- valid/incomplete/unavailable
    PRIMARY KEY (factor_set_id, symbol, date)
);
COMMENT ON TABLE public.market_daily_adjustment_factor IS
    '每日累计复权因子。仅受支持资产入库；指数/ETF 不写 factor_cum=1 伪记录。';

CREATE INDEX IF NOT EXISTS idx_daily_factor_symbol_date
    ON public.market_daily_adjustment_factor (factor_set_id, symbol, date);
CREATE INDEX IF NOT EXISTS idx_daily_factor_date
    ON public.market_daily_adjustment_factor (factor_set_id, date);

COMMIT;

-- 验证
SELECT table_name FROM information_schema.tables
WHERE table_schema='public' AND table_name LIKE 'market_%factor%'
ORDER BY table_name;
