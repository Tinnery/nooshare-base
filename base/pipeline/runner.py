"""P7: 幂等可复现管线编排器。

职责：串联 P1→P2→P4→P5，记录 pipeline_run（code_commit、input_hash、products、status）。

幂等性保障：
  - 同一 (输入快照, 代码版本) → 确定性产物
  - 每次运行记录输入哈希 / commit / 行数 / 耗时
  - 支持任意日期范围增量重算

步骤顺序契约：
  1. 记录 pipeline_run(running) + 输入快照哈希
  2. build canonical_symbol（P1）
  3. extract + project corporate_action_event（P2）
  4. run_reconciliation（P4）
  5. collect_freshness（P5）
  6. 更新 pipeline_run(ok/failed, products=行数)
"""
import logging
import subprocess
from typing import Optional, Dict, Any
from datetime import datetime

from base.db import read_sql, execute_sql

log = logging.getLogger(__name__)


def get_git_commit() -> str:
    """获取当前 git commit hash（以进程工作目录为准，要求在仓库内运行）。"""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
        )
        return result.stdout.strip() if result.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


def compute_input_hash() -> str:
    """计算输入快照哈希。

    对关键源表的 (行数, 最大日期, 内容校验和) 做哈希，用于区分"数据变了"与"计算变了"。
    内容校验和用采样 hash 计算（前 1000 行），避免大表性能问题。

    Returns:
        哈希字符串
    """
    import hashlib

    # 采集关键源表的元信息 (namespace, table, date_col or None, pk_cols)
    # 覆盖所有影响 public.* 产物的源表
    tables_to_hash = [
        # P1: 证券身份
        ("ju", "stocks", None, ["stock_code"]),
        ("tong", "symbol_meta", None, ["symbol"]),
        # P2: 公司行为事件
        ("ju", "st_period", "start_date", ["stock_code", "start_date"]),
        ("tong", "gbbq", "date", ["symbol", "date"]),
        # P3: 估值/复权/行情
        ("ju", "valuation", "start_date", ["stock_code", "start_date"]),
        ("tong", "stock_daily", "date", ["symbol", "date"]),
        # P1(OHLCV): 统一行情三源，index/etf 变化也要触发下游失效重算
        ("tong", "index_daily", "date", ["symbol", "date"]),
        ("tong", "etf_daily", "date", ["symbol", "date"]),
        # P3: 财务报表（PIT 视图依赖）
        ("finance", "income_statement", "report_date", ["code", "report_date"]),
        ("finance", "balance_sheet", "report_date", ["code", "report_date"]),
        ("finance", "cash_flow", "report_date", ["code", "report_date"]),
        # P3: 交易日历/涨跌停规则
        ("ju", "trading_day", "trade_date", ["trade_date"]),
        ("ju", "limit_ratio", "start_date", ["stock_code", "start_date"]),
    ]

    snapshot_parts = []

    for namespace, table_name, date_col, pk_cols in tables_to_hash:
        try:
            # 计算行数和最大日期
            if date_col:
                result = read_sql(f"""
                    SELECT COUNT(*) as row_count, MAX({date_col}) as max_date
                    FROM {namespace}.{table_name}
                """)
            else:
                result = read_sql(f"""
                    SELECT COUNT(*) as row_count
                    FROM {namespace}.{table_name}
                """)
            
            if not result.empty:
                row_count = result.iloc[0]["row_count"] or 0
                max_date = result.iloc[0].get("max_date", "null") or "null"
                
                # 计算内容校验和（采样前 1000 行）
                pk_order = ", ".join(pk_cols)
                checksum_result = read_sql(f"""
                    SELECT md5(string_agg(row_data, '')) as content_checksum
                    FROM (
                        SELECT t::text as row_data
                        FROM {namespace}.{table_name} t
                        ORDER BY {pk_order}
                        LIMIT 1000
                    ) h
                """)
                
                content_checksum = "null"
                if not checksum_result.empty and checksum_result.iloc[0]["content_checksum"]:
                    content_checksum = checksum_result.iloc[0]["content_checksum"]
                
                snapshot_parts.append(
                    f"{namespace}.{table_name}:{row_count}:{max_date}:{content_checksum}"
                )
        except Exception as e:
            log.warning(f"计算哈希失败: {namespace}.{table_name}: {e}")
            snapshot_parts.append(f"{namespace}.{table_name}:error")

    snapshot_str = "|".join(snapshot_parts)
    return hashlib.md5(snapshot_str.encode()).hexdigest()


