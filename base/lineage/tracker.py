"""数据血缘追踪（Data Lineage Tracking）。

目标：
  每行 public.* 数据可追溯到 namespace 源头。
  当数据出错时，能在 5 分钟内定位根因。

核心能力：
  1. 在 public.data_lineage 表记录血缘关系
  2. 在 ETL 过程中，记录每行数据的来源（namespace.table.row_id）
  3. 提供查询接口：
     - 正向追踪：输入 public 行 ID，输出 namespace 源行
     - 反向追踪：输入 namespace 行 ID，输出 public 行
     - 全链路追踪：输入 public 行 ID，输出完整的数据转换链路

数据模型：
  public.data_lineage:
    - target_table: public.* 表名
    - target_row_id: public 行标识（canonical_id + date 等）
    - source_table: namespace 表名（如 ju.valuation）
    - source_row_id: namespace 行标识（ctid 或主键）
    - source_observed_at: 观测时间戳
    - transform_type: 转换类型（direct_join, event_sourcing, pit_interval 等）
    - transform_rule: 转换规则（JSON）

CLI:
  python -m base.lineage.tracker --trace public.st_status_pit --canonical-id sh600000
  python -m base.lineage.tracker --reverse-trace ju.valuation --source-id 12345
"""
import json
import logging
from datetime import datetime
from typing import Dict, Any, List, Optional

import pandas as pd
import typer

from base.db import read_sql, execute_sql, get_connection

log = logging.getLogger(__name__)

app = typer.Typer(help="数据血缘追踪")


