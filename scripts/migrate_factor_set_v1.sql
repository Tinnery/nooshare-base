-- ═══════════════════════════════════════════════════════════════════
-- [HISTORICAL MIGRATION — 已退役，仅保留 provenance 事实记录]
--
-- 历史事实：Factor Set v1（11111111-...）最初由 public.hfq_daily.factor_cum
-- 一次性导入生成，其 input_manifest.source_table 记录 'public.hfq_daily'。
-- 该字符串是对历史构建过程的事实描述，不是运行时依赖，不应篡改。
--
-- public.hfq_daily 已 DROP。因此：
--   * 本脚本不再读取旧表，也不再写入任何因子数据；
--   * 若 v1 已存在（现状），脚本为纯空操作，只做存在性自检；
--   * 若 v1 不存在（全新环境），应使用 `base factor-builder`
--     从 market_daily_bar + tong.gbbq 构建新的 Factor Set，而不是复原 v1。
--
-- 保留本文件仅用于追溯 v1 的来源；禁止纳入自动 migration runner。
-- ═══════════════════════════════════════════════════════════════════

\pset pager off

-- 存在性自检：v1 是否仍在库中
SELECT
    fs.factor_set_id,
    fs.factor_set_name,
    fs.status,
    fs.row_count AS declared_rows,
    fs.input_manifest->>'source_table' AS historical_source_table,
    fs.algorithm_version,
    fs.code_commit,
    fs.validated_at
FROM public.market_adjustment_factor_set fs
WHERE fs.factor_set_id = '11111111-1111-4111-8111-111111111111';

-- 因子规模自检
SELECT
    COUNT(*)            AS actual_rows,
    COUNT(DISTINCT symbol) AS symbols,
    MIN(date)           AS min_date,
    MAX(date)           AS max_date
FROM public.market_daily_adjustment_factor
WHERE factor_set_id = '11111111-1111-4111-8111-111111111111';