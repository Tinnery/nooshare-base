-- P3: 生成 factor_set v1（从已全市场验证的 hfq_daily.factor_cum 派生）
-- 输入: public.hfq_daily（2008-01-02..2026-11-10, 15,655,244 行, 5,970 股）
-- 算法: 与 hfq.py _compute_symbol 一致（v1 基线），绑定 code_commit
-- 幂等: 同 factor_set_name 已存在则跳过

BEGIN;

INSERT INTO public.market_adjustment_factor_set (
    factor_set_id, factor_set_name, market_source_id, action_source_id,
    adjustment_method, algorithm_version, base_date_policy,
    input_bar_hash, input_action_hash, input_manifest,
    code_commit, known_at_cutoff, pre_base_policy, status, row_count, validated_at, notes
)
SELECT
    '11111111-1111-4111-8111-111111111111'::uuid,
    'hfq_v1_from_hfq_daily',
    'tdx', 'tdx', 'HFQ', 'v1(hfq.py baseline)',
    'listing_date=1.0',
    md5('15655244|20080102|20261110|42433834.59|5970'),
    md5('tong.gbbq category=1; 69398 rows; verified 99.92%'),
    jsonb_build_object(
        'source_table', 'public.hfq_daily',
        'row_count', 15655244,
        'min_date', 20080102,
        'max_date', 20261110,
        'n_symbols', 5970,
        'factor_cum_sum', 42433834.59
    ),
    '1ce97a3',
    'ex_date_based_no_pub_date(PIT_UNPROVEN)',
    'pre_2008_events_skipped_factor=1',
    'validated', 15655244, now(),
    'v1 baseline factor set: imported from validated public.hfq_daily.factor_cum; PIT unverified (pub_date absent)'
ON CONFLICT (factor_set_id) DO NOTHING;

-- 导入每日累计因子（幂等: ON CONFLICT DO NOTHING）
INSERT INTO public.market_daily_adjustment_factor (
    factor_set_id, symbol, date, factor_cum, factor_status
)
SELECT
    '11111111-1111-4111-8111-111111111111'::uuid,
    symbol, date, factor_cum, 'valid'
FROM public.hfq_daily
ON CONFLICT (factor_set_id, symbol, date) DO NOTHING;

COMMIT;

-- 验证
SELECT fs.factor_set_name, fs.status, fs.row_count AS declared,
       count(f.symbol) AS actual, min(f.date) AS min_date, max(f.date) AS max_date
FROM public.market_adjustment_factor_set fs
LEFT JOIN public.market_daily_adjustment_factor f ON f.factor_set_id = fs.factor_set_id
WHERE fs.factor_set_name = 'hfq_v1_from_hfq_daily'
GROUP BY fs.factor_set_name, fs.status, fs.row_count;