class LineageTracker:
    """数据血缘追踪器。

    负责：
      1. 初始化 public.data_lineage 表
      2. 记录血缘关系
      3. 查询血缘关系
    """

    def __init__(self):
        self._init_lineage_table()

    def _init_lineage_table(self):
        """初始化 public.data_lineage 表。"""
        ddl = """
        CREATE TABLE IF NOT EXISTS public.data_lineage (
            lineage_id BIGSERIAL PRIMARY KEY,
            target_table TEXT NOT NULL,
            target_row_id TEXT NOT NULL,
            source_table TEXT NOT NULL,
            source_row_id TEXT NOT NULL,
            source_observed_at TIMESTAMPTZ,
            transform_type TEXT,
            transform_rule JSONB,
            created_at TIMESTAMPTZ DEFAULT now(),
            UNIQUE (target_table, target_row_id, source_table, source_row_id)
        );

        CREATE INDEX IF NOT EXISTS idx_lineage_target
            ON public.data_lineage(target_table, target_row_id);
        CREATE INDEX IF NOT EXISTS idx_lineage_source
            ON public.data_lineage(source_table, source_row_id);
        """
        execute_sql(ddl)
        log.info("已初始化 public.data_lineage 表")

    def record_lineage(
        self,
        target_table: str,
        target_row_id: str,
        source_table: str,
        source_row_id: str,
        source_observed_at: Optional[datetime] = None,
        transform_type: Optional[str] = None,
        transform_rule: Optional[Dict] = None,
    ):
        """记录一条血缘关系。

        Args:
            target_table: public.* 表名
            target_row_id: public 行标识
            source_table: namespace 表名
            source_row_id: namespace 行标识
            source_observed_at: 观测时间戳
            transform_type: 转换类型
            transform_rule: 转换规则
        """
        sql = """
        INSERT INTO public.data_lineage
            (target_table, target_row_id, source_table, source_row_id,
             source_observed_at, transform_type, transform_rule)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (target_table, target_row_id, source_table, source_row_id)
        DO NOTHING
        """
        params = (
            target_table,
            target_row_id,
            source_table,
            source_row_id,
            source_observed_at,
            transform_type,
            json.dumps(transform_rule) if transform_rule else None,
        )
        execute_sql(sql, params)

    def record_batch_lineage(
        self,
        lineage_records: List[Dict[str, Any]],
    ):
        """批量记录血缘关系。

        Args:
            lineage_records: 血缘记录列表，每个记录包含：
                - target_table, target_row_id
                - source_table, source_row_id
                - source_observed_at (optional)
                - transform_type (optional)
                - transform_rule (optional)
        """
        if not lineage_records:
            return

        df = pd.DataFrame(lineage_records)
        df["transform_rule"] = df["transform_rule"].apply(
            lambda x: json.dumps(x) if x else None
        )

        with get_connection() as conn:
            with conn.cursor() as cur:
                # 使用临时表批量插入
                cur.execute("DROP TABLE IF EXISTS _temp_lineage")
                cur.execute("""
                    CREATE TEMP TABLE _temp_lineage AS
                    SELECT * FROM public.data_lineage WHERE FALSE
                """)

                # COPY 数据
                from io import StringIO
                buf = StringIO()
                df.to_csv(
                    buf,
                    sep="\t",
                    header=False,
                    index=False,
                    na_rep="\\N",
                    columns=[
                        "target_table",
                        "target_row_id",
                        "source_table",
                        "source_row_id",
                        "source_observed_at",
                        "transform_type",
                        "transform_rule",
                    ],
                )
                buf.seek(0)
                cur.copy_from(
                    buf,
                    "_temp_lineage",
                    null="\\N",
                    columns=[
                        "target_table",
                        "target_row_id",
                        "source_table",
                        "source_row_id",
                        "source_observed_at",
                        "transform_type",
                        "transform_rule",
                    ],
                )

                # 插入目标表
                cur.execute("""
                    INSERT INTO public.data_lineage
                        (target_table, target_row_id, source_table, source_row_id,
                         source_observed_at, transform_type, transform_rule)
                    SELECT target_table, target_row_id, source_table, source_row_id,
                           source_observed_at, transform_type, transform_rule
                    FROM _temp_lineage
                    ON CONFLICT (target_table, target_row_id, source_table, source_row_id)
                    DO NOTHING
                """)

                cur.execute("DROP TABLE IF EXISTS _temp_lineage")
            conn.commit()

        log.info(f"已批量记录 {len(lineage_records)} 条血缘关系")

    def trace_forward(
        self,
        target_table: str,
        target_row_id: str,
    ) -> List[Dict[str, Any]]:
        """正向追踪：输入 public 行 ID，输出 namespace 源行。

        Args:
            target_table: public.* 表名
            target_row_id: public 行标识

        Returns:
            血缘关系列表，每个包含：
                - source_table, source_row_id
                - source_observed_at
                - transform_type, transform_rule
        """
        sql = """
        SELECT source_table, source_row_id, source_observed_at,
               transform_type, transform_rule
        FROM public.data_lineage
        WHERE target_table = %s AND target_row_id = %s
        ORDER BY source_table, source_row_id
        """
        df = read_sql(sql, params=(target_table, target_row_id))
        return df.to_dict("records") if not df.empty else []

    def trace_reverse(
        self,
        source_table: str,
        source_row_id: str,
    ) -> List[Dict[str, Any]]:
        """反向追踪：输入 namespace 行 ID，输出 public 行。

        Args:
            source_table: namespace 表名
            source_row_id: namespace 行标识

        Returns:
            血缘关系列表，每个包含：
                - target_table, target_row_id
                - source_observed_at
                - transform_type, transform_rule
        """
        sql = """
        SELECT target_table, target_row_id, source_observed_at,
               transform_type, transform_rule
        FROM public.data_lineage
        WHERE source_table = %s AND source_row_id = %s
        ORDER BY target_table, target_row_id
        """
        df = read_sql(sql, params=(source_table, source_row_id))
        return df.to_dict("records") if not df.empty else []

    def trace_full_chain(
        self,
        target_table: str,
        target_row_id: str,
        max_depth: int = 5,
    ) -> Dict[str, Any]:
        """全链路追踪：输入 public 行 ID，输出完整的数据转换链路。

        Args:
            target_table: public.* 表名
            target_row_id: public 行标识
            max_depth: 最大追踪深度

        Returns:
            {
                "target": {"table": str, "row_id": str},
                "chain": [
                    {"depth": 0, "table": str, "row_id": str, "sources": [...]},
                    ...
                ],
            }
        """
        chain = []
        visited = set()

        def _trace_recursive(table: str, row_id: str, depth: int):
            if depth > max_depth or (table, row_id) in visited:
                return
            visited.add((table, row_id))

            sources = self.trace_forward(table, row_id)
            chain.append({
                "depth": depth,
                "table": table,
                "row_id": row_id,
                "sources": sources,
            })

            # 递归追踪每个源
            for src in sources:
                _trace_recursive(
                    src["source_table"],
                    src["source_row_id"],
                    depth + 1,
                )

        _trace_recursive(target_table, target_row_id, 0)

        return {
            "target": {"table": target_table, "row_id": target_row_id},
            "chain": chain,
        }

    def get_lineage_stats(self) -> Dict[str, Any]:
        """获取血缘统计信息。

        Returns:
            {
                "total_records": int,
                "by_target_table": {...},
                "by_source_table": {...},
            }
        """
        total = read_sql("SELECT COUNT(*) as cnt FROM public.data_lineage")
        total_count = total.iloc[0]["cnt"] if not total.empty else 0

        by_target = read_sql("""
            SELECT target_table, COUNT(*) as cnt
            FROM public.data_lineage
            GROUP BY target_table
            ORDER BY cnt DESC
        """)

        by_source = read_sql("""
            SELECT source_table, COUNT(*) as cnt
            FROM public.data_lineage
            GROUP BY source_table
            ORDER BY cnt DESC
        """)

        return {
            "total_records": int(total_count),
            "by_target_table": by_target.to_dict("records") if not by_target.empty else [],
            "by_source_table": by_source.to_dict("records") if not by_source.empty else [],
        }


