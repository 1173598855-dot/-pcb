# PCBFlow

PCBFlow 是一个本地优先、以证据为中心的自动化 PCB 开发工作流。本仓库当前实现的是 Phase 0/1 的首个可恢复纵向切片：注册本地 KiCad 工程，探测 `kicad-cli`，在隔离副本中运行 ERC/DRC，并持久化任务、原始证据和规范化 Finding。

当前切片严格只读：不会修改注册的 KiCad 源文件，不会生成 Gerber、BOM、坐标或其他制造包，也不包含 AI 自动设计、Web UI、自动改图或嘉立创导出。

## 前置条件

- Windows PowerShell。
- Python 3.12 或 3.13。
- Git；仅开发验证和查看差异时需要。
- 可选：KiCad 9.x 的 `kicad-cli`。可将其加入 `PATH`，或通过 `PCBFLOW_KICAD_CLI` 指定完整路径。

未安装 KiCad 时，项目注册、持久任务和查询功能仍可使用；真实验证任务会以稳定错误码 `KICAD_CLI_UNAVAILABLE` 终止，不会伪造成功结果。

## 安装

在仓库根目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

如需显式指定 KiCad CLI：

```powershell
$env:PCBFLOW_KICAD_CLI = "C:\Program Files\KiCad\9.0\bin\kicad-cli.exe"
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
  "reason": "kicad_cli_not_found"
}
```

只有检测到受支持的 KiCad 9.x 时，`available` 才为 `true`。

## 完整 CLI 工作流

以下示例假设 KiCad 工程位于 `C:\work\controller`；请替换为自己的实际路径。工程根目录必须至少包含一个 `.kicad_sch` 或 `.kicad_pcb` 文件，并且每种最多一个。

### 1. 注册项目

```powershell
$project = .\.venv\Scripts\pcbflow.exe project add "C:\work\controller" `
  --name "Controller" `
  --idempotency-key "controller-project-v1" `
  --json | ConvertFrom-Json

$project.id
```

同一个幂等键和相同输入可安全重试；用同一个键提交不同输入会返回冲突。

列出已注册项目：

```powershell
.\.venv\Scripts\pcbflow.exe project list --json
```

### 2. 排队只读验证

```powershell
$task = .\.venv\Scripts\pcbflow.exe validate $project.id `
  --idempotency-key "controller-validation-r1" `
  --json | ConvertFrom-Json

