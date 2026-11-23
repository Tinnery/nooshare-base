"""契约测试自动化（Contract Test Automation）。

从 YAML 契约生成 JSON Schema，并自动验证数据是否符合契约。

核心能力：
  1. 从 contracts/public_*.yaml 生成 JSON Schema
  2. 每次 ETL 运行后，自动验证数据是否符合 Schema
  3. 契约测试覆盖率 = 100%（所有 public.* 表都有自动化验证）

CLI:
  python -m tests.contract_tests --validate-all
  python -m tests.contract_tests --table public.st_status_pit
  pytest tests/contract_tests.py -v
"""
import json
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional

import pandas as pd
from ruamel.yaml import YAML
import typer

from base.db import read_sql, get_connection

log = logging.getLogger(__name__)

app = typer.Typer(help="契约测试自动化")

CONTRACTS_DIR = Path(__file__).parent.parent / "contracts"


# ─── 类型映射 ─────────────────────────────────────────────────────────────────

PG_TYPE_TO_JSON_SCHEMA = {
    "TEXT": {"type": "string"},
    "VARCHAR": {"type": "string"},
    "CHAR": {"type": "string"},
    "DATE": {"type": "string", "format": "date"},
    "TIMESTAMP": {"type": "string", "format": "date-time"},
    "TIMESTAMPTZ": {"type": "string", "format": "date-time"},
    "INTEGER": {"type": "integer"},
    "INT": {"type": "integer"},
    "BIGINT": {"type": "integer"},
    "SMALLINT": {"type": "integer"},
    "NUMERIC": {"type": "number"},
    "DECIMAL": {"type": "number"},
    "FLOAT": {"type": "number"},
    "DOUBLE": {"type": "number"},
    "REAL": {"type": "number"},
    "BOOLEAN": {"type": "boolean"},
    "BOOL": {"type": "boolean"},
    "JSON": {"type": "object"},
    "JSONB": {"type": "object"},
    "UUID": {"type": "string", "format": "uuid"},
}


def pg_type_to_json_schema(pg_type: str) -> Dict[str, Any]:
    """将 PostgreSQL 类型映射为 JSON Schema 类型。

    Args:
        pg_type: PostgreSQL 类型（如 TEXT, NUMERIC, DATE）

    Returns:
        JSON Schema 类型定义
    """
    pg_type_upper = pg_type.upper().split("(")[0]  # 去掉参数如 VARCHAR(100)
    schema = PG_TYPE_TO_JSON_SCHEMA.get(pg_type_upper, {"type": "string"})
    return schema.copy()


def load_contract(yaml_path: Path) -> Dict[str, Any]:
    """加载 YAML 契约文件。

    Args:
        yaml_path: 契约文件路径

    Returns:
        契约字典
    """
    yaml = YAML(typ="safe")
    with open(yaml_path, "r", encoding="utf-8") as f:
        return yaml.load(f)


def contract_to_json_schema(contract: Dict[str, Any]) -> Dict[str, Any]:
    """将 YAML 契约转换为 JSON Schema。

    Args:
        contract: YAML 契约字典

    Returns:
        JSON Schema 字典
    """
    table_name = contract["contract"]
    columns = contract.get("columns", [])

    properties = {}
    required = []

    for col in columns:
        col_name = col["name"]
        col_type = col.get("type", "TEXT")
        nullable = col.get("nullable", True)

        prop = pg_type_to_json_schema(col_type)

        # 添加语义描述
        if "semantic" in col:
            prop["description"] = col["semantic"]

        properties[col_name] = prop

        if not nullable:
            required.append(col_name)

    schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": table_name,
        "description": contract.get("description", ""),
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }

    return schema


def generate_all_schemas(output_dir: Optional[Path] = None) -> List[Path]:
    """从所有 public_* 契约生成 JSON Schema。

    Args:
        output_dir: 输出目录（默认 contracts/schemas/）

    Returns:
        生成的 Schema 文件路径列表
    """
    if output_dir is None:
        output_dir = CONTRACTS_DIR / "schemas"
    output_dir.mkdir(exist_ok=True)

    generated = []

    for yaml_file in CONTRACTS_DIR.glob("public_*.yaml"):
        contract = load_contract(yaml_file)
        schema = contract_to_json_schema(contract)

        table_name = contract["contract"].replace(".", "_")
        schema_path = output_dir / f"{table_name}.json"

        with open(schema_path, "w", encoding="utf-8") as f:
            json.dump(schema, f, ensure_ascii=False, indent=2)

        log.info(f"生成 Schema: {schema_path}")
        generated.append(schema_path)

    return generated