def track_lineage(
    target_table: str,
    target_row_id: str,
    source_table: str,
    source_row_id: str,
    source_observed_at: Optional[datetime] = None,
    transform_type: Optional[str] = None,
    transform_rule: Optional[Dict] = None,
):
    """便捷函数：记录一条血缘关系。

    Args:
        target_table: public.* 表名
        target_row_id: public 行标识
        source_table: namespace 表名
        source_row_id: namespace 行标识
        source_observed_at: 观测时间戳
        transform_type: 转换类型
        transform_rule: 转换规则
    """
    tracker = LineageTracker()
    tracker.record_lineage(
        target_table=target_table,
        target_row_id=target_row_id,
        source_table=source_table,
        source_row_id=source_row_id,
        source_observed_at=source_observed_at,
        transform_type=transform_type,
        transform_rule=transform_rule,
    )


# ─── ETL 集成示例 ─────────────────────────────────────────────────────────────

def build_valuation_lineage():
    """构建 valuation_pit 的血缘关系。

    在 pipeline build 完成后调用，记录 valuation_pit 每行数据的来源。
    """
    log.info("构建 valuation_pit 血缘关系...")

    tracker = LineageTracker()

    # 查询 valuation_pit 的所有行
    pit_df = read_sql("""
        SELECT canonical_id, symbol, effective_from, effective_to, observed_at
        FROM public.valuation_pit
    """)

    if pit_df.empty:
        log.warning("valuation_pit 为空，跳过血缘构建")
        return

    lineage_records = []

    # 对每行 PIT 数据，找到对应的 ju.valuation 源行
    for _, row in pit_df.iterrows():
        canonical_id = row["canonical_id"]
        effective_from = row["effective_from"]
        observed_at = row["observed_at"]

        # 查找对应的 ju.valuation 行
        # 注意：ju.valuation 没有主键，用 (stock_code, start_date) 作为标识
        source_df = read_sql("""
            SELECT stock_code, start_date
            FROM ju.valuation
            WHERE start_date = %s
            LIMIT 1
        """, params=(effective_from,))

        if not source_df.empty:
            source_row_id = f"{source_df.iloc[0]['stock_code']}|{effective_from}"

            lineage_records.append({
                "target_table": "public.valuation_pit",
                "target_row_id": f"{canonical_id}|{effective_from}",
                "source_table": "ju.valuation",
                "source_row_id": source_row_id,
                "source_observed_at": observed_at,
                "transform_type": "direct_join",
                "transform_rule": {
                    "join_condition": "canonical_symbol.code = ju.valuation.stock_code",
                    "projection": ["capitalization", "circulating_cap"],
                },
            })

    # 批量记录
    tracker.record_batch_lineage(lineage_records)
    log.info(f"已构建 {len(lineage_records)} 条 valuation_pit 血缘关系")