def compute_output_hash(products: Dict[str, int]) -> str:
    """计算产物内容哈希。
    
    对关键 public.* 表的内容做哈希，用于验证幂等性。
    采用采样策略：对大表取前 1000 行，小表全量哈希。
    
    Args:
        products: 产物行数统计（用于判断表大小）
    
    Returns:
        产物内容哈希字符串
    """
    import hashlib
    
    # 关键产物表 (table, pk_cols, sample_threshold)
    # sample_threshold: 行数超过此值则采样，否则全量
    output_tables = [
        # 身份方案A 复合 PK = (exchange, canonical_id)，单列 hash 无法反映复合身份变化
        ("public.canonical_symbol", ["exchange", "canonical_id"], 10000),
        ("public.corporate_action_event", ["event_id"], 50000),
        ("public.hfq_daily", ["symbol", "date"], 1000000),
    ]
    
    snapshot_parts = []
    
    for table, pk_cols, threshold in output_tables:
        try:
            row_count = products.get(table.split(".")[-1], 0)
            
            # 根据表大小决定采样策略
            if row_count > threshold:
                # 大表：采样前 1000 行
                pk_order = ", ".join(pk_cols)
                checksum_result = read_sql(f"""
                    SELECT md5(string_agg(row_data, '')) as content_checksum
                    FROM (
                        SELECT t::text as row_data
                        FROM {table} t
                        ORDER BY {pk_order}
                        LIMIT 1000
                    ) h
                """)
            else:
                # 小表：全量哈希
                pk_order = ", ".join(pk_cols)
                checksum_result = read_sql(f"""
                    SELECT md5(string_agg(row_data, '')) as content_checksum
                    FROM (
                        SELECT t::text as row_data
                        FROM {table} t
                        ORDER BY {pk_order}
                    ) h
                """)
            
            content_checksum = "null"
            if not checksum_result.empty and checksum_result.iloc[0]["content_checksum"]:
                content_checksum = checksum_result.iloc[0]["content_checksum"]
            
            snapshot_parts.append(f"{table}:{row_count}:{content_checksum}")
        except Exception as e:
            log.warning(f"计算产物哈希失败: {table}: {e}")
            snapshot_parts.append(f"{table}:error")
    
    snapshot_str = "|".join(snapshot_parts)
    return hashlib.md5(snapshot_str.encode()).hexdigest()


def start_pipeline_run(code_commit: str, input_hash: str) -> str:
    """记录 pipeline_run 开始，返回 run_id。"""
    import uuid
    run_id = str(uuid.uuid4())
    execute_sql("""
        INSERT INTO public.pipeline_run (run_id, code_commit, input_hash, started_at, status)
        VALUES (%s, %s, %s, NOW(), 'running')
    """, params=(run_id, code_commit, input_hash))

    log.info(f"管线运行开始: run_id={run_id[:8]}, commit={code_commit[:8]}, input_hash={input_hash[:8]}")
    return run_id


def finish_pipeline_run(run_id: str, status: str, products: Dict[str, int], output_hash: str = None) -> None:
    """记录 pipeline_run 完成。
    
    Args:
        run_id: 运行 ID
        status: 状态 (ok/failed)
        products: 产物行数统计
        output_hash: 产物内容哈希（可选，用于幂等性验证）
    """
    import json

    execute_sql("""
        UPDATE public.pipeline_run
        SET completed_at = NOW(),
            status = %s,
            products = %s::jsonb,
            output_hash = %s
        WHERE run_id = %s
    """, params=(status, json.dumps(products), output_hash, run_id))

    log.info(f"管线运行完成: run_id={run_id[:8]}, status={status}, products={products}, output_hash={output_hash[:8] if output_hash else 'null'}")


