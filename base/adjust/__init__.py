"""复权因子构建模块。

提供两个独立的实现：
- hfq.py: 旧版实现（已废弃，写入 public.hfq_daily）
- factor_builder.py: 新版实现（写入 market_daily_adjustment_factor）

新版 Factor Builder 是 OHLCV 最终架构的核心：
- 输入: market_daily_bar (Raw OHLCV) + tong.gbbq (公司行为)
- 输出: market_adjustment_factor_set + market_daily_adjustment_factor
- 不依赖 public.hfq_daily
"""

__all__ = ["factor_builder"]
