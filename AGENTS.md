# AGENTS.md — nooshare-base

## 权威路由

| 对象 | 权威仓库 | 入口 |
|---|---|---|
| 仓库注册、主机与环境授权 | nooshare-workenv | workspace.yaml |
| 系统职责与跨仓边界 | nooshare-design | architecture/matrix.md |
| base 架构、数据契约、验收映射 | nooshare-design | architecture/nooshare-base.md |
| 数据流与上游坑清单 | nooshare-design | data-flow/matrix-data-flow.md, data-flow/upstream-pitfalls.md |
| public.* 数据产品契约 | 本仓库 | contracts/ |
| CLI 参考、数据字典、查询示例 | 本仓库 | docs/（自动生成） |
| 需求与验收标准 | 本仓库 | deliverables/ |

## 铁律

1. 只读源 namespaces（ju / tong / finance / capital），只写 public.*。
2. 下游只读 public.*，禁止直读源 namespace。
3. public.* 结构变更必须：contracts/ 版本递增 + DDL 同步。DDL inline 于 base/db.py 的 init_schema()。
4. 对账差异只记录不修正；修复在上游 repo 进行。
5. 构建失败必须非零退出；管线幂等——同 (输入快照, 代码 commit) 重跑产物一致。

## 操作要点

- 依赖管理用 uv；CLI 入口：base（13 个子命令）。
- 文档三件套（DATA_DICTIONARY / API_REFERENCE / USAGE_EXAMPLES）由 scripts/generate_docs.py 从 contracts/ 生成，勿手改。
- 测试：uv run pytest。
- CI：GitHub Actions 在合成数据上运行，不依赖生产数据库。