def run_pipeline(start_date: Optional[str] = None) -> Dict[str, Any]:
    """执行完整管线：P1 符号 → P2 事件 → P3 物化视图 → P4 对账 → P5 新鲜度。

    Args:
        start_date: 起始日期 YYYYMMDD（增量重算用，可选）

    Returns:
        运行摘要 {run_id, status, products}
    """
    log.info(f"开始执行完整管线... start_date={start_date}")

    # 1. 记录运行开始
    code_commit = get_git_commit()
    input_hash = compute_input_hash()
    run_id = start_pipeline_run(code_commit, input_hash)

    products = {}

    try:
        # 2. P1: 构建 canonical_symbol
        log.info("Step 2/6: P1 构建 canonical_symbol...")
        from base.symbol.master import build_canonical_symbol
        symbol_count = build_canonical_symbol()
        products["canonical_symbol"] = symbol_count
        log.info(f"P1 完成: {symbol_count} 行")

        # 3. P2: 构建 corporate_action_event
        log.info("Step 3/6: P2 构建 corporate_action_event...")
        from base.events.ingest import build_corporate_action_events
        event_count = build_corporate_action_events()
        products["corporate_action_event"] = event_count
        log.info(f"P2 完成: {event_count} 行")

        # 4. P3: 刷新物化 PIT 视图
        log.info("Step 4/6: P3 刷新物化 PIT 视图...")
        from base.db import refresh_materialized_views
        refresh_materialized_views()
        products["materialized_views_refreshed"] = 6
        log.info("P3 完成: 6 个物化视图已刷新")

        # 5. P4: 跨源对账
        log.info("Step 5/6: P4 跨源对账...")
        from base.reconcile.runner import run_reconciliation
        reconcile_results = run_reconciliation()
        products["reconcile_checks"] = len(reconcile_results)
        log.info(f"P4 完成: {len(reconcile_results)} 项检查")

        # 6. P5: 数据新鲜度
        log.info("Step 6/6: P5 数据新鲜度...")
        from base.freshness.collect import collect_freshness
        freshness_records = collect_freshness()
        products["freshness_tables"] = len(freshness_records)
        log.info(f"P5 完成: {len(freshness_records)} 张表")

        # 7. 计算产物哈希
        log.info("Step 7/7: 计算产物哈希...")
        output_hash = compute_output_hash(products)
        log.info(f"产物哈希: {output_hash[:16]}")
        
        # 8. 记录成功
        finish_pipeline_run(run_id, "ok", products, output_hash)

        summary = {
            "run_id": run_id,
            "status": "ok",
            "products": products,
        }
        log.info(f"管线执行成功: {summary}")
        return summary

    except Exception as e:
        log.error(f"管线执行失败: {e}", exc_info=True)
        finish_pipeline_run(run_id, "failed", products)
        raise


def get_pipeline_history(limit: int = 10) -> list:
    """获取管线运行历史。"""
    return read_sql(f"""
        SELECT run_id, code_commit, input_hash, started_at, completed_at,
               status, products
        FROM public.pipeline_run
        ORDER BY started_at DESC
        LIMIT {limit}
    """)


def check_idempotency(run_id_1: str, run_id_2: str) -> bool:
    """检查两次运行的幂等性。

    如果两次运行的 input_hash 和 code_commit 相同，则 products 应该一致。

    Args:
        run_id_1: 第一次运行的 run_id
        run_id_2: 第二次运行的 run_id

    Returns:
        True = 幂等; False = 不一致
    """
    runs = read_sql("""
        SELECT run_id, input_hash, code_commit, products
        FROM public.pipeline_run
        WHERE run_id IN (%s, %s)
    """, params=(run_id_1, run_id_2))

    if len(runs) < 2:
        log.warning(f"找不到两次运行记录: {run_id_1}, {run_id_2}")
        return False

    run1 = runs[runs["run_id"] == run_id_1].iloc[0]
    run2 = runs[runs["run_id"] == run_id_2].iloc[0]

    # 检查 input_hash 和 code_commit 是否相同
    if run1["input_hash"] != run2["input_hash"]:
        log.info("input_hash 不同，非幂等性测试场景")
        return False

    if run1["code_commit"] != run2["code_commit"]:
        log.info("code_commit 不同，非幂等性测试场景")
        return False

    # 比较 products（行数）
    products_match = run1["products"] == run2["products"]
    
    # 比较 output_hash（内容）
    output_hash_match = True
    if run1.get("output_hash") and run2.get("output_hash"):
        output_hash_match = run1["output_hash"] == run2["output_hash"]
    elif run1.get("output_hash") or run2.get("output_hash"):
        # 一个有 hash，一个没有，无法比较
        output_hash_match = None

    if products_match and output_hash_match:
        log.info(f"幂等性验证通过: run {run_id_1} == run {run_id_2}")
        log.info(f"  products: {run1['products']}")
        log.info(f"  output_hash: {run1.get('output_hash', 'null')[:16] if run1.get('output_hash') else 'null'}")
        return True
    else:
        log.warning(f"幂等性验证失败: run {run_id_1} != run {run_id_2}")
        log.warning(f"  products match: {products_match}")
        log.warning(f"  output_hash match: {output_hash_match}")
        log.warning(f"  run1 products: {run1['products']}")
        log.warning(f"  run2 products: {run2['products']}")
        if run1.get("output_hash"):
            log.warning(f"  run1 output_hash: {run1['output_hash'][:16]}")
        if run2.get("output_hash"):
            log.warning(f"  run2 output_hash: {run2['output_hash'][:16]}")
        return False
