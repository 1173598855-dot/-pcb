# Phase 2A：受控设计变更内核设计

日期：2026-07-29  
状态：已完成设计评审，等待实施计划  
适用仓库：PCBFlow

## 1. 文档目的

本文定义 PCBFlow 在 Phase 0/1 只读验证能力之上增加的第一个写入型纵向切片：

```text
需求草稿
  -> G1 冻结
  -> 确定性 DesignCommand
  -> 隔离 KiCad 原理图变更
  -> 语义 Diff 与 ERC
  -> 候选接受或拒绝
```

本切片的核心目标不是一次实现完整自动设计，而是建立后续原理图生成、PCB 自动化、AI 提案、审批和 Web UI 都必须复用的安全写入内核。

本文是总体蓝图
`docs/superpowers/specs/2026-07-29-automated-pcb-development-platform-design.md`
的 Phase 2A 补充规范。两者冲突时，以本文对 Phase 2A 的更具体约束为准；总体产品边界仍由总设计约束。

## 2. 当前基线

截至设计时，仓库已实现：

- Python 3.12/3.13 后端与 Typer CLI。
- FastAPI REST API。
- SQLAlchemy、Alembic 和 SQLite WAL。
- Project、Task、TaskAttempt、Artifact、Evidence、Finding。
- 任务租约、fencing token、崩溃后重新领取。
- KiCad 9.x `kicad-cli` 探测。
- 隔离副本中的只读 ERC/DRC。
- 内容寻址制品存储。
- 原始报告、规范化 Finding、API 和 CLI 查询。

当前注册项目仍以外部 `source_path` 为来源，Worker 只支持单次领取任务；不存在受控写入、项目 revision、需求冻结、审批和候选变更。

## 3. 目标

Phase 2A 必须交付：

1. 把已注册项目安全地采用为平台管理的独立 Git 工程。
2. 提供结构化需求草稿、规范化摘要、G1 审批和不可变冻结版本。
3. 定义严格、可审计、可幂等执行的 `DesignCommandBatch`。
4. 在隔离 Git worktree 中执行有限的原理图语义操作。
5. 使用真实解析、KiCad ERC 和语义 Diff 验证候选。
6. 允许人工接受或拒绝候选。
7. 确保失败、拒绝、过期和崩溃路径不改变权威工程。
8. 通过 API、CLI、任务和内容寻址证据公开整个流程。

## 4. 非目标

本切片不包含：

- 任意器件搜索和供应商实时数据。
- 完整器件修订管理界面。
- 自由生成任意新电路。
- 任意脚本、Shell 或 Python 命令。
- PCB 文件编辑、自动布局或自动布线。
- Gerber、钻孔、BOM、CPL 或嘉立创发布包。
- AI 模型调用。
- React Web UI。
- 完整 RBAC、多用户签名或远程团队模式。
- 常驻 Worker、心跳续租和进程树取消。
- KiCad 之外的 EDA 写入适配器。

Phase 2A 可以携带一个仅用于契约和端到端验证的受控层次模块。通用模块导入、审核和发布属于后续 Phase 2B。

## 5. 已评估方案

### 5.1 方案 A：结构化命令、语义 IR 和受控 KiCad 适配器

所有变更先表达为严格命令，再由领域适配器映射到 KiCad。读取层构建语义 IR，写入层操作保留未知字段的具体语法树。

优点：

- 命令、权限、前置条件和审计边界清晰。
- 可在没有 AI 和 Web UI 时独立运行。
- 可做稳定语义 Diff、幂等和属性测试。
- 可在将来替换底层 IPC 或文件适配器。

代价：

- 需要建立 KiCad S-expression CST、金样和兼容矩阵。
- 首批支持的操作必须刻意受限。

### 5.2 方案 B：KiCad IPC 优先

由运行中的 KiCad 进程执行所有编辑。

优点是减少文件格式耦合；缺点是原理图 IPC 能力、无人值守稳定性、版本兼容和 Windows 进程生命周期尚不足以作为唯一写入路径。

### 5.3 方案 C：模板生成器优先

使用固定模板生成完整工程或文本片段。

优点是演示速度快；缺点是难以安全修改现有工程、难以保留未知字段，并会导致模板与命令协议重复表达设计语义。

### 5.4 决策

采用方案 A。适配器端口允许未来优先使用经过契约测试的 KiCad IPC；当前缺失能力由受控 CST 适配器提供。不得使用正则表达式或无结构字符串替换工程文件。

## 6. 核心不变量

以下要求是实现和测试必须共同保证的系统不变量：