def build_st_status_lineage():
    """构建 st_status_pit 的血缘关系。

    st_status_pit 来自 corporate_action_event，而 event 又来自 ju.st_period。
    所以血缘链路是：st_status_pit → corporate_action_event → ju.st_period
    """
    log.info("构建 st_status_pit 血缘关系...")

    tracker = LineageTracker()

    # 查询 st_status_pit 的所有行
    pit_df = read_sql("""
        SELECT canonical_id, symbol, effective_from, effective_to, observed_at
        FROM public.st_status_pit
    """)

    if pit_df.empty:
        log.warning("st_status_pit 为空，跳过血缘构建")
        return

    lineage_records = []

    for _, row in pit_df.iterrows():
        canonical_id = row["canonical_id"]
        effective_from = row["effective_from"]

        # 查找对应的 corporate_action_event
        event_df = read_sql("""
            SELECT event_id, source_id
            FROM public.corporate_action_event
            WHERE canonical_id = %s
              AND effective_date = %s
              AND event_type IN ('st_mark', 'st_remove')
            LIMIT 1
        """, params=(canonical_id, effective_from))

        if not event_df.empty:
            event_id = event_df.iloc[0]["event_id"]
            source_id = event_df.iloc[0]["source_id"]

            # 第一层：st_status_pit → corporate_action_event
            lineage_records.append({
                "target_table": "public.st_status_pit",
                "target_row_id": f"{canonical_id}|{effective_from}",
                "source_table": "public.corporate_action_event",
                "source_row_id": event_id,
                "source_observed_at": row["observed_at"],
                "transform_type": "event_sourcing",
                "transform_rule": {
                    "event_types": ["st_mark", "st_remove"],
                    "interval_construction": "LEAD(effective_date) OVER (PARTITION BY canonical_id)",
                },
            })

            # 第二层：corporate_action_event → ju.st_period（如果有 source_id）
            if source_id:
                lineage_records.append({
                    "target_table": "public.corporate_action_event",
                    "target_row_id": event_id,
                    "source_table": "ju.st_period",
                    "source_row_id": source_id,
                    "transform_type": "event_ingestion",
                    "transform_rule": {
                        "mapping": {
                            "stock_code": "canonical_id",
                            "st_type": "payload->>'st_type'",
                            "start_date": "effective_date",
                        }
                    },
                })

    tracker.record_batch_lineage(lineage_records)
    log.info(f"已构建 {len(lineage_records)} 条 st_status_pit 血缘关系")


# ─── CLI ─────────────────────────────────────────────────────────────────────

@app.command()
def trace(
    table: str = typer.Option(..., help="public.* 表名"),
    row_id: str = typer.Option(..., help="行标识（如 sh600000|2023-01-01）"),
    full_chain: bool = typer.Option(False, help="是否追踪完整链路"),
):
    """正向追踪：输入 public 行 ID，输出 namespace 源行。"""
    tracker = LineageTracker()

    if full_chain:
        result = tracker.trace_full_chain(table, row_id)
        typer.echo(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        sources = tracker.trace_forward(table, row_id)
        typer.echo(json.dumps(sources, ensure_ascii=False, indent=2, default=str))


@app.command()
def reverse_trace(
    table: str = typer.Option(..., help="namespace 表名"),
    row_id: str = typer.Option(..., help="行标识"),
):
    """反向追踪：输入 namespace 行 ID，输出 public 行。"""
    tracker = LineageTracker()
    targets = tracker.trace_reverse(table, row_id)
    typer.echo(json.dumps(targets, ensure_ascii=False, indent=2, default=str))


@app.command()
def stats():
    """查看血缘统计信息。"""
    tracker = LineageTracker()
    stats = tracker.get_lineage_stats()
    typer.echo(json.dumps(stats, ensure_ascii=False, indent=2))


@app.command()
def build():
    """构建所有 PIT 视图的血缘关系。"""
    build_valuation_lineage()
    build_st_status_lineage()
    typer.echo("✓ 血缘关系构建完成")


if __name__ == "__main__":
    app()
