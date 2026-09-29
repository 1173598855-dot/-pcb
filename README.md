# PCBFlow

[![CI](https://github.com/1173598855-dot/-pcb/actions/workflows/ci.yml/badge.svg)](https://github.com/1173598855-dot/-pcb/actions/workflows/ci.yml)

PCBFlow 是一个本地优先、以证据为中心的自动化 PCB 开发工作流。当前仓库已实现
Phase 0/1 的只读验证切片和 Phase 2A 的受控设计变更内核：注册本地 KiCad 工程，
探测 `kicad-cli`，在隔离副本中运行 ERC/DRC，持久化任务、原始证据和规范化
Finding，并将项目采纳到受管 Git，冻结 G1 需求，在隔离 worktree 中执行受控
原理图命令，记录语义 Diff 与 ERC 证据后接受或拒绝候选。

后续增量已实现 BoardIR-only 的 PCB 布局、受限布线、GND 铜皮规划、候选 G3/G4
和制造发布清单校验；这不等同于真实 LCEDA Pro 原生写入、原生 DRC 或真实制造
发布。AI 设计和 Web UI 仍不在当前范围内。

核心原则是 **fail-closed**：真实外部写入能力（LCEDA 原生写入、原生 DRC、真实
制造发布）在通过官方 bridge capability gate 验证之前一律以稳定错误码失败，
绝不伪造成功结果。系统全程依赖幂等键重放、内容寻址的不可变证据（SHA-256）
和 fencing token 租约。

## 已实现能力

- **只读验证（Phase 0/1）**：项目注册、`kicad-cli` 探测（9.x/10.x profile）、
  隔离副本 ERC/DRC、任务队列与租约恢复、原始证据与规范化 Finding、任务取消。
- **受控设计变更（Phase 2A）**：受管 Git 采纳、G1 需求冻结与审批、五种严格
  原理图操作的提案、隔离 worktree 执行、语义 Diff、显式 accept/reject。
- **PCB 自动化（BoardIR-only）**：布局、受限布线、GND 铜皮规划、候选
  G3/G4 门禁、制造发布清单校验。
- **已验证组件目录**：元件修订导入与 KiCad 模块绑定（manifest digest 冻结）。
- **接口**：CLI（Typer）、本地 REST API（FastAPI）、MCP 服务器（stdio）。

被能力门阻断、当前不提供：LCEDA Pro 原生写入、原生 DRC、真实制造发布、
AI 自动设计、任意元件/导线编辑、完整 Web UI、PostgreSQL、供应商网络访问。

## KiCad 兼容性矩阵

| KiCad major | Profile | CLI validation | Controlled schematic writes |
| --- | --- | --- | --- |
| 9.x | kicad-9-v1 | Supported | Supported |
| 10.x | kicad-10-v1 | Supported | Supported |
| Other | none | Rejected | Rejected |

`PCBFLOW_KICAD_CLI` 选择唯一生效的可执行文件。`doctor --json` 会报告其精确
版本、可执行文件摘要、major、profile id 与 profile revision。

## LCEDA Pro capability gate

`pcbflow doctor --json` 同时报告 `kicad_cli` 与 `lceda_pro`。LCEDA Pro 结果
请用专用的只读 probe：

```powershell
.\.venv\Scripts\pcbflow.exe eda probe lceda-pro --json
```

将 `PCBFLOW_LCEDA_PRO_EXECUTABLE` 指向 GUI 可执行文件可采集其版本与 SHA-256
摘要；`PCBFLOW_LCEDA_PRO_OFFICIAL_BRIDGE` 记录候选的厂商支持 CLI、API 或插件
路径。但仅配置这两个路径永远不构成写入验证：只有当版本明确的官方 bridge 通过
冻结的最小 create/save/reopen/readback fixture 契约时，`write_verified` 才会
变为 `true`。该契约只能证明 bridge 显式上报的 `snapshot`、`create_candidate`
和 `apply_operations` 结果；`run_drc` 与 `export_release` 需要单独的官方证据。

在此之前，每个原生写入请求都以
`LCEDA_PRO_WRITE_CAPABILITY_UNVERIFIED` 失败；discovery 和两个 probe 命令
从不写入原生工程。

## 前置条件

- Windows PowerShell。
- Python 3.12 或 3.13。
- Git；仅开发验证和查看差异时需要。
- 可选：KiCad 9.x 或 10.x 的 `kicad-cli`。可将其加入 `PATH`，或通过
  `PCBFLOW_KICAD_CLI` 指定完整路径。

未安装 KiCad 时，项目注册、受管采纳、G1 和查询功能仍可使用；真实验证任务与
候选 ERC 会以稳定错误码 `KICAD_CLI_UNAVAILABLE` 终止，不会伪造成功结果。

## 安装

在仓库根目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

默认运行数据写入仓库下的 `.pcbflow-data/`。可在运行任何命令前覆盖：

```powershell
$env:PCBFLOW_DATA_DIR = "C:\pcbflow-data"
```

## 数据库迁移与测试

应用命令启动时会自动把数据库迁移到最新版本，也可以手动执行：

```powershell
.\.venv\Scripts\python.exe -m alembic -c alembic.ini upgrade head
.\.venv\Scripts\python.exe -m pytest -q
```

只运行真实 KiCad 契约测试：

```powershell
.\.venv\Scripts\python.exe -m pytest -m kicad -v
```

如果本机没有 `kicad-cli`，该契约测试会明确跳过。

## 检查本机能力

```powershell
.\.venv\Scripts\pcbflow.exe doctor --json
```

输出中的 `kicad_cli` 始终包含以下字段：

```json
{
  "available": false,
  "path": null,
  "version": null,
  "executable_digest": null,
  "reason": "kicad_cli_not_found",
  "major": null,
  "profile_id": null,
  "profile_revision": null
}
```

只有检测到受支持的 KiCad 9.x 或 10.x profile 时，`available` 才为 `true`。

## 完整 CLI 工作流

以下示例假设 KiCad 工程位于 `C:\work\controller`；请替换为自己的实际路径。
工程根目录必须至少包含一个 `.kicad_sch` 或 `.kicad_pcb` 文件，并且每种最多一个。

### 1. 注册项目

```powershell
$project = .\.venv\Scripts\pcbflow.exe project add "C:\work\controller" `
  --name "Controller" `
  --idempotency-key "controller-project-v1" `
  --json | ConvertFrom-Json

$project.id
```

同一个幂等键和相同输入可安全重试；用同一个键提交不同输入会返回冲突。
列出已注册项目：`.\.venv\Scripts\pcbflow.exe project list --json`。

### 2. 排队只读验证

```powershell
$task = .\.venv\Scripts\pcbflow.exe validate $project.id `
  --idempotency-key "controller-validation-r1" `
  --json | ConvertFrom-Json

$task.id
```

### 3. 运行 Worker 任务

生产环境使用常驻 Worker（收到 SIGTERM/SIGINT 后等待活动任务完成并优雅关闭）：

```powershell
.\.venv\Scripts\pcbflow.exe worker --run
```

开发和测试使用单任务模式：

```powershell
.\.venv\Scripts\pcbflow.exe worker --once --json
```

Worker 行为通过 `PCBFLOW_WORKER_*` 环境变量配置（并发槽位、轮询间隔、租约
续期、关闭超时），完整清单见下文[配置](#配置环境变量)。

检查 Worker 健康状态（读取常驻 Worker 原子发布的 `worker-state.json`）：

```powershell
.\.venv\Scripts\pcbflow.exe worker health --json
```

未检测到运行中的常驻 Worker 快照时，命令以稳定错误码
`WORKER_STATE_UNAVAILABLE` 退出；也可以使用 `--file` 检查指定快照文件。

### 4. 查询任务、证据与 Finding

```powershell
.\.venv\Scripts\pcbflow.exe task show $task.id --json
.\.venv\Scripts\pcbflow.exe task cancel $task.id --reason "operator requested cancellation" --idempotency-key cancel-task-001 --json
.\.venv\Scripts\pcbflow.exe evidence $project.id --json
.\.venv\Scripts\pcbflow.exe findings $project.id --json
```

`task show` 读取同一 SQLite 数据库，跨进程可见。若 Worker 在任务处于
`leased` 或 `running` 时退出，租约过期后，新的 Worker 可用新的 fencing token
接管任务；旧 token 不能再提交结果。

`task cancel` 可取消 `queued`、`retry_wait`、`leased` 或 `running` 任务。
取消会持久化为终态 `cancelled`，保留取消时间和原因，并将活动 TaskAttempt
记录为 `cancelled`；重复请求返回首次持久化的取消快照。REST 使用
`POST /api/v1/tasks/{task_id}:cancel`，原因会去除首尾空白，不能为空且最多
1000 个字符。运行中的外部工具会复用既有的 Windows 进程树或 Linux 进程组
终止路径。取消不会修改注册的源工程；候选工作仍由现有的 fencing 和恢复流程
保护。

## 受控设计变更（Phase 2A）

Phase 2A 是已实现的写入切片：把外部 KiCad 工程采纳进受管 Git，在 G1 冻结
结构化需求集，在隔离 worktree 中执行一种受控原理图操作，记录语义与工具证据，
并等待显式 accept/reject 决策。采纳后注册的 `source_path` 永不写入。

### 采纳、冻结 G1 并提交提案

```powershell
.\.venv\Scripts\python.exe -m pcbflow project adopt <project-id> `
  --idempotency-key controller-adopt --json
.\.venv\Scripts\python.exe -m pcbflow requirements import <project-id> `
  --file requirements.yaml --idempotency-key requirements-import --json
.\.venv\Scripts\python.exe -m pcbflow requirements show <requirement-set-id> --json
.\.venv\Scripts\python.exe -m pcbflow requirements submit <requirement-set-id> `
  --idempotency-key requirements-submit --json
.\.venv\Scripts\python.exe -m pcbflow approval decide <requirement-set-id> `
  --subject-digest sha256:<digest> --approve --actor-id reviewer `
  --comment "G1 approved" --idempotency-key g1-approval --json
```

创建一个 JSON DesignCommand 批次并用 `proposal create` 提交。支持的五种操作
类型为 `schematic.instantiate_module`、`schematic.instantiate_bound_module`、
`schematic.set_property`、`schematic.assign_footprint` 和
`schematic.add_label`。

当某个元件/模块绑定已为其 KiCad major 冻结了允许的模块时，使用
`schematic.instantiate_bound_module`。该操作是严格的：payload 只包含
`component_module_binding_id`，不接受调用方自选的 `module_revision_id` 或
manifest digest。

```json
{
  "schema_version": "1.0",
  "batch_id": "bat_bound_status_led",
  "project_id": "prj_controller",
  "base_revision": "git:1111111111111111111111111111111111111111",
  "requirement_set_id": "reqset_controller_v1",
  "idempotency_key": "bound-status-led",
  "actor": {"type": "human", "id": "reviewer"},
  "intent": "Instantiate the frozen status LED",
  "risk": "medium",
  "commands": [
    {
      "schema_version": "1.0",
      "command_id": "cmd_bound_status_led",
      "batch_id": "bat_bound_status_led",
      "project_id": "prj_controller",
      "base_revision": "git:1111111111111111111111111111111111111111",
      "idempotency_key": "bound-status-led:1",
      "actor": {"type": "human", "id": "reviewer"},
      "intent": "Instantiate the frozen status LED",
      "risk": "medium",
      "preconditions": [],
      "operation": {
        "type": "schematic.instantiate_bound_module",
        "payload": {
          "component_module_binding_id": "compmod_status_led_v1",
          "instance_name": "STATUS_LED",
          "target_sheet_ref": {
            "kind": "sheet",
            "sheet_uuid": "00000000-0000-0000-0000-000000000001",
            "object_uuid": "00000000-0000-0000-0000-000000000001",
            "pin_number": null
          },
          "parameter_bindings": {"LED_VALUE": "GREEN"},
          "port_bindings": {},
          "placement_slot": "auto"
        }
      },
      "required_validations": ["semantic_diff"],
      "provenance": {
        "requirement_ids": ["REQ-FUNC-001"],
        "evidence_ids": [],
        "module_revision_ids": ["modrev_status_led_v1"]
      }
    }
  ]
}
```

执行时 Worker 会重载不可变绑定与配置的模块目录。活动 KiCad major 与实时
已验证 manifest digest 必须与绑定一致；否则提案记录终态解析错误，不写
原理图、不创建候选修订或提案引用。现有的 REST/CLI 批量提交接口直接支持该
操作，无需新端点或命令。

### 审阅与决策

```powershell
.\.venv\Scripts\python.exe -m pcbflow proposal create <project-id> `
  --file commands.json --idempotency-key proposal-create --json
.\.venv\Scripts\python.exe -m pcbflow worker --once --json
.\.venv\Scripts\python.exe -m pcbflow proposal show <proposal-id> --json
.\.venv\Scripts\python.exe -m pcbflow proposal diff <proposal-id> --json
.\.venv\Scripts\python.exe -m pcbflow proposal accept <proposal-id> `
  --candidate-digest sha256:<review-digest> --actor-id reviewer `
  --comment "accepted" --idempotency-key proposal-accept --json
# 或者拒绝候选：
.\.venv\Scripts\python.exe -m pcbflow proposal reject <proposal-id> `
  --reason "needs another review" --actor-id reviewer `
  --idempotency-key proposal-reject --json
```

## 已验证组件目录与模块绑定

本地 `component.yaml` 可以声明一个已验证的厂商元件修订，以及其配套数据手册、
引脚定义、KiCad 符号、封装和可选 STEP 模型的 SHA-256 摘要。导入会把规范化
manifest 和全部声明的证据复制进 PCBFlow 的内容寻址数据目录；命令与 API
响应只暴露元数据和 artifact 摘要，从不暴露源路径或资产字节。

```powershell
pcbflow component import C:\components\LED-0603-RED\component.yaml `
  --idempotency-key acme-led-red-rev-a --json
pcbflow component show <component-revision-id> --json
pcbflow component list --component-key Acme:LED-0603-RED --json

pcbflow component bind-module <component-revision-id> `
  --kicad-major 10 `
  --module-revision-id modrev_status_led_v1 `
  --idempotency-key component-module-v1 --json
pcbflow component bindings <component-revision-id> --json
```

`PCBFLOW_MODULE_CATALOG_DIR` 配置元件绑定使用的服务器端已验证模块目录。
创建绑定会冻结所选模块的 manifest digest；同一元件修订与 KiCad major 不能
重新绑定到不同模块。该目录不联系供应商、不生成 BOM、不选择替代料、不修改
KiCad 工程。本地路径导入在远程模式下禁用。

## PCB 候选生命周期

`POST /api/v1/projects/{project_id}/pcb-candidates` 和
`pcbflow pcb candidate create` 目前只创建 `boardir_only` 候选。候选会冻结
项目 authority、base revision/snapshot、BoardIR snapshot、规则包、capability
evidence、操作序列和算法 seed；相同项目和幂等键只能重放完全相同的冻结输入，
显式改变 BoardIR、capability 或 seed 会返回 `IDEMPOTENCY_CONFLICT`。

候选行与内部 `pcb.generate_candidate` 任务在同一数据库事务中写入。Worker
在取消或租约失效时不会发布候选状态：取消会镜像为 `cancelled`，租约在状态写入
后失效时会通过版本比较恢复旧状态。不存在的候选任务以
`PCB_CANDIDATE_NOT_FOUND` 终止。未通过官方 bridge capability gate 时，任务只会
保留 `boardir_only` 结果并标记为受阻，不会写入原生嘉立创专业版工程。

创建时传入的 capability digest 必须与 capability gate 返回的已验证证据完全
一致；KiCad authority、缺少 authority 或缺少验证证据都会以
`PCB_CAPABILITY_GATE_BLOCKED` 拒绝。CLI 与 REST 共用候选输入约束，拒绝负
seed、重复或空白 net id 以及非 SHA-256 digest。无论候选状态或调用方的 JSON
声明为何，当前公开视图的 `output_kind` 均固定为 `boardir_only`，直到后续
阶段持久化并验证原生候选或发布证据为止。

### 候选与发布命令

```powershell
pcbflow pcb candidate create <project-id> --idempotency-key <key> --json
pcbflow pcb candidate show <candidate-id> --json
pcbflow pcb candidate approve-g3 <candidate-id> `
  --candidate-digest sha256:<digest> --actor-id <id> `
  --comment "<text>" --approve --idempotency-key <key> --json
pcbflow pcb release export <candidate-id> --idempotency-key <key> --json
pcbflow pcb release approve-g4 <candidate-id> `
  --manifest-digest sha256:<digest> --actor-id <id> `
  --comment "<text>" --approve --idempotency-key <key> --json
```

隔离正向 fixture 已走通 `g3_approved -> ready_for_g4 -> released`，但它仅
证明 fixture 路径，绝不是 LCEDA 原生写入或发布证明。迁移往返必须以编程方式
覆盖 `sqlalchemy.url` 指向隔离数据库；不要对默认 `alembic.ini` URL 执行降级。

## 启动本地 REST API

```powershell
.\.venv\Scripts\pcbflow.exe serve --host 127.0.0.1 --port 8765
```

另开一个 PowerShell 窗口检查服务：

```powershell
Invoke-RestMethod http://127.0.0.1:8765/health
Invoke-RestMethod http://127.0.0.1:8765/api/v1/projects
```

当前 API 操作：

- `GET /health`
- `POST /api/v1/projects`
- `GET /api/v1/projects`
- `POST /api/v1/projects/{project_id}/eda-authority`
- `POST /api/v1/projects/{project_id}/eda-capability-probes`
- `POST /api/v1/projects/{project_id}:adopt`
- `POST /api/v1/projects/{project_id}/pcb-candidates`
- `GET /api/v1/pcb-candidates/{candidate_id}`
- `POST /api/v1/pcb-candidates/{candidate_id}:approve-g3`
- `POST /api/v1/pcb-candidates/{candidate_id}:export-release`
- `POST /api/v1/pcb-candidates/{candidate_id}:approve-g4`
- `POST /api/v1/projects/{project_id}/requirement-sets`
- `GET /api/v1/requirement-sets/{requirement_set_id}`
- `POST /api/v1/requirement-sets/{requirement_set_id}:submit`
- `POST /api/v1/approvals`
- `POST /api/v1/projects/{project_id}/proposals`
- `GET /api/v1/proposals/{proposal_id}`
- `GET /api/v1/proposals/{proposal_id}/diff`
- `POST /api/v1/proposals/{proposal_id}:accept`
- `POST /api/v1/proposals/{proposal_id}:reject`
- `POST /api/v1/projects/{project_id}/validations`
- `GET /api/v1/tasks/{task_id}`
- `POST /api/v1/tasks/{task_id}:cancel`
- `POST /api/v1/worker:run-once`
- `GET /api/v1/projects/{project_id}/evidence`
- `GET /api/v1/projects/{project_id}/findings`
- `POST /api/v1/component-revisions`
- `GET /api/v1/component-revisions/{component_revision_id}`
- `GET /api/v1/component-revisions`
- `POST /api/v1/component-revisions/{component_revision_id}/module-bindings`
- `GET /api/v1/component-revisions/{component_revision_id}/module-bindings`

写操作需要 `Idempotency-Key` 请求头。若服务以远程模式启动，本地路径注册会被
拒绝；除 `/health` 外的每个端点都要求 `Authorization: Bearer <PCBFLOW_API_TOKEN>`
请求头。服务器把经过认证的远程操作记录为配置的服务 actor，而不是信任请求方
提供的 actor ID。本地源注册与 REST worker 执行端点在远程模式下保持禁用；
worker 应在受信的本地进程中运行。

```powershell
$env:PCBFLOW_REMOTE_MODE = "true"
$env:PCBFLOW_API_TOKEN = "replace-with-a-long-random-secret"
.\.venv\Scripts\pcbflow.exe serve --host 127.0.0.1 --port 8765
```

## MCP 服务器

`pcbflow-mcp`（入口 `pcbflow.mcp_server:app`）通过 stdio 暴露一个名为
`pcbflow-enhanced` 的 MCP 服务器，把项目、工作流、EDA 能力、artifact 和历史
查询操作提供给兼容 MCP 的 AI 助手：

| 工具 | 说明 |
| --- | --- |
| `pcbflow_register_project` | 登记本地 PCB 项目路径 |
| `pcbflow_run_workflow` | 运行 AI 协作工作流（多模型路由与回退） |
| `pcbflow_query_chains` | 查询可用协作链及其模型 |
| `pcbflow_list_history` | 查询协作历史 |
| `pcbflow_list_projects` | 列出已登记项目 |
| `pcbflow_read_artifacts` | 读取项目证据制品 |
| `pcbflow_query_eda_capabilities` | 查询 EDA 能力 |

模型后端支持 ANTHROPIC、OPENAI、LOCAL、AZURE 等端点类型，凭据从
`ANTHROPIC_API_KEY`、`OPENAI_API_KEY`、`LOCAL_API_KEY` 等环境变量读取；
内置健康检查与熔断，后端不可用时返回结构化错误而不是伪造成功。启动：

```powershell
.\.venv\Scripts\pcbflow-mcp.exe
```

## 持久化数据与制品

默认目录布局：

```text
.pcbflow-data/
├── pcbflow.db
├── worker-state.json
├── artifacts/
│   └── objects/sha256/aa/bb/<完整 SHA-256>
├── projects/<project-id>/repo.git/
└── workspaces/
```

- SQLite 保存 Project、Task、TaskAttempt、Artifact、Evidence 和 Finding 元数据，
  并启用 WAL、外键和 5000 ms busy timeout。
- 原始 ERC/DRC JSON 以 `sha256:<digest>` 内容地址不可变保存；重复字节不会产生
  第二份对象。
- Evidence 引用原始 Artifact；Finding 只保存规范化规则、严重级别、对象和消息。
- `workspaces/` 只保存正在执行的隔离临时目录，不是设计真源。

任务正常结束时会删除对应的临时 workspace。若进程被强制终止，可能留下孤立
目录；确认没有 Worker 正在使用后，可以人工删除 `workspaces/pcbflow-validation-*`。
数据库与 Artifact 不依赖这些临时副本。

## 配置（环境变量）

`PCBFLOW_DATABASE_URL` 和 `PCBFLOW_ARTIFACT_DIR` 的显式值优先于根据
`PCBFLOW_DATA_DIR` 推导出的默认位置。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `PCBFLOW_DATA_DIR` | `./.pcbflow-data` | 数据根目录 |
| `PCBFLOW_DATABASE_URL` | 派生自数据目录 | 数据库 URL（仅 SQLite） |
| `PCBFLOW_ARTIFACT_DIR` | `<数据目录>/artifacts` | 内容寻址制品库 |
| `PCBFLOW_KICAD_CLI` | 自动探测 | KiCad CLI 可执行文件路径 |
| `PCBFLOW_LCEDA_PRO_EXECUTABLE` | 无 | LCEDA Pro GUI 可执行文件路径；未配置时探测报告 `lceda_pro_not_found`，不会自动探测 |
| `PCBFLOW_LCEDA_PRO_OFFICIAL_BRIDGE` | - | 候选官方自动化桥路径；配置不代表写入已验证 |
| `PCBFLOW_MODULE_CATALOG_DIR` | - | 已验证模块目录 |
| `PCBFLOW_TASK_LEASE_SECONDS` | `180` | 任务租约时长（秒） |
| `PCBFLOW_PROCESS_TIMEOUT_SECONDS` | `120` | 外部进程超时（秒） |
| `PCBFLOW_MAX_PROCESS_OUTPUT_BYTES` | `2000000` | stdout/stderr 各自的最大字节数 |
| `PCBFLOW_MAX_PROJECT_FILES` | `10000` | 项目最大文件数 |
| `PCBFLOW_MAX_PROJECT_BYTES` | `1000000000` | 项目最大总字节数 |
| `PCBFLOW_MAX_API_BODY_BYTES` | `1000000` | API 请求体最大字节数 |
| `PCBFLOW_MAX_KICAD_DESIGN_FILE_BYTES` | `50000000` | 单个设计文件最大字节数 |
| `PCBFLOW_MAX_KICAD_REPORT_BYTES` | `10000000` | 单个报告最大字节数 |
| `PCBFLOW_TASK_RETRY_MAX_ATTEMPTS` | `5` | 任务重试次数上限 |
| `PCBFLOW_TASK_RETRY_BASE_SECONDS` | `5` | 重试退避基数（秒） |
| `PCBFLOW_TASK_RETRY_MAX_DELAY_SECONDS` | `300` | 重试退避上限（秒） |
| `PCBFLOW_REMOTE_MODE` | `false` | 启用远程模式 |
| `PCBFLOW_API_TOKEN` | - | 远程模式 API 令牌（必填） |
| `PCBFLOW_API_ACTOR_ID` | `remote-api` | 服务 actor ID |
| `PCBFLOW_WORKER_SLOTS` | `1` | 并发任务槽位数（1-10） |
| `PCBFLOW_WORKER_POLL_SECONDS` | `5` | 空闲轮询间隔（秒） |
| `PCBFLOW_WORKER_POLL_MAX_SECONDS` | `60` | 最大退避间隔（秒） |
| `PCBFLOW_WORKER_HEARTBEAT_SECONDS` | 自动 | 租约续期间隔；默认取 `min(30, 租约/3)` |
| `PCBFLOW_WORKER_SHUTDOWN_TIMEOUT_SECONDS` | `300` | 优雅关闭超时（60-600 秒） |
| `PCBFLOW_WORKER_ID` | 自动生成 | Worker 唯一标识符 |

## 当前限制

- 仅支持 SQLite；原理图写入限于显式验证的 KiCad 9.x/10.x profile，KiCad PCB
  适配器保持只读，LCEDA 原生写入必须通过官方 bridge capability gate。
- 每种设计文件在工程根目录中最多一个；多个根原理图或 PCB 会被判定为歧义工程。
- Phase 2A 支持五种受控操作：直接模块实例化、绑定模块实例化、属性设置、
  封装指派和标签添加。
- 不包含 AI 自动设计、任意元件/导线编辑、完整 Web UI 或 PostgreSQL；PCB 自动化
  仅覆盖文档中冻结的 BoardIR V1 范围。系统可在隔离候选中生成 BoardIR-only
  PCB 结果并校验制造发布制品；未验证官方 LCEDA bridge 时不会写入原生工程、
  运行原生 DRC 或声明真实制造发布成功。
- 不修改注册的外部 `source_path`，不访问供应商网络。

## 开发验证

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pytest --cov=pcbflow --cov-report=term-missing --cov-fail-under=90
.\.venv\Scripts\python.exe -m pytest -m kicad -v
.\.venv\Scripts\pcbflow.exe doctor --json
.\.venv\Scripts\python.exe -m pcbflow --help
git diff --check
```

快速检查与并行运行：

```powershell
# lint（pycodestyle 实用子集 + pyflakes + isort + bugbear + simplify + pyupgrade）
.\.venv\Scripts\ruff.exe check src tests
# 类型检查已清零并纳入 CI 门禁
.\.venv\Scripts\mypy.exe src/pcbflow

# 并行全量回归（pytest-xdist），显著快于串行
.\.venv\Scripts\python.exe -m pytest -q -n 2

# 每个 Python 测试默认带 600s 超时（pytest-timeout，见 pyproject addopts）
# 全量套件在 filterwarnings = ["error"] 下必须零警告
```

提交钩子（首次克隆后执行一次）：

```powershell
.\.venv\Scripts\pre-commit.exe install
```

可复现的依赖版本见 `requirements-lock.txt`（由通过全量回归的 venv 冻结）。
CI 在每次 push/PR 时运行 ruff、mypy，并在 Windows runner 的 Python 3.12 与
3.13 矩阵上执行带覆盖率门禁（90%）的全量测试；另有一个咨询性 Linux job
（允许失败）为 POSIX 分支积累证据。

已知怪癖：pytest-cov 只接受完整包目标 `--cov=pcbflow`；子模块目标（如
`--cov=pcbflow.approvals`）在本依赖组合下会在收集阶段因 numpy 的
"cannot load module more than once per process" 失败。请用全包目标并从
`term-missing` 报告中读取单模块数据。

## 文档

- `docs/DEVELOPMENT_GUIDE.md` — 开发指南
- `docs/OPTIMIZATION_GUIDE.md` — 优化顺序与每次增量的验证记录
- `docs/PROJECT_STATUS.md` — 按日期的项目整理与实测记录
- `docs/QUICK_REFERENCE.md` — 故障排查、部署与性能速查
- `docs/DEVELOPMENT_SUMMARY.md` — 2026-08 产品愿景草案（未实现，仅存档）
- `docs/superpowers/specs/` — 设计规格
- `docs/superpowers/plans/` — 实施计划