def validate_table_against_contract(
    table_name: str,
    contract_path: Optional[Path] = None,
    sample_size: int = 1000,
) -> Dict[str, Any]:
    """验证表数据是否符合契约。

    Args:
        table_name: 表名（如 public.st_status_pit）
        contract_path: 契约文件路径（默认自动查找）
        sample_size: 抽样验证行数

    Returns:
        {
            "table": str,
            "contract": str,
            "validated_rows": int,
            "violations": [...],
            "passed": bool,
        }
    """
    # 查找契约文件
    if contract_path is None:
        contract_name = table_name.replace(".", "_")
        contract_path = CONTRACTS_DIR / f"{contract_name}.yaml"

    if not contract_path.exists():
        return {
            "table": table_name,
            "contract": None,
            "validated_rows": 0,
            "violations": [{"type": "contract_not_found", "path": str(contract_path)}],
            "passed": False,
        }

    # 加载契约
    contract = load_contract(contract_path)
    columns = contract.get("columns", [])

    # 查询数据
    try:
        df = read_sql(f"SELECT * FROM {table_name} LIMIT {sample_size}")
    except Exception as e:
        return {
            "table": table_name,
            "contract": str(contract_path),
            "validated_rows": 0,
            "violations": [{"type": "query_failed", "error": str(e)}],
            "passed": False,
        }

    if df.empty:
        return {
            "table": table_name,
            "contract": str(contract_path),
            "validated_rows": 0,
            "violations": [],
            "passed": True,
            "detail": "表为空，跳过验证",
        }

    # 验证每一列
    violations = []

    for col_def in columns:
        col_name = col_def["name"]
        col_type = col_def.get("type", "TEXT")
        nullable = col_def.get("nullable", True)

        # 检查列是否存在
        if col_name not in df.columns:
            violations.append({
                "type": "column_missing",
                "column": col_name,
                "expected_type": col_type,
            })
            continue

        col_data = df[col_name]

        # 检查非空约束
        if not nullable:
            null_count = col_data.isna().sum()
            if null_count > 0:
                violations.append({
                    "type": "null_constraint_violated",
                    "column": col_name,
                    "null_count": int(null_count),
                    "sample": col_data[col_data.isna()].head(3).tolist(),
                })

        # 检查类型兼容性（简化版：检查是否能转换）
        # 更严格的类型检查需要 jsonschema 库
        if col_type in ("DATE", "TIMESTAMP", "TIMESTAMPTZ"):
            # 检查日期格式
            try:
                pd.to_datetime(col_data.dropna(), errors="raise")
            except Exception as e:
                violations.append({
                    "type": "date_format_invalid",
                    "column": col_name,
                    "error": str(e),
                    "sample": col_data.dropna().head(3).tolist(),
                })

        elif col_type in ("INTEGER", "BIGINT", "SMALLINT"):
            # 检查是否为整数
            non_null = col_data.dropna()
            if not pd.api.types.is_integer_dtype(non_null):
                try:
                    pd.to_numeric(non_null, errors="raise")
                except Exception as e:
                    violations.append({
                        "type": "integer_type_invalid",
                        "column": col_name,
                        "error": str(e),
                    })

        elif col_type in ("NUMERIC", "DECIMAL", "FLOAT", "DOUBLE", "REAL"):
            # 检查是否为数值
            non_null = col_data.dropna()
            if not pd.api.types.is_numeric_dtype(non_null):
                try:
                    pd.to_numeric(non_null, errors="raise")
                except Exception as e:
                    violations.append({
                        "type": "numeric_type_invalid",
                        "column": col_name,
                        "error": str(e),
                    })

    return {
        "table": table_name,
        "contract": str(contract_path),
        "validated_rows": len(df),
        "violations": violations,
        "passed": len(violations) == 0,
    }


def validate_all_public_tables(sample_size: int = 1000) -> Dict[str, Any]:
    """验证所有 public.* 表是否符合契约。

    Args:
        sample_size: 每个表抽样验证的行数

    Returns:
        {
            "tables_tested": int,
            "tables_passed": int,
            "tables_failed": int,
            "results": [...],
            "overall_passed": bool,
        }
    """
    log.info("=" * 60)
    log.info("契约测试自动化：验证所有 public.* 表")
    log.info("=" * 60)

    # 查找所有 public_* 契约
    contract_files = list(CONTRACTS_DIR.glob("public_*.yaml"))
    log.info(f"找到 {len(contract_files)} 个 public 契约")

    results = []

    for contract_file in contract_files:
        contract = load_contract(contract_file)
        table_name = contract["contract"]

        log.info(f"验证: {table_name}")
        result = validate_table_against_contract(
            table_name, contract_file, sample_size
        )
        results.append(result)

        status = "✓" if result["passed"] else "✗"
        log.info(f"  {status} {table_name}: {result['validated_rows']} rows, {len(result['violations'])} violations")

    # 汇总
    passed_count = sum(1 for r in results if r["passed"])
    failed_count = len(results) - passed_count

    summary = {
        "tables_tested": len(results),
        "tables_passed": passed_count,
        "tables_failed": failed_count,
        "results": results,
        "overall_passed": failed_count == 0,
    }

    log.info("=" * 60)
    log.info(f"契约测试汇总: {passed_count}/{len(results)} 通过")
    log.info("=" * 60)

    return summary


@app.command()
def generate_schemas(
    output_dir: str = typer.Option(None, help="输出目录"),
):
    """从 YAML 契约生成 JSON Schema。"""
    out = Path(output_dir) if output_dir else None
    generated = generate_all_schemas(out)
    typer.echo(f"已生成 {len(generated)} 个 JSON Schema")


@app.command()
def validate(
    table: str = typer.Option(None, help="指定表名（如 public.st_status_pit）"),
    sample_size: int = typer.Option(1000, help="抽样验证行数"),
    output: str = typer.Option(None, help="输出报告路径（JSON）"),
):
    """验证表数据是否符合契约。"""
    if table:
        result = validate_table_against_contract(table, sample_size=sample_size)
        results = {"tables_tested": 1, "results": [result], "overall_passed": result["passed"]}
    else:
        results = validate_all_public_tables(sample_size)

    if output:
        with open(output, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2, default=str)
        typer.echo(f"验证报告已保存: {output}")

    typer.echo(json.dumps(results, ensure_ascii=False, indent=2, default=str))

    if not results["overall_passed"]:
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