1. 外部导入目录永远不是自动写入目标。
2. 平台只修改自己创建的隔离 worktree。
3. 失败、拒绝和过期候选不能推进 `Project.current_revision`。
4. 每个命令批次绑定精确 `base_revision` 和冻结需求摘要。
5. 相同项目和幂等键只能对应一个逻辑命令批次。
6. 过期任务租约不能提交候选或接受结果。
7. 候选摘要变化后，已有审阅决定失效。
8. 只有通过强制验证的候选才能进入 `ready_for_review`。
9. AI、API 和 CLI 都不能绕过同一命令执行服务。
10. 数据库中的 `current_revision` 是工作流权威状态。
11. Git commit、语义 Diff、验证证据和审批对象必须能相互追溯。
12. 未知 KiCad 节点必须被保留；不支持的语义操作必须失败而不是猜测。

## 7. 总体架构

```text
CLI / REST API
      |
      v
RequirementsService ------ ApprovalService
      |                           |
      v                           v
RequirementStore          GateDecisionStore
      |
      v
DesignCommandService
      |
      v
TaskRepository -> Worker -> ProposalExecutor
                              |
                              +-> RevisionService
                              +-> SchematicAdapter
                              +-> SemanticDiffService
                              +-> KicadPort
                              +-> Artifact/Evidence
                                      |
                                      v
                              ChangeProposalStore
```

领域服务只依赖端口，不直接依赖 FastAPI、Typer、SQLAlchemy 或具体 Git 命令。API 和 CLI 只负责解析输入、鉴权上下文、调用服务以及稳定错误映射。

## 8. 项目采用与设计真源

### 8.1 采用流程

`project adopt` 接受一个现有 Project：

1. 确认外部来源目录存在且仍满足项目复制限制。
2. 拒绝符号链接、Windows reparse point 和路径逃逸。
3. 创建 `data_dir/projects/<project_id>/repo.git` 裸 Git 仓库。
4. 在平台临时 worktree 中复制外部来源，但排除来源目录中的 `.git` 元数据。
5. 若来源本身是 Git 工作树，记录可读取的 source HEAD 作为 provenance，不直接继承 hooks、配置或隐藏引用。
6. 若工程尚无标准目录结构，保留现有相对路径，不强制搬迁 KiCad 文件。
7. 生成不包含自引用 revision 的最小 `pcbflow.yaml`。
8. 生成首个导入提交和规范化项目快照摘要。
9. 创建 `refs/heads/design`。
10. 在数据库事务中记录 managed 模式和当前 revision。
11. 原始来源目录继续作为 `import_source_path` 审计信息，不再参与写入流程。

项目采用是幂等操作。相同导入内容重复调用返回已有 managed 状态；外部内容变化时必须使用后续显式重新导入流程，不能静默覆盖。

### 8.2 路径布局

```text
.pcbflow-data/
├─ pcbflow.db
├─ artifacts/
├─ projects/
│  └─ <project_id>/
│     └─ repo.git/
└─ workspaces/
   ├─ validation-<run>/
   └─ proposal-<run>/
```

数据库只保存相对仓库键或项目 ID，不把可移动的数据目录绝对路径写入项目清单。

### 8.3 Revision

API 中 revision 使用 `git:<object-id>`。另行计算的项目快照摘要使用 `sha256:<digest>`，两者不得混用。

快照摘要由受控项目文件的相对路径、类型、字节摘要和文件大小构成。必须排除：

- `.git`。
- KiCad 临时锁文件。
- 平台 worktree 元数据。
- 已登记的本地缓存和构建输出。

排除规则必须版本化并进入快照元数据。

提交到 Git 的 `pcbflow.yaml` 不保存 `current_revision`，因为 commit 不能可靠包含自己的 object ID。`current_revision` 由数据库和 `refs/heads/design` 投影，在 API、CLI 或生成的只读诊断视图中展示。总体设计第 6.1 节中的该字段按此规则解释。

## 9. 结构化需求与 G1

### 9.1 RequirementSet

`RequirementSet` 包含：

- `id`
- `project_id`
- `base_revision`
- `status`
- `schema_version`
- `requirements`
- `interfaces`
- `power_rails`
- `assumptions`
- `verification_items`
- `canonical_digest`
- `candidate_revision`
- `candidate_snapshot_digest`
- `frozen_revision`
- `created_at`
- `submitted_at`
- `frozen_at`

状态机：

```text
draft -> pending_approval -> frozen
              |
              v
           rejected

frozen -> superseded
```

`pending_approval` 之后内容不可修改。任何修改都创建新的 RequirementSet。`frozen_revision` 必须是后续 DesignCommand `base_revision` 的祖先，且 RequirementSet 必须仍是项目的 active requirement set。

