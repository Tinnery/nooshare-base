"""复权计算（已迁移至 Factor Builder）。

历史说明
--------
旧实现在本模块内计算 HFQ 全量 OHLCV 并写入物理表 ``public.hfq_daily``。
该表已退役：复权的唯一基础设施是版本化因子
（``public.market_adjustment_factor_set`` + ``public.market_daily_adjustment_factor``），
Adjusted OHLCV 由 ``public.market_daily_adjusted_bar`` VIEW 按 factor_set_id 派生。

本模块保留 ``run()`` 作为兼容入口，行为已改为调用 Factor Builder：
不再生产、不再写入 ``public.hfq_daily``，也不保存 HFQ 价格物理副本。
新代码请直接使用 :func:`base.adjust.factor_builder.build_factor_set`。
"""

import logging
from typing import Optional

from base.adjust.factor_builder import build_factor_set

log = logging.getLogger(__name__)

__all__ = ["run", "build_factor_set"]


def run(symbols: Optional[list] = None) -> dict:
    """兼容入口：构建新的版本化 Factor Set。

    .. deprecated::
        ``public.hfq_daily`` 已退役。此函数不再写物理 HFQ 表，
        只为旧调用方保留签名，内部转调 Factor Builder。

    Returns:
        dict: Factor Builder 构建结果（含 factor_set_id / status / validation）。
    """
    log.warning(
        "base.adjust.hfq.run() 已迁移：不再写入 public.hfq_daily，"
        "改为构建版本化 Factor Set。请改用 factor_builder.build_factor_set()。"
    )
    return build_factor_set(symbols=symbols)