"""P4: 跨源对账。

职责：对重叠数据交叉验证（tong vs 交易所公报、ju.stocks vs tong.symbol_meta、
ju.trading_day vs 官方休市安排、finance.* vs annual-semi 提取数字），
差异写 public.reconcile_status，只记录不修正。
"""