### 9.2 最小需求字段

每条需求至少包含：

- `id`
- `kind`
- `statement`
- `rationale`
- `priority`
- `source`
- `verification_method`
- `acceptance_criteria`

`kind` 只能是：

```text
functional
interface
power
environment
mechanical
manufacturing
cost
compliance
verification
```

未知字段、非法单位、重复 ID、悬空引用和非有限数值必须被拒绝。

### 9.3 规范化与摘要

摘要输入是严格模型导出的规范化 JSON：

- UTF-8。
- 对象键按字典序排列。
- 数组顺序只在具有业务顺序时保留。
- 单位在输入边界转换为规定 SI 表达。
- 禁止 NaN 和 Infinity。
- 不包含数据库 ID、时间戳和展示字段。

规范化结果以 `application/vnd.pcbflow.requirements+json` 保存为 Artifact，并计算 SHA-256。

### 9.4 G1

提交 G1 前必须：

- Schema 校验通过。
- 不存在 blocking assumption。
- 验证方法和验收条件完整。
- 所有引用可解析。
- RequirementSet 基线仍等于项目当前 revision。

提交操作在隔离 worktree 中把规范化需求写入：

```text
requirements/product.yaml
requirements/interfaces.yaml
requirements/power-tree.yaml
requirements/assumptions.yaml
requirements/verification.yaml
```

随后更新 `pcbflow.yaml` 中不自引用的 active requirement set 候选元数据，创建候选 commit，并用 `refs/pcbflow/requirements/<requirement_set_id>` 保持可达。YAML 是规范化 JSON 的确定性人类视图；提交前必须重新解析并证明摘要一致。

G1 `subject_digest` 对以下规范化对象计算 SHA-256：

```json
{
  "schema_version": "1.0",
  "project_id": "prj_...",
  "requirement_set_id": "reqset_...",
  "requirements_digest": "sha256:...",
  "base_revision": "git:...",
  "candidate_revision": "git:...",
  "candidate_snapshot_digest": "sha256:..."
}
```

`GateDecision` 签署：

```text
gate = G1
subject_type = requirement_set
subject_id
subject_digest
project_id
base_revision
candidate_revision
candidate_snapshot_digest
decision
actor
comment
created_at
```

批准时再次比较项目 revision；随后在一个数据库事务中推进 `Project.current_revision`、更新 active requirement set、写入 ProjectRevision、记录 GateDecision 和 outbox 事件。事务后由协调器更新 `refs/heads/design`。批准后 RequirementSet 变为 `frozen` 并记录 `frozen_revision`，旧 active RequirementSet 变为 `superseded`。拒绝后保持不可变并标记 `rejected`，不改变项目 revision，用户通过创建新草稿继续修改。

## 10. DesignCommand 协议

### 10.1 命令批次

```json
{
  "schema_version": "1.0",
  "batch_id": "bat_...",
  "project_id": "prj_...",
  "base_revision": "git:...",
  "requirement_set_id": "reqset_...",
  "idempotency_key": "project:revision:intent",
  "actor": {
    "type": "human",
    "id": "local-user"
  },
  "intent": "Instantiate the verified input protection module",
  "risk": "medium",
  "commands": []
}
```

批次字段必须使用 Pydantic 严格模式，拒绝类型强转和未知字段。

### 10.2 单条命令

```json
{
  "schema_version": "1.0",
  "command_id": "cmd_...",
  "batch_id": "bat_...",
  "project_id": "prj_...",
  "base_revision": "git:...",
  "idempotency_key": "project:revision:intent:ordinal",
  "actor": {
    "type": "human",
    "id": "local-user"
  },
  "intent": "Add the verified input protection module",
  "risk": "medium",
  "preconditions": [],
  "operation": {
    "type": "schematic.instantiate_module",
    "payload": {}
  },
  "required_validations": [
    "schema",
    "semantic_diff",
    "kicad_erc"
  ],
  "provenance": {
    "requirement_ids": ["REQ-PWR-001"],
    "evidence_ids": [],
    "module_revision_ids": ["modrev_..."]
  }
}
```

服务必须验证批次中每条命令的项目、批次、基线和 actor 与外层一致。

### 10.3 幂等

- `project_id + batch.idempotency_key` 唯一。
- `project_id + command.idempotency_key` 唯一。
- 相同键和相同规范化输入返回已有对象。
- 相同键和不同输入返回 `IDEMPOTENCY_CONFLICT`。
- 已创建候选的批次不得再次执行产生第二个候选。

### 10.4 前置条件

