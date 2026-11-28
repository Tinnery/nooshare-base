-- G05: Factor Set 不可变性（数据库层面强制，不依赖 Agent 约定）
--
-- 规则:
--   building   : 允许写入因子、允许更新元数据
--   validated  : 冻结因子值与关键输入身份（禁止 UPDATE/DELETE 因子行；
--                禁止改写 factor_set 的关键身份列）
--   retired    : 历史 Run 仍可读，禁止作为新默认版本；同样冻结因子与身份
--
-- 实现: 触发器 + 强制固化的身份列。使用最小必要范围，不引入额外权限体系。

-- ── 1. 因子行不可变 ──────────────────────────────────────────────
CREATE OR REPLACE FUNCTION public.fn_guard_daily_factor_immutable()
RETURNS TRIGGER AS $$
DECLARE
    st TEXT;
BEGIN
    -- 被操作行的 factor_set 当前状态
    SELECT status INTO st
    FROM public.market_adjustment_factor_set
    WHERE factor_set_id = COALESCE(NEW.factor_set_id, OLD.factor_set_id);

    IF st IN ('validated', 'retired') THEN
        IF TG_OP = 'UPDATE' THEN
            -- 值不变允许（幂等重写），值变化一律拒绝
            IF NEW.factor_cum IS DISTINCT FROM OLD.factor_cum
               OR NEW.factor_status IS DISTINCT FROM OLD.factor_status
               OR NEW.symbol IS DISTINCT FROM OLD.symbol
               OR NEW.date IS DISTINCT FROM OLD.date THEN
                RAISE EXCEPTION
                    'Factor Set % 已 %，禁止修改因子行 (% / %)',
                    COALESCE(NEW.factor_set_id, OLD.factor_set_id), st,
                    OLD.symbol, OLD.date;
            END IF;
            RETURN NEW;
        ELSIF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION
                'Factor Set % 已 %，禁止删除因子行 (% / %)',
                OLD.factor_set_id, st, OLD.symbol, OLD.date;
        END IF;
    END IF;

    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_daily_factor_immutable ON public.market_daily_adjustment_factor;
CREATE TRIGGER trg_daily_factor_immutable
    BEFORE UPDATE OR DELETE ON public.market_daily_adjustment_factor
    FOR EACH ROW EXECUTE FUNCTION public.fn_guard_daily_factor_immutable();

-- ── 2. Factor Set 关键身份列不可改 ────────────────────────────────
CREATE OR REPLACE FUNCTION public.fn_guard_factor_set_identity()
RETURNS TRIGGER AS $$
BEGIN
    IF OLD.status IN ('validated', 'retired') THEN
        IF NEW.market_source_id  IS DISTINCT FROM OLD.market_source_id
           OR NEW.action_source_id IS DISTINCT FROM OLD.action_source_id
           OR NEW.adjustment_method IS DISTINCT FROM OLD.adjustment_method
           OR NEW.algorithm_version IS DISTINCT FROM OLD.algorithm_version
           OR NEW.base_date_policy IS DISTINCT FROM OLD.base_date_policy
           OR NEW.input_bar_hash IS DISTINCT FROM OLD.input_bar_hash
           OR NEW.input_action_hash IS DISTINCT FROM OLD.input_action_hash
           OR NEW.row_count IS DISTINCT FROM OLD.row_count
           OR NEW.factor_set_name IS DISTINCT FROM OLD.factor_set_name THEN
            RAISE EXCEPTION
                'Factor Set % 已 %，禁止修改关键输入身份或行数',
                OLD.factor_set_id, OLD.status;
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_factor_set_identity ON public.market_adjustment_factor_set;
CREATE TRIGGER trg_factor_set_identity
    BEFORE UPDATE ON public.market_adjustment_factor_set
    FOR EACH ROW EXECUTE FUNCTION public.fn_guard_factor_set_identity();

-- ── 3. 状态迁移白名单 ────────────────────────────────────────────
CREATE OR REPLACE FUNCTION public.fn_guard_factor_set_status()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.status IS DISTINCT FROM OLD.status THEN
        IF NOT (
            (OLD.status = 'building'  AND NEW.status IN ('validated', 'building'))
         OR (OLD.status = 'validated' AND NEW.status = 'retired')
        ) THEN
            RAISE EXCEPTION
                'Factor Set 状态迁移非法: % -> %（允许 building->validated, validated->retired）',
                OLD.status, NEW.status;
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_factor_set_status ON public.market_adjustment_factor_set;
CREATE TRIGGER trg_factor_set_status
    BEFORE UPDATE ON public.market_adjustment_factor_set
    FOR EACH ROW EXECUTE FUNCTION public.fn_guard_factor_set_status();