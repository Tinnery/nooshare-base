# AGENTS.md — nooshare-base

## 公共仓库文档禁令（硬约束）

本仓库是 public repo，以下规则优先于任何“按需写文档”的冲动：

1. **禁止文档目录**：不得新建或保留 `docs/`、`doc/`、`documentation/`、`deliverables/`、`reports/`、`notes/`、`decisions/`、`history/` 等文档或过程目录。
2. **禁止生成文字材料**：数据字典、CLI 参考、查询示例、需求、设计、报告、评审记录、笔记等文字文件（`.md` / `.rst` / `.pdf` / `.docx` 等）不得在本仓库生成或提交。生成脚本默认写入被忽略的 `.generated/`；需要正式版本时输出到 nooshare-design，不在本仓库留档。
3. **README.md 只做入口**：仅保留标题与一句话定位。`AGENTS.md` 只放规则与路由。
4. **文字材料唯一归属**：所有文字材料必须维护在私有仓库 `nooshare-design` 的 `implementation-docs/nooshare-base/` 下；本仓库只引用，不复制。
5. **违规处理**：发现本仓库出现上述目录或文字材料，不就地修订——先迁移到 nooshare-design，再删除本地文件，并更新下方索引表。

### 文字材料索引（nooshare-design）

| 材料 | 权威位置（nooshare-design） |
|------|------------------------------|
| 数据字典 | `implementation-docs/nooshare-base/DATA_DICTIONARY.md` |
| CLI / API 参考 | `implementation-docs/nooshare-base/API_REFERENCE.md` |
| 使用示例 | `implementation-docs/nooshare-base/USAGE_EXAMPLES.md` |
| 文档索引 | `implementation-docs/nooshare-base/README.md` |
| 需求说明书 | `implementation-docs/nooshare-base/01-需求说明书-nooshare-base.md` |
| 契约目录说明 | `implementation-docs/nooshare-base/contracts-README.md` |
| 架构、数据契约、验收映射 | `architecture/nooshare-base.md` |
| 数据流与上游坑清单 | `data-flow/matrix-data-flow.md`、`data-flow/upstream-pitfalls.md` |

通过 `workspace.yaml` 定位 nooshare-design 的 checkout 路径后读取上述文件。

## 权威路由

| 对象 | 权威仓库 | 入口 |
|---|---|---|
| 仓库注册、主机与环境授权 | nooshare-workenv | workspace.yaml |
| 系统职责与跨仓边界 | nooshare-design | architecture/matrix.md |
| base 架构、数据契约、验收映射 | nooshare-design | architecture/nooshare-base.md |
| 数据流与上游坑清单 | nooshare-design | data-flow/matrix-data-flow.md, data-flow/upstream-pitfalls.md |
| 数据字典 / CLI 参考 / 查询示例 / 需求说明书 | nooshare-design | 见上方文字材料索引 |
| public.* 数据产品契约（YAML，非文字材料） | 本仓库 | contracts/ |
| 需求与验收标准（系统级） | nooshare-design | architecture/nooshare-base.md |

## 铁律

1. 只读源 namespaces（ju / tong / finance / capital），只写 public.*。
2. 下游只读 public.*，禁止直读源 namespace。
3. public.* 结构变更必须：contracts/ 版本递增 + DDL 同步。DDL inline 于 base/db.py 的 init_schema()。
4. 对账差异只记录不修正；修复在上游 repo 进行。
5. 构建失败必须非零退出；管线幂等——同 (输入快照, 代码 commit) 重跑产物一致。

## 操作要点

- 依赖管理用 uv；CLI 入口：base（13 个子命令）。
- 数据字典 / API 参考 / 使用示例由 `scripts/generate_docs.py` 从 `contracts/` 生成：
  - 默认输出到被 `.gitignore` 忽略的 `.generated/`，不进版本控制；
  - 需要更新 nooshare-design 中的正式版本时，设置 `NOOSHARE_DOCS_OUT=<nooshare-design checkout>/implementation-docs/nooshare-base` 后重新生成，并在 design 仓提交。
- 测试：uv run pytest。
- CI：GitHub Actions 在合成数据上运行，不依赖生产数据库。

## 主机约束

本仓库只在 pro 主机开发和运行。禁止 clone 到 Windows 主机（travis-hp）。如果在 Windows 主机上发现本仓库的 checkout，应删除该 checkout 并修正来源（不正确的 clone 操作或 workspace 配置）。