首批支持：

- `project.revision_equals`
- `requirements.digest_equals`
- `schematic.object_exists`
- `schematic.property_equals`
- `schematic.module_absent`
- `tool.capability_available`

前置条件只读求值。任何 `false` 或 `unknown` 都阻断命令，错误中返回失败条件和可定位对象。

## 11. 首批原理图操作

### 11.1 `schematic.instantiate_module`

载荷包含：

- `module_revision_id`
- `instance_name`
- `target_sheet_ref`
- `parameter_bindings`
- `port_bindings`
- `placement_slot` 或 `auto`

模块修订必须：

- 状态为 `verified`。
- 模板和 manifest 摘要匹配。
- 声明兼容的 KiCad 主版本和适配器契约。
- 参数、端口和禁用组合通过 Schema。

Phase 2A 使用层次原理图模块。实例化会复制独立子页并在父页添加层次 sheet 和端口连接，避免把任意符号文本拼入现有页。

模块通过 `ModuleCatalogPort` 从 `PCBFLOW_MODULE_CATALOG_DIR` 只读加载。目录中的 manifest、模板和引用资源必须通过内容摘要互相绑定；Phase 2A 不提供修改目录或把模块标记为 verified 的 API。仓库测试夹具提供一个参考模块，实际使用者需要显式配置经过人工审核的本地目录。

模块本地 UUID 通过 UUIDv5 从以下输入派生：

```text
project_id
batch_id
command_id
module_revision_digest
module_local_uuid
```

因此同一逻辑执行产生相同对象身份。

### 11.2 `schematic.set_property`

载荷包含对象语义引用、属性名、新值和可选旧值。允许的属性由白名单控制。引用标号、价值、描述和用户字段可以分开授权；KiCad 内部字段和 UUID 不可通过该命令修改。

### 11.3 `schematic.assign_footprint`

载荷引用受控封装修订，不接受任意绝对路径。适配器写入规范化库标识，并把封装修订摘要加入 provenance。

Phase 2A 只允许使用随验证模块或测试目录提供的只读封装修订清单；通用封装导入、审核和发布接口属于 Phase 2B。

### 11.4 `schematic.add_label`

目标必须是已有引脚、层次端口、线段端点或已知网络的语义引用。命令不得仅提供裸坐标。网络名经过命名、作用域和冲突规则检查。

### 11.5 暂不支持

Phase 2A 不支持：

- 任意 `schematic.add_component`。
- 任意几何线段绘制。
- 总线重构。
- 多根原理图消歧。
- 自动注释全工程。
- 删除未知对象。

这些操作必须在后续规范中逐项增加，不能通过通用 escape hatch 绕过。

## 12. KiCad CST 与语义 IR

### 12.1 CST

CST tokenizer 必须识别：

- 圆括号。
- bare atom。
- quoted string 及转义。
- 空白和换行。
- UTF-8 文本。
- 未知节点。

未改变的 token 范围按原始字节输出；只有被修改或新增的子树使用规范化格式化器输出。解析器必须设置文件大小、嵌套深度、节点数和字符串长度上限。

### 12.2 语义 IR

首批 IR 包含：

- SchematicDocument
- Sheet
- HierarchicalPort
- Symbol
- SymbolProperty
- PinReference
- Label
- NetConnectivity
- FootprintAssignment

对象身份优先使用 KiCad UUID；引用标号和名称是可变展示属性，不作为唯一身份。

### 12.3 适配器端口

```python
class SchematicAdapter(Protocol):
    def inspect(self, project: Path) -> SchematicDocument: ...
    def apply(
        self,
        project: Path,
        commands: tuple[DesignCommand, ...],
    ) -> ApplyResult: ...
```

`ApplyResult` 包含修改文件、命令结果、修改后 IR 和适配器能力报告。

### 12.4 能力检测

适配器按能力选择：

1. 已通过契约测试的 KiCad IPC 操作。
2. KiCad 9 受控 CST 操作。
3. 明确返回 `DESIGN_COMMAND_UNSUPPORTED`。

不得因检测到更高版本就假定能力存在。

## 13. 语义 Diff

修改前后 IR 通过稳定对象身份比较。首批差异类型：

- `sheet_added`
- `sheet_removed`
- `symbol_added`
- `symbol_removed`
- `symbol_property_changed`
- `footprint_changed`
- `label_added`
- `label_removed`
- `net_connectivity_changed`

每条差异包含：

- `kind`
- `subject_ref`
- `before`
- `after`
- `command_id`
- `requirement_ids`
- `risk`

