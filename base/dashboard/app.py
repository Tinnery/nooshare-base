"""数据质量可视化监控 Dashboard - Flask 应用。

提供 API 端点查询数据质量指标，前端使用 ECharts 展示。

启动方式：
  python -m base.dashboard.app
  或
  base dashboard --port 5000

访问：http://localhost:5000
"""
import logging
from datetime import datetime, timedelta
from typing import Dict, Any, List

from flask import Flask, render_template, jsonify

from base.db import read_sql

log = logging.getLogger(__name__)

app = Flask(__name__, template_folder="templates", static_folder="static")


# ─── 路由 ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    """Dashboard 主页。"""
    return render_template("dashboard.html")


@app.route("/api/overview")
def api_overview():
    """总览数据。"""
    try:
        # 数据新鲜度统计
        freshness_df = read_sql("""
            SELECT 
                status,
                COUNT(*) as count
            FROM public.data_freshness
            GROUP BY status
        """)
        
        freshness_stats = {}
        for _, row in freshness_df.iterrows():
            freshness_stats[row["status"]] = int(row["count"])
        
        # 总表数
        total_tables = sum(freshness_stats.values())
        
        # SLA 达成率
        sla_df = read_sql("""
            SELECT 
                COUNT(*) as total_checks,
                SUM(CASE WHEN is_violation THEN 1 ELSE 0 END) as violations
            FROM public.sla_violation_log
            WHERE check_date >= CURRENT_DATE - INTERVAL '7 days'
        """)
        
        if not sla_df.empty and sla_df.iloc[0]["total_checks"] > 0:
            total_checks = int(sla_df.iloc[0]["total_checks"])
            violations = int(sla_df.iloc[0]["violations"])
            sla_compliance = 1 - (violations / total_checks) if total_checks > 0 else 1.0
        else:
            sla_compliance = 1.0
            total_checks = 0
            violations = 0
        
        # 最近管线运行
        pipeline_df = read_sql("""
            SELECT 
                run_id,
                started_at,
                completed_at,
                status
            FROM public.pipeline_run
            ORDER BY started_at DESC
            LIMIT 1
        """)
        
        last_run = None
        if not pipeline_df.empty:
            row = pipeline_df.iloc[0]
            last_run = {
                "run_id": row["run_id"],
                "started_at": row["started_at"].isoformat() if row["started_at"] else None,
                "completed_at": row["completed_at"].isoformat() if row["completed_at"] else None,
                "status": row["status"],
            }
        
        return jsonify({
            "success": True,
            "data": {
                "total_tables": total_tables,
                "freshness_stats": freshness_stats,
                "sla_compliance": sla_compliance,
                "sla_total_checks": total_checks,
                "sla_violations": violations,
                "last_pipeline_run": last_run,
            }
        })
    except Exception as e:
        log.error(f"API overview 失败: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/freshness")
def api_freshness():
    """数据新鲜度详情。"""
    try:
        df = read_sql("""
            SELECT 
                namespace,
                table_name,
                max_date,
                row_count,
                refreshed_at,
                status
            FROM public.data_freshness
            ORDER BY namespace, table_name
        """)
        
        tables = []
        for _, row in df.iterrows():
            tables.append({
                "namespace": row["namespace"],
                "table_name": row["table_name"],
                "max_date": row["max_date"].isoformat() if row["max_date"] else None,
                "row_count": int(row["row_count"]) if row["row_count"] else 0,
                "refreshed_at": row["refreshed_at"].isoformat() if row["refreshed_at"] else None,
                "status": row["status"],
            })
        
        return jsonify({"success": True, "data": tables})
    except Exception as e:
        log.error(f"API freshness 失败: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/freshness_heatmap")
def api_freshness_heatmap():
    """数据新鲜度热力图数据。"""
    try:
        df = read_sql("""
            SELECT 
                namespace,
                table_name,
                EXTRACT(DAY FROM NOW() - refreshed_at) as age_days,
                status
            FROM public.data_freshness
            ORDER BY namespace, table_name
        """)
        
        heatmap_data = []
        for _, row in df.iterrows():
            heatmap_data.append({
                "namespace": row["namespace"],
                "table": row["table_name"],
                "age_days": float(row["age_days"]) if row["age_days"] else 0,
                "status": row["status"],
            })
        
        return jsonify({"success": True, "data": heatmap_data})
    except Exception as e:
        log.error(f"API freshness_heatmap 失败: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/sla_violations")