$task.id
```

### 3. 运行一个 Worker 任务

```powershell
.\.venv\Scripts\pcbflow.exe worker --once --json
```

Worker 会先检查工程中的链接、Windows reparse point、文件数和总字节数，再把工程复制到 `.pcbflow-data/workspaces/` 下的临时隔离目录。ERC/DRC 只针对该副本运行，临时目录在任务结束后删除。

### 4. 查询任务、证据与 Finding

```powershell
.\.venv\Scripts\pcbflow.exe task show $task.id --json
.\.venv\Scripts\pcbflow.exe evidence $project.id --json
.\.venv\Scripts\pcbflow.exe findings $project.id --json
```

关闭终端或结束当前进程后，再次运行 `task show` 会读取同一 SQLite 数据库。若 Worker 在任务处于 `leased` 或 `running` 时退出，租约过期后，新的 Worker 可用新的 fencing token 接管任务；旧 token 不能再提交结果。

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
- `POST /api/v1/projects/{project_id}/validations`
- `GET /api/v1/tasks/{task_id}`
- `POST /api/v1/worker:run-once`
- `GET /api/v1/projects/{project_id}/evidence`
- `GET /api/v1/projects/{project_id}/findings`

两个 `POST` 创建操作需要 `Idempotency-Key` 请求头。若服务以远程模式启动，本地路径注册会被拒绝：

```powershell
$env:PCBFLOW_REMOTE_MODE = "true"
.\.venv\Scripts\pcbflow.exe serve --host 127.0.0.1 --port 8765
```

## 持久化数据与制品

默认目录布局：

```text
.pcbflow-data/
├── pcbflow.db
├── artifacts/
│   └── objects/sha256/aa/bb/<完整 SHA-256>
└── workspaces/
```

- SQLite 保存 Project、Task、TaskAttempt、Artifact、Evidence 和 Finding 元数据，并启用 WAL、外键和 5000 ms busy timeout。
- 原始 ERC/DRC JSON 以 `sha256:<digest>` 内容地址不可变保存；重复字节不会产生第二份对象。
- Evidence 引用原始 Artifact；Finding 只保存规范化规则、严重级别、对象和消息。
- `workspaces/` 只保存正在执行的隔离临时目录，不是设计真源。

任务正常结束时会删除对应的临时 workspace。若进程被强制终止，可能留下孤立目录；确认没有 Worker 正在使用后，可以人工删除 `workspaces/pcbflow-validation-*`。数据库与 Artifact 不依赖这些临时副本。

可配置的主要环境变量：

- `PCBFLOW_DATA_DIR`
- `PCBFLOW_DATABASE_URL`
- `PCBFLOW_ARTIFACT_DIR`
- `PCBFLOW_KICAD_CLI`
- `PCBFLOW_TASK_LEASE_SECONDS`
- `PCBFLOW_PROCESS_TIMEOUT_SECONDS`
- `PCBFLOW_MAX_PROCESS_OUTPUT_BYTES`
- `PCBFLOW_MAX_PROJECT_FILES`
- `PCBFLOW_MAX_PROJECT_BYTES`
- `PCBFLOW_REMOTE_MODE`

`PCBFLOW_DATABASE_URL` 和 `PCBFLOW_ARTIFACT_DIR` 的显式值优先于根据 `PCBFLOW_DATA_DIR` 推导出的默认位置。

## 当前限制

- 仅支持 KiCad 9.x CLI 的只读 ERC/DRC。
- 每种设计文件在工程根目录中最多一个；多个根原理图或 PCB 会被判定为歧义工程。
- Worker 当前只提供 `--once` 单任务模式。
- 不修改 KiCad 工程，不生成制造资料，不访问供应商网络。
- 不包含自动设计、审批流、React UI、PostgreSQL 或生产部署能力。

## 开发验证

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pytest --cov=pcbflow --cov-report=term-missing --cov-fail-under=80
.\.venv\Scripts\python.exe -m pytest -m kicad -v
.\.venv\Scripts\pcbflow.exe doctor --json
git diff --check
```

设计规范与实施计划位于：

- `docs/DEVELOPMENT_GUIDE.md`
- `docs/superpowers/specs/2026-07-29-automated-pcb-development-platform-design.md`
- `docs/superpowers/specs/2026-07-29-phase-2a-controlled-design-change-kernel-design.md`
- `docs/superpowers/plans/2026-07-29-phase-0-1-read-only-validation.md`

## Phase 2A controlled-change CLI

The controlled workflow is available from the same CLI. Write operations
require a distinct `--idempotency-key`; JSON output is stable and intended for
automation.

```powershell
pcbflow project adopt <project-id> --idempotency-key <key> --json
pcbflow requirements import <project-id> --file <requirements.yaml> --idempotency-key <key> --json
pcbflow requirements show <requirement-set-id> --json
pcbflow requirements submit <requirement-set-id> --idempotency-key <key> --json
pcbflow approval decide <requirement-set-id> --subject-digest <sha256:...> --approve --actor-id <id> --comment <text> --idempotency-key <key> --json
pcbflow proposal create <project-id> --file <commands.json> --idempotency-key <key> --json
pcbflow proposal show <proposal-id> --json
pcbflow proposal diff <proposal-id> --json
pcbflow proposal accept <proposal-id> --candidate-digest <sha256:...> --actor-id <id> --comment <text> --idempotency-key <key> --json
pcbflow proposal reject <proposal-id> --reason <text> --actor-id <id> --idempotency-key <key> --json
```

## Phase 2A controlled design change kernel

Phase 2A is the implemented write-capable slice. It adopts an external KiCad
project into managed Git, freezes a structured requirement set at G1, executes
one of the four strict schematic operations in an isolated worktree, records
semantic and tool evidence, and waits for an explicit accept or reject
decision. The registered `source_path` is never written after adoption.

### Initialize and configure