差异按类型和对象引用稳定排序。规范化 JSON 保存为 Artifact 并计算摘要。文本 Git diff 也必须保存，但不能代替语义 Diff。

过滤项只能是明确登记的非语义噪声。不得忽略 UUID、连接关系、封装、层次页或用户字段变化。

Proposal `review_digest` 对以下规范化对象计算 SHA-256：

```json
{
  "schema_version": "1.0",
  "proposal_id": "prop_...",
  "project_id": "prj_...",
  "base_revision": "git:...",
  "candidate_revision": "git:...",
  "candidate_snapshot_digest": "sha256:...",
  "requirement_set_digest": "sha256:...",
  "semantic_diff_digest": "sha256:...",
  "evidence_set_digest": "sha256:...",
  "adapter_capability_digest": "sha256:..."
}
```

任一字段变化都会产生新的审阅对象，旧决定不可复用。

## 14. 候选执行

状态机：

```text
queued -> executing
             |
             +-> validation_failed
             |
             +-> ready_for_review
                        |
                        +-> accepted
                        +-> rejected
                        +-> stale
```

执行步骤：

1. 读取命令批次并验证租约 token 与租约未过期。
2. 检查项目 managed 状态、当前 revision 和冻结需求。
3. 求值所有前置条件。
4. 从精确基线创建临时 worktree。
5. 解析修改前 IR。
6. 按顺序执行全部命令。
7. 解析修改后文件。
8. 运行 CST 回环与项目路径检查。
9. 生成语义 Diff 和文本 Diff。
10. 如果没有任何语义变化，返回 `DESIGN_COMMAND_NO_EFFECT`。
11. 运行强制验证。
12. 保存日志、Diff、ERC 和命令结果制品。
13. 使用受控作者身份创建候选 commit。
14. 创建 `refs/pcbflow/proposals/<proposal_id>`。
15. 持久化候选 revision、摘要和证据集合。
16. 进入 `ready_for_review`。

在候选状态、proposal ref 和任务完成结果写入前必须再次验证 fencing token。租约失效后的执行最多留下可识别、可回收的孤立 Git 对象或 proposal ref，不能提交数据库事实。

候选 commit 的作者、提交者、时间和消息模板来自不可变批次元数据，不使用执行机器当前时间。相同 parent、tree 和批次元数据应产生相同 commit object，便于崩溃后的幂等恢复。

强制验证集合是策略要求与命令声明的并集。调用方不能通过少报 `required_validations` 移除：

- Schema。
- 前置条件。
- 路径与文件限制。
- 修改后解析。
- 语义 Diff。
- KiCad ERC。

## 15. 接受、拒绝与恢复

### 15.1 接受

接受前检查：

- 候选状态为 `ready_for_review`。
- 请求携带的 `candidate_digest` 与 proposal 的 `review_digest` 一致。
- `base_revision == Project.current_revision`。
- RequirementSet 仍为 frozen。
- 强制验证全部通过。
- 候选 Git object 和 proposal ref 可读取。

数据库事务：

1. 通过项目 `version` 做乐观并发比较。
2. 更新 `Project.current_revision`。
3. 写入 `ProjectRevision`。
4. 写入 `gate=DESIGN_CHANGE` 的接受 `GateDecision`。
5. 标记 proposal 为 accepted。
6. 写入 outbox 事件。

事务提交后，协调器把 `refs/heads/design` 比较交换到 candidate revision。若 Git ref 更新失败，数据库 revision 仍为权威；后续协调器重试。

### 15.2 拒绝

拒绝写入带 actor 和 reason 的决定，将 proposal 标记为 rejected，不修改项目 revision。proposal ref 在保留期内继续存在。

### 15.3 过期

接受时基线不匹配：

- 返回 HTTP 409。
- 错误码为 `PROJECT_REVISION_CONFLICT`。
- proposal 标记为 stale。
- 不自动 rebase 或重放。
- 用户必须基于新 revision 创建新批次。

### 15.4 启动协调

启动时扫描：

- accepted proposal 与 `refs/heads/design` 不一致。
- ready proposal 缺失 proposal ref。
- pending RequirementSet 缺失 requirements candidate ref。
- Git proposal ref 指向数据库未知候选。
- managed 项目数据库状态与 managed repo 创建状态不一致。
- 数据库 current revision 在仓库中不存在。

可修复情况自动修复并记录审计；对象缺失或摘要不一致时阻断项目写入并返回终止错误。

RevisionService 始终从数据库记录的 commit object ID 创建 worktree，不依赖 `refs/heads/design` 已经完成刷新。

## 16. 数据模型

迁移文件：`alembic/versions/0002_controlled_design_changes.py`。

