-- P2 迁移：给 corporate_action_event 的 dividend/split/rights_issue 事件
-- 补充 source_action_key 与完整 c1-c4 原始 payload（追溯 tong.gbbq 记录）。
-- 幂等：仅更新 payload 缺少 source_action_key 的行；保留 event_id/observed_at/pub_date。
-- 不覆盖 event_type/value；只做追溯字段增强，不是修复错误事件。

BEGIN;

WITH g AS (
    SELECT
        stock_code AS canonical_id,
        to_date(date::text, 'YYYYMMDD') AS effective_date,
        c1, c2, c3, c4,
        'tong.gbbq|' || symbol || '|' || date::text || '|1' AS source_action_key,
        symbol AS source_symbol,
        date
    FROM tong.gbbq
    WHERE category = 1
      AND stock_code IS NOT NULL
      AND date IS NOT NULL
),
enriched AS (
    SELECT
        e.event_id,
        jsonb_build_object(
            'c1', g.c1, 'c2', g.c2, 'c3', g.c3, 'c4', g.c4,
            'category', 1,
            'source_action_key', g.source_action_key,
            'source_symbol', g.source_symbol,
            -- 保留原有语义键
            'cash_per_10', CASE WHEN e.event_type = 'dividend' THEN g.c1 END,
            'bonus_per_10', 0.0,
            'convert_per_10', CASE WHEN e.event_type = 'split' THEN g.c3 END,
            'rights_shares_per_10', CASE WHEN e.event_type = 'rights_issue' THEN g.c4 END,
            'rights_price', CASE WHEN e.event_type = 'rights_issue' THEN g.c2 END
        ) AS new_payload
    FROM public.corporate_action_event e
    JOIN g
      ON g.canonical_id = e.canonical_id
     AND g.effective_date = e.effective_date
    WHERE e.event_type IN ('dividend', 'split', 'rights_issue')
      AND NOT (e.payload ? 'source_action_key')
      AND (
          (e.event_type = 'dividend'      AND g.c1 > 0)
       OR (e.event_type = 'split'         AND g.c3 > 0)
       OR (e.event_type = 'rights_issue'  AND g.c4 > 0)
      )
)
UPDATE public.corporate_action_event e
SET payload = enriched.new_payload
FROM enriched
WHERE e.event_id = enriched.event_id;

COMMIT;

-- 验证
SELECT count(*) AS total,
       count(*) FILTER (WHERE payload ? 'source_action_key') AS with_key
FROM public.corporate_action_event;