Use Python 3.12 or 3.13, Git, and KiCad 9.x when proposal ERC or the real-KiCad
contract is required. The first CLI invocation runs the Alembic migrations;
they can also be run explicitly:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m alembic -c alembic.ini upgrade head
```

Set `PCBFLOW_KICAD_CLI` to the KiCad 9 `kicad-cli` executable when it is not on
`PATH`. `PCBFLOW_MODULE_CATALOG_DIR` points at verified module revisions used by
the instantiate operation.

### Adopt, freeze G1, and propose

The command arguments below match `python -m pcbflow ... --help`. Every write
command requires an idempotency key and replays only the same canonical input.

```powershell
$p = .\.venv\Scripts\python.exe -m pcbflow project add C:\work\controller `
  --name Controller --idempotency-key controller-add --json
# Read the returned project id, then:
.\.venv\Scripts\python.exe -m pcbflow project adopt <project-id> `
  --idempotency-key controller-adopt --json
.\.venv\Scripts\python.exe -m pcbflow requirements import <project-id> `
  --file requirements.yaml --idempotency-key requirements-import --json
.\.venv\Scripts\python.exe -m pcbflow requirements submit <requirement-set-id> `
  --idempotency-key requirements-submit --json
.\.venv\Scripts\python.exe -m pcbflow approval decide <requirement-set-id> `
  --subject-digest sha256:<digest> --approve --actor-id reviewer `
  --comment "G1 approved" --idempotency-key g1-approval --json
```

Create a JSON DesignCommand batch with `proposal create`. The supported
operation types are `schematic.instantiate_module`,
`schematic.set_property`, `schematic.assign_footprint`, and
`schematic.add_label`.

```powershell
.\.venv\Scripts\python.exe -m pcbflow proposal create <project-id> `
  --file commands.json --idempotency-key proposal-create --json
.\.venv\Scripts\python.exe -m pcbflow worker --once --json
.\.venv\Scripts\python.exe -m pcbflow proposal show <proposal-id> --json
.\.venv\Scripts\python.exe -m pcbflow proposal diff <proposal-id> --json
.\.venv\Scripts\python.exe -m pcbflow proposal accept <proposal-id> `
  --candidate-digest sha256:<review-digest> --actor-id reviewer `
  --comment "accepted" --idempotency-key proposal-accept --json
# Or reject the candidate:
.\.venv\Scripts\python.exe -m pcbflow proposal reject <proposal-id> `
  --reason "needs another review" --actor-id reviewer `
  --idempotency-key proposal-reject --json
```

### Read-only validation and storage

Queue validation with `python -m pcbflow validate <project-id> --idempotency-key <key> --json`, then run `worker --once`. Registered projects are copied through the link, path, file-count, and byte-count policy. Managed projects are materialized from SQLite `Project.current_revision`; the external import path is not a validation input. A successful run stores immutable raw reports in the content-addressed artifact store and normalized findings in SQLite.

The default data root is `.pcbflow-data/`. It contains `pcbflow.db`,
`artifacts/`, `projects/<project-id>/repo.git/`, and temporary `workspaces/`.
Set `PCBFLOW_DATA_DIR` to relocate the root, or set
`PCBFLOW_DATABASE_URL`/`PCBFLOW_ARTIFACT_DIR` explicitly. Other supported
settings are `PCBFLOW_TASK_LEASE_SECONDS`, `PCBFLOW_PROCESS_TIMEOUT_SECONDS`,
`PCBFLOW_MAX_PROCESS_OUTPUT_BYTES`, `PCBFLOW_MAX_PROJECT_FILES`,
`PCBFLOW_MAX_PROJECT_BYTES`, `PCBFLOW_MODULE_CATALOG_DIR`, and
`PCBFLOW_REMOTE_MODE`.

### Phase 2A limits

Only SQLite and KiCad 9.x are supported. The Worker is an explicit `--once`
runner, not a resident service. Phase 2A does not include AI generation,
arbitrary component or wire editing, PCB layout, manufacturing outputs such as
Gerber/BOM/CPL, supplier access, a Web UI, PostgreSQL, or a resident Worker.