### 16.1 projects 增量字段

- `mode`：`registered|managed`
- `managed_repo_key`
- `current_revision`
- `project_snapshot_digest`
- `active_requirement_set_id`
- `managed_at`
- `version`

现有 `source_path` 保留为初始导入来源。

### 16.2 project_revisions

- `id`
- `project_id`
- `revision`
- `parent_revision`
- `snapshot_digest`
- `requirement_set_id`
- `command_batch_id`
- `created_at`

`project_id + revision` 唯一。

### 16.3 requirement_sets

- `id`
- `project_id`
- `base_revision`
- `schema_version`
- `status`
- `payload_json`
- `canonical_digest`
- `canonical_artifact_digest`
- `candidate_revision`
- `candidate_snapshot_digest`
- `frozen_revision`
- `created_at`
- `submitted_at`
- `frozen_at`

### 16.4 gate_decisions

- `id`
- `project_id`
- `gate`
- `subject_type`
- `subject_id`
- `subject_digest`
- `base_revision`
- `idempotency_key`
- `decision`
- `actor_type`
- `actor_id`
- `comment`
- `created_at`

决策追加写，不更新历史行；`project_id + idempotency_key` 唯一。

### 16.5 design_command_batches

- `id`
- `project_id`
- `base_revision`
- `requirement_set_id`
- `idempotency_key`
- `actor_json`
- `intent`
- `risk`
- `commands_json`
- `canonical_digest`
- `created_at`

`commands_json` 是提交时的不可变规范化批次快照；`design_commands` 行用于逐命令唯一约束、查询和结果关联，两者摘要必须一致。

### 16.6 design_commands

- `id`
- `batch_id`
- `project_id`
- `ordinal`
- `idempotency_key`
- `operation_type`
- `payload_json`
- `canonical_digest`

`batch_id + ordinal`、`project_id + idempotency_key` 和 `batch_id + id` 分别唯一。

### 16.7 change_proposals

- `id`
- `project_id`
- `command_batch_id`
- `task_id`
- `status`
- `candidate_revision`
- `candidate_snapshot_digest`
- `review_digest`
- `semantic_diff_digest`
- `evidence_set_digest`
- `result_json`
- `last_error_code`
- `created_at`
- `updated_at`
- `version`

`command_batch_id` 唯一。

### 16.8 outbox_events

- `id`
- `aggregate_type`
- `aggregate_id`
- `event_type`
- `payload_json`
- `created_at`
- `processed_at`
- `attempt_count`
- `last_error_code`

## 17. 代码模块

```text
src/pcbflow/
├─ requirements.py
├─ requirement_store.py
├─ commands.py
├─ approvals.py
├─ proposals.py
├─ proposal_store.py
├─ revisions.py
├─ design_tables.py
└─ schematic/
   ├─ __init__.py
   ├─ cst.py
   ├─ semantic.py
   ├─ adapter.py
   ├─ modules.py
   └─ diff.py
```

现有模块修改边界：

- `domain.py`：仅添加跨子系统共享的枚举和值对象。
- `tasks.py`：保持通用 Worker，不放入设计逻辑。
- `validation.py`：通过 RevisionService 物化 managed 项目 revision。
- `container.py`：组装新服务和 task handler。
- `api.py`：增加请求/响应模型和路由。
- `cli.py`：增加命令组。
- `repositories.py`：不继续塞入新设计仓储。

### 17.1 依赖决策

- Git 使用已安装的 Git CLI 和受控 ProcessPort，不引入 GitPython。
- YAML 输入使用 `PyYAML>=6.0.2,<7` 的安全加载器；需求摘要仍以规范化 JSON 为准。
- Settings 增加可选 `module_catalog_dir`，来源为 `PCBFLOW_MODULE_CATALOG_DIR`。
- 不依赖第三方 KiCad 对象模型承担可信写入，避免未知字段被静默丢弃。
- CST 和语义 IR 保持内部接口，后续可以在契约不变时替换实现。

## 18. CLI 与 REST API

### 18.1 CLI

```text
pcbflow project adopt <project-id>
pcbflow requirements import <project-id> --file <requirements.yaml>
pcbflow requirements show <requirement-set-id>
pcbflow requirements submit <requirement-set-id>
pcbflow approval decide <subject-id> --subject-digest <sha256:...> --approve|--reject
pcbflow proposal create <project-id> --file <commands.json>
pcbflow proposal show <proposal-id>
pcbflow proposal diff <proposal-id>
pcbflow proposal accept <proposal-id> --candidate-digest <sha256:...>
pcbflow proposal reject <proposal-id> --reason <text>
pcbflow worker --once
```