def api_sla_violations():
    """SLA 违约历史。"""
    try:
        days = 30  # 默认查询最近 30 天
        
        df = read_sql("""
            SELECT 
                v.check_date,
                s.namespace,
                s.table_name,
                s.priority,
                v.is_violation,
                v.delay_minutes
            FROM public.sla_violation_log v
            JOIN public.data_sla s ON s.sla_id = v.sla_id
            WHERE v.check_date >= CURRENT_DATE - INTERVAL '%s days'
            ORDER BY v.check_date DESC
        """ % days)
        
        violations = []
        for _, row in df.iterrows():
            violations.append({
                "check_date": row["check_date"].isoformat() if row["check_date"] else None,
                "namespace": row["namespace"],
                "table_name": row["table_name"],
                "priority": row["priority"],
                "is_violation": bool(row["is_violation"]),
                "delay_minutes": int(row["delay_minutes"]) if row["delay_minutes"] else 0,
            })
        
        return jsonify({"success": True, "data": violations})
    except Exception as e:
        log.error(f"API sla_violations 失败: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/pipeline_history")
def api_pipeline_history():
    """管线运行历史。"""
    try:
        limit = 50
        
        df = read_sql("""
            SELECT 
                run_id,
                started_at,
                completed_at,
                status,
                code_commit
            FROM public.pipeline_run
            ORDER BY started_at DESC
            LIMIT %s
        """ % limit)
        
        runs = []
        for _, row in df.iterrows():
            duration = None
            if row["started_at"] and row["completed_at"]:
                duration = (row["completed_at"] - row["started_at"]).total_seconds()
            
            runs.append({
                "run_id": row["run_id"],
                "started_at": row["started_at"].isoformat() if row["started_at"] else None,
                "completed_at": row["completed_at"].isoformat() if row["completed_at"] else None,
                "status": row["status"],
                "code_commit": row["code_commit"],
                "duration_seconds": duration,
            })
        
        return jsonify({"success": True, "data": runs})
    except Exception as e:
        log.error(f"API pipeline_history 失败: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/data_volume")
def api_data_volume():
    """数据量统计。"""
    try:
        df = read_sql("""
            SELECT 
                namespace,
                table_name,
                row_count,
                refreshed_at
            FROM public.data_freshness
            WHERE row_count IS NOT NULL
            ORDER BY namespace, row_count DESC
        """)
        
        volumes = []
        for _, row in df.iterrows():
            volumes.append({
                "namespace": row["namespace"],
                "table_name": row["table_name"],
                "row_count": int(row["row_count"]) if row["row_count"] else 0,
                "refreshed_at": row["refreshed_at"].isoformat() if row["refreshed_at"] else None,
            })
        
        return jsonify({"success": True, "data": volumes})
    except Exception as e:
        log.error(f"API data_volume 失败: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/sla_definitions")
def api_sla_definitions():
    """SLA 定义列表。"""
    try:
        df = read_sql("""
            SELECT 
                sla_id,
                namespace,
                table_name,
                sla_type,
                expected_time,
                timezone,
                grace_period_minutes,
                priority,
                description,
                is_active
            FROM public.data_sla
            WHERE is_active = true
            ORDER BY priority, namespace, table_name
        """)
        
        slas = []
        for _, row in df.iterrows():
            slas.append({
                "sla_id": int(row["sla_id"]),
                "namespace": row["namespace"],
                "table_name": row["table_name"],
                "sla_type": row["sla_type"],
                "expected_time": row["expected_time"],
                "timezone": row["timezone"],
                "grace_period_minutes": int(row["grace_period_minutes"]),
                "priority": row["priority"],
                "description": row["description"],
                "is_active": bool(row["is_active"]),
            })
        
        return jsonify({"success": True, "data": slas})
    except Exception as e:
        log.error(f"API sla_definitions 失败: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


# ─── 启动 ────────────────────────────────────────────────────────────────────

def run_dashboard(host: str = "127.0.0.1", port: int = 5000, debug: bool = False):
    """启动 Dashboard 服务。"""
    log.info(f"启动 Dashboard: http://{host}:{port}")
    app.run(host=host, port=port, debug=debug)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    run_dashboard(debug=True)