所有创建、提交和决定操作提供 `--idempotency-key`。

### 18.2 REST

```text
POST /api/v1/projects/{project_id}:adopt
POST /api/v1/projects/{project_id}/requirement-sets
GET  /api/v1/requirement-sets/{requirement_set_id}
POST /api/v1/requirement-sets/{requirement_set_id}:submit
POST /api/v1/approvals
POST /api/v1/projects/{project_id}/proposals
GET  /api/v1/proposals/{proposal_id}
GET  /api/v1/proposals/{proposal_id}/diff
POST /api/v1/proposals/{proposal_id}:accept
POST /api/v1/proposals/{proposal_id}:reject
```

创建和决定请求要求 `Idempotency-Key`。审批请求必须携带 `subject_digest`，候选接受请求必须携带 `candidate_digest`。远程模式下不接受任意本机文件路径；需求和命令通过请求体或受限上传制品提供。

## 19. 错误协议

新增稳定错误码：

| 错误码 | 可重试 | 含义 |
| --- | --- | --- |
| `REQUIREMENTS_SCHEMA_INVALID` | 否 | 结构、类型、单位或引用非法 |
| `REQUIREMENTS_BLOCKED` | 否 | 存在阻断假设或缺失验收项 |
| `APPROVAL_DIGEST_MISMATCH` | 否 | 决策对象已变化 |
| `PROJECT_NOT_MANAGED` | 否 | 写入操作作用于未采用项目 |
| `PROJECT_WORKTREE_DIRTY` | 否 | 平台管理工作区出现未登记修改 |
| `PROJECT_REVISION_CONFLICT` | 否 | 基线已变化 |
| `DESIGN_COMMAND_SCHEMA_INVALID` | 否 | 命令合同不合法 |
| `DESIGN_COMMAND_PRECONDITION_FAILED` | 否 | 前置条件为 false 或 unknown |
| `DESIGN_COMMAND_UNSUPPORTED` | 否 | 当前适配器不支持操作 |
| `MODULE_REVISION_NOT_FOUND` | 否 | 模块不存在或未验证 |
| `KICAD_PARSE_FAILED` | 否 | KiCad 文件无法安全解析 |
| `KICAD_ROUNDTRIP_FAILED` | 否 | 写入后回环不满足合同 |
| `DESIGN_COMMAND_NO_EFFECT` | 否 | 新批次没有语义变化 |
| `CANDIDATE_VALIDATION_FAILED` | 否 | 强制验证未通过 |
| `CANDIDATE_NOT_REVIEWABLE` | 否 | 候选状态不允许决定 |
| `GIT_OPERATION_FAILED` | 视原因 | Git 操作失败 |
| `REVISION_RECONCILIATION_REQUIRED` | 否 | 自动协调无法证明安全 |

错误响应沿用统一错误信封，包含 correlation ID、可定位详情和用户可执行 action，不返回内部堆栈。

## 20. 证据与审计

每个候选至少产生：

- `design_command_batch`
- `project_snapshot_before`
- `project_snapshot_after`
- `git_text_diff`
- `schematic_semantic_diff`
- `kicad_erc`
- `command_execution_log`
- `adapter_capability_report`
- `approval_signature` 或拒绝决定

Evidence 必须引用 project、task、proposal、candidate revision 和 artifact digest。审计事件记录 actor、动作、对象、前后摘要、结果和 correlation ID。

## 21. 安全边界

- Git 子进程使用参数数组，不通过 Shell。
- worktree 路径必须位于平台分配根目录。
- 复制和解析前检查链接、文件数和总大小。
- 模块包限制文件类型、文件数、大小、路径和压缩比。
- 不信任模块 manifest 中的绝对路径。
- 不执行 KiCad 工程内脚本。
- Git 配置使用平台受控作者信息，不读取项目自定义 hooks。
- 所有外部工具使用环境白名单、超时和输出上限。
- 日志移除用户目录、token 和不必要的工程内容。

## 22. 测试策略

### 22.1 单元测试

- RequirementSet 严格 Schema。
- 规范化和摘要。
- 状态转换。
- G1 摘要绑定。
- 命令批次和单命令幂等。
- 前置条件求值。
- UUIDv5 派生。
- 语义对象引用。
- 语义 Diff 稳定排序。
- 错误映射。

### 22.2 属性测试

- 任意合法 Unicode 字符串 CST 回环。
- 任意合法空白布局不改变未编辑字节。
- 相同语义需求产生相同摘要。
- 任意命令排序变化会改变批次摘要。
- 不合法路径无法逃逸 workspace。
- 相同命令输入产生相同生成 UUID。

### 22.3 金样测试

至少包含：

- 空白原理图。
- 单页简单原理图。
- 层次原理图。
- Unicode 名称。
- Windows 空格路径。
- 多单元器件。
- 自定义属性。
- 有意 ERC 错误。
- 一个可实例化的验证模块。

每个金样验证：

- 解析。
- 无变更字节回环。
- 受控命令。
- 语义 Diff。
- KiCad ERC。

### 22.4 集成测试

- 注册项目采用为 managed repo。
- 初始 Git commit 与快照。
- 需求导入、提交、批准和冻结。
- 候选 worktree 创建与清理。
- 候选 commit 和 proposal ref。
- 接受后 current revision 推进。
- 拒绝后 current revision 不变。
- 基线变化导致 stale。
- 重复幂等键返回同一对象。
- 过期 fencing token 无法提交。

### 22.5 故障恢复

故障注入点：

- 创建 commit 前。
- 创建 proposal ref 后、数据库写入前。
- 数据库接受事务后、design ref 更新前。
- 保存 Diff 制品中断。
- Worker 执行后失去租约。

每个注入点必须证明可恢复或安全终止，不能留下被误认为已接受的半成品。

### 22.6 E2E

参考路径：

```text
注册现有 KiCad fixture
  -> adopt
  -> 导入需求
  -> 提交并批准 G1
  -> 创建实例化模块命令
  -> worker 执行
  -> 查看语义 Diff 和 ERC
  -> 接受候选
  -> 重启应用
  -> 从新 revision 运行只读验证
```

## 23. 可观测性

新增日志上下文：

- `project_id`
- `requirement_set_id`
- `command_batch_id`
- `proposal_id`
- `base_revision`
- `candidate_revision`
- `task_id`
- `trace_id`

新增指标：

- 候选执行时长。
- 解析和 ERC 时长。
- 候选验证失败率。
- revision 冲突率。
- proposal 审阅等待时间。
- Git ref 协调重试次数。
- 每个适配器合同的成功率。

## 24. 兼容与迁移

- 现有 registered 项目仍可运行只读验证。
- 只有 managed 项目可以执行 DesignCommand。
- `project adopt` 不修改外部目录。
- 旧 API 和 CLI 保持兼容。
- 新迁移只能前滚增加表和可空字段；数据回填在应用服务中幂等完成。
- SQLite 是 Phase 2A 唯一支持数据库；PostgreSQL 合同留到团队模式。

## 25. 交付顺序

1. RevisionService 与 managed project。
2. RequirementSet、规范化和 G1。
3. DesignCommand Schema 与持久化。
4. KiCad CST、语义 IR 和无变更回环。
5. 首批原理图命令。
6. 语义 Diff。
7. 候选任务执行和证据。
8. 接受、拒绝和协调恢复。
9. API、CLI 和 E2E。
10. 文档、金样和故障注入。

每一步必须采用 TDD，并在独立可审查提交中保持全量测试通过。

## 26. 完成定义

Phase 2A 只有同时满足以下条件才算完成：

- 原始导入目录在所有测试路径中字节不变。
- 未通过验证的候选不能进入审阅。
- 失败、拒绝和 stale 候选不能改变 current revision。
- 相同幂等键不能产生两个批次、候选或接受决定。
- 过期 fencing token 不能提交。
- 候选可通过真实 KiCad 9 解析和 ERC。
- 语义 Diff 可定位到命令和需求。
- 接受流程在故障注入后能够协调恢复。
- 重启后 Project、RequirementSet、Proposal、Evidence 和 revision 一致。
- 旧只读验证流程保持通过。
- 全量测试覆盖率不低于 90%。
- CLI `--help`、OpenAPI、README 和开发指导与实现一致。

## 27. 后续衔接

Phase 2A 完成后，后续子项目按以下顺序独立设计和实施：

1. Phase 2B：器件修订、受控模块目录和更完整原理图操作。
2. Phase 2C：AI 需求编译、原理图规划和独立审查提案。
3. Phase 3A：PCB 板框、层叠、网络类和约束初始化。
4. Phase 3B：确定性布局评分、关键相对布局和协同布线。
5. Phase 4A：制造输出、BOM/CPL 和嘉立创规则包。
6. Phase 4B：发布候选、G4 和嘉立创 EDA 专业版交换。
7. Phase 5A：常驻 Worker、取消、心跳和可观测性。
8. Phase 5B：完整审批 Web UI、团队模式和生产强化。

每个子项目必须拥有独立设计规范、实施计划和可运行纵向验收路径。
