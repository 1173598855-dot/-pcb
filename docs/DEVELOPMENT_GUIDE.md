# PCBFlow 全面开发指导

日期：2026-07-29  
适用对象：后端、EDA 自动化、前端、AI、测试、发布和运维开发者

## 1. 文档定位

PCBFlow 是一个本地优先、以证据为中心的自动化 PCB 开发平台。它把需求、KiCad 工程、自动化命令、验证、审批和制造输出组织为可恢复、可审查的工程工作流。

本文回答四类问题：

1. 当前仓库已经能做什么。
2. 如何在本机建立开发环境并验证修改。
3. 新能力应该放在哪个模块、遵守哪些不变量。
4. 原理图、PCB、嘉立创、AI、Web UI 和常驻 Worker 应如何分阶段实现。

本文不是产品宣传或用户操作手册。当前用户操作以根目录 `README.md` 为准；详细设计以 `docs/superpowers/specs/` 为准；逐步实施任务以 `docs/superpowers/plans/` 为准。

## 2. 当前实现状态

| 能力 | 状态 | 说明 |
| --- | --- | --- |
| Python 包、CLI、REST API | 已实现 | Python 3.12/3.13、Typer、FastAPI |
| SQLite 与 Alembic | 已实现 | WAL、外键、busy timeout |
| Project 注册 | 已实现 | 注册本地目录，不修改源文件 |
| 持久任务与租约 | 已实现 | claim、lease、fencing、失败状态 |
| KiCad 9 CLI 探测 | 已实现 | 记录版本和可执行文件摘要 |
| 隔离 ERC/DRC | 已实现 | 在临时副本中只读执行 |
| Artifact/Evidence/Finding | 已实现 | SHA-256 内容寻址 |
| 重启后读取任务结果 | 已实现 | SQLite 持久化 |
| 需求冻结与 G1 | 已设计 | Phase 2A，尚未实现 |
| 原理图自动修改 | 已设计 | Phase 2A 首批受控操作 |
| PCB 自动布局布线 | 路线图 | Phase 3 |
| Gerber/BOM/CPL | 路线图 | Phase 4 |
| 嘉立创规则与交换 | 路线图 | Phase 4 |
| AI 辅助设计 | 路线图 | Phase 2C 以后 |
| Web UI 与完整审批 | 路线图 | Phase 5 |
| 常驻 Worker 与取消 | 路线图 | Phase 5A |

开发者必须在文档、CLI 帮助和 API 中区分“当前已实现”和“计划能力”。不能发布一个尚未存在的命令示例。

## 3. 关键设计资料

- 总体设计：
  `docs/superpowers/specs/2026-07-29-automated-pcb-development-platform-design.md`
- Phase 0/1 实施计划：
  `docs/superpowers/plans/2026-07-29-phase-0-1-read-only-validation.md`
- Phase 2A 受控设计变更：
  `docs/superpowers/specs/2026-07-29-phase-2a-controlled-design-change-kernel-design.md`

修改领域边界、数据真源、质量门或写入协议前，先更新对应规范。实现与规范临时不一致时，必须记录有期限的偏差，不能让差异只存在于代码中。

## 4. 项目原则

### 4.1 单一设计真源

KiCad 是首版设计真源。嘉立创 EDA 专业版是发布副本和受控交换目标，不是并行权威编辑源。

### 4.2 所有写入都经过命令

API、CLI、Web UI 和 AI 只能提交结构化领域命令。不得存在仅供调试的任意脚本入口，也不得让前端直接改数据库或 KiCad 文件。

### 4.3 证据先于结论

ERC、DRC、语义 Diff、计算、仿真和制造检查都应保存原始输出、工具版本、输入摘要和规范化结果。用户界面的“通过”只是证据的投影。

### 4.4 候选先行

自动修改必须发生在隔离候选中。失败、拒绝或过期候选永远不能修改权威 revision。

### 4.5 能力检测

外部工具适配基于启动时探测和契约测试，不基于版本号猜测。检测到 KiCad 10 不代表 KiCad 9 的适配器自动兼容。

### 4.6 确定性优先

稳定摘要、幂等键、UUID、命令顺序、差异排序、规则版本和导出清单必须可复算。随机性必须显式提供 seed 并进入证据。

### 4.7 AI 只产生提案

AI 不直接成为数据库事实，不直接改文件，不降低规则严重度，也不能声明自己运行过工具。所有 AI 输出先经过严格 Schema、引用校验、确定性规则和审批。

## 5. 系统架构

```text
                    +----------------------+
                    | CLI / REST / Web UI  |
                    +----------+-----------+
                               |
                               v
                    +----------------------+
                    | Application Services |
                    +----------+-----------+
                               |
             +-----------------+-----------------+
             |                 |                 |
             v                 v                 v
       Domain Models     Task Orchestrator   Approval/Gates
             |                 |                 |
             +-----------------+-----------------+
                               |
                               v
                    +----------------------+
                    | Ports and Adapters   |
                    +---+------+------+----+
                        |      |      |
                        v      v      v
                      Git   KiCad   AI/Supplier
                               |
                               v
                  SQLite + Artifact Store
```

领域规则不能依赖 HTTP、CLI 输出格式或具体数据库 Session。外部系统通过端口隔离，测试使用 fake 或记录好的响应。

### 5.1 数据真源与恢复

| 存储 | 保存内容 | 是否权威 |
| --- | --- | --- |
| Managed Git | 已接受的需求、KiCad 文件、约束和发布索引 | 设计文件历史权威 |
| SQLite | 工作流状态、`current_revision`、任务、审批和候选状态 | 状态推进权威 |
| Artifact Store | 原始报告、Diff、日志、快照和制造文件 | 不可变大对象权威 |
| Workspaces | 验证副本和候选 worktree | 临时，不是权威 |
| External source path | 初始导入来源 | 采用后只作 provenance |

数据库和 Git ref 中断不一致时，以数据库 `current_revision` 为准，由协调器把 Git ref 修复到数据库记录的、摘要已验证的 commit。Artifact 必须按摘要重新验证，不能因为数据库存在引用就假定文件完整。

## 6. 当前仓库结构

```text
.
├─ alembic.ini
├─ alembic/
│  ├─ env.py
│  └─ versions/
├─ docs/
│  ├─ DEVELOPMENT_GUIDE.md
│  └─ superpowers/
├─ src/pcbflow/
│  ├─ api.py
│  ├─ artifacts.py
│  ├─ cli.py
│  ├─ config.py
│  ├─ container.py
│  ├─ db.py
│  ├─ domain.py
│  ├─ kicad.py
│  ├─ process.py
│  ├─ repositories.py
│  ├─ tables.py
│  ├─ tasks.py
│  └─ validation.py
├─ tests/
│  ├─ contract/
│  ├─ e2e/
│  ├─ fixtures/
│  ├─ integration/
│  └─ unit/
├─ pyproject.toml
└─ README.md
```

### 6.1 当前模块职责

| 模块 | 职责 |
| --- | --- |
| `config.py` | 环境变量和路径配置 |
| `db.py` | Engine、Session 和 SQLite 连接设置 |
| `tables.py` | 当前 SQLAlchemy 表 |
| `domain.py` | 不依赖框架的领域值对象 |
| `repositories.py` | 当前 Project、Task、Evidence、Finding 仓储 |
| `artifacts.py` | 内容寻址写入、读取和校验 |
| `process.py` | 外部进程限制、超时和输出捕获 |
| `kicad.py` | KiCad CLI 探测、命令构造和报告归一化 |
| `validation.py` | 隔离复制与 ERC/DRC 任务处理 |
| `tasks.py` | 通用任务领取和 handler 调度 |
| `container.py` | 依赖组装和迁移执行 |
| `api.py` | FastAPI 请求、响应和错误映射 |
| `cli.py` | Typer 命令和 JSON/文本输出 |

`repositories.py` 已经较大。后续领域仓储应放入独立文件，避免继续扩张单个模块。

## 7. 建立开发环境

### 7.1 前置条件

- Windows PowerShell。
- Python 3.12 或 3.13。
- Git。
- 可选：KiCad 9.x。
- 可选：Node.js，开始 Web UI 后才需要。

### 7.2 创建虚拟环境

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

不要把 `.venv`、`.pcbflow-data`、临时 worktree、KiCad 安装包或制造输出提交到 Git。

### 7.3 运行迁移

```powershell
.\.venv\Scripts\python.exe -m alembic -c alembic.ini upgrade head
```

应用启动时也会运行前滚迁移，但开发数据库变更必须显式验证迁移命令。

### 7.4 运行测试

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pytest --cov=pcbflow --cov-report=term-missing --cov-fail-under=80
.\.venv\Scripts\python.exe -m pytest -m kicad -v
git diff --check
```

真实 KiCad 契约测试在未安装工具时允许明确跳过。普通单元测试不得静默依赖本机 KiCad。

## 8. 当前可运行工作流

```powershell
$project = .\.venv\Scripts\pcbflow.exe project add "C:\work\controller" `
  --name "Controller" `
  --idempotency-key "controller-project-v1" `
  --json | ConvertFrom-Json

$task = .\.venv\Scripts\pcbflow.exe validate $project.id `
  --idempotency-key "controller-validation-r1" `
  --json | ConvertFrom-Json

.\.venv\Scripts\pcbflow.exe worker --once --json
.\.venv\Scripts\pcbflow.exe task show $task.id --json
.\.venv\Scripts\pcbflow.exe evidence $project.id --json
.\.venv\Scripts\pcbflow.exe findings $project.id --json
```

当前 `worker --once` 每次最多执行一个任务。循环执行或作为服务运行尚未实现。

## 9. 配置与运行数据

主要环境变量：

| 变量 | 作用 |
| --- | --- |
| `PCBFLOW_DATA_DIR` | 数据库、Artifact 和 workspace 根目录 |
| `PCBFLOW_DATABASE_URL` | SQLAlchemy 数据库 URL |
| `PCBFLOW_ARTIFACT_DIR` | Artifact 根目录 |
| `PCBFLOW_KICAD_CLI` | `kicad-cli` 绝对路径 |
| `PCBFLOW_TASK_LEASE_SECONDS` | 任务租约时长 |
| `PCBFLOW_PROCESS_TIMEOUT_SECONDS` | 外部进程超时 |
| `PCBFLOW_MAX_PROCESS_OUTPUT_BYTES` | stdout 和 stderr 各自的字节上限 |
| `PCBFLOW_MAX_PROJECT_FILES` | 项目复制文件数上限 |
| `PCBFLOW_MAX_PROJECT_BYTES` | 项目复制总字节上限 |
| `PCBFLOW_REMOTE_MODE` | 禁止通过远程 API 注册本机路径 |

Phase 2A 计划新增 `PCBFLOW_MODULE_CATALOG_DIR`，用于只读加载本地已审核模块目录；在实现落地前该变量不存在。

默认数据布局：

```text
.pcbflow-data/
├─ pcbflow.db
├─ artifacts/
│  └─ objects/sha256/aa/bb/<digest>
└─ workspaces/
```

测试必须使用 `tmp_path` 和隔离 Settings，不得写入开发者的默认数据目录。

## 10. 日常开发流程

1. 从干净基线创建功能分支或隔离 worktree。
2. 阅读对应设计规范和现有测试。
3. 为一个最小行为写失败测试。
4. 运行该测试，确认失败原因正确。
5. 写最小实现。
6. 运行相关测试和全量测试。
7. 检查迁移、CLI 帮助、OpenAPI 和文档。
8. 使用 `git diff --check` 检查空白问题。
9. 提交一个可独立审查的变更。

不要把多个独立子系统塞进一个提交。数据库迁移、领域模型、API 和文档可以在同一纵向任务中提交，但必须共同形成可运行结果。

## 11. 领域模型开发

领域对象应满足：

- 不依赖 FastAPI、Typer、SQLAlchemy Session 或全局配置。
- 使用明确枚举和值对象，不使用散落字符串表达状态。
- 时间使用带 UTC 时区的 `datetime`。
- 外部输入在边界转换为严格类型。
- 状态转换集中在服务或聚合中，不能由路由直接改字段。
- 不可变事实优先使用 frozen dataclass 或严格 Pydantic 模型。

新增状态时必须同时更新：

1. 领域枚举。
2. 仓储映射。
3. 数据库约束或迁移。
4. API Schema。
5. CLI 展示。
6. 状态转换测试。
7. 恢复和重试逻辑。

## 12. 数据库与迁移

### 12.1 规则

- 已提交的 Alembic 历史迁移不得重写。
- 一个迁移只服务一个明确领域切片。
- SQLite 和 SQLAlchemy 模型必须一致。
- 新增非空列时提供安全默认值或分阶段回填。
- 数据回填必须幂等，避免应用重启时重复破坏数据。
- 大型制品不进入数据库 JSON；数据库只保存摘要和索引。
- 时间戳使用 UTC。
- 幂等键、revision 和对象身份必须有数据库唯一约束。

### 12.2 验证迁移

至少测试：

- 空数据库升级到 head。
- 从上一迁移升级到 head。
- 关键表、索引、外键和唯一约束存在。
- 应用容器可以在升级后启动。
- 重复启动不会重复写入种子数据。

如果 SQLite 无法安全执行某种列变更，使用批量迁移或新表复制，不要依赖只在 PostgreSQL 上成立的 DDL。

## 13. 仓储设计

仓储负责持久化合同，不负责业务流程。推荐模式：

```python
class RequirementStore:
    def create_draft(...) -> RequirementSet: ...
    def get(requirement_set_id: str) -> RequirementSet: ...
    def submit(...) -> RequirementSet: ...
    def approve_and_freeze(...) -> RequirementSet: ...
```

仓储方法必须：

- 在自己拥有的事务边界内保持原子性，或明确接受上层事务对象。
- 把数据库唯一冲突映射为稳定领域错误。
- 对幂等键比较完整输入，而不是只返回已有记录。
- 使用乐观版本或条件更新保护并发状态转换。
- 返回领域对象，不向 API 泄漏 ORM Row。

当一个跨仓储操作需要单一事务时，使用明确的 Unit of Work 或专用事务服务，不能靠多个仓储连续调用假装原子。

## 14. 任务与 Worker

### 14.1 当前合同

当前 Worker：

1. 从数据库领取 claimable Task。
2. 创建新的 lease token 和 TaskAttempt。
3. 把状态从 `leased` 转为 `running`。
4. 调用按 task kind 注册的 handler。
5. 把结果标记为 succeeded、retry_wait 或 failed_terminal。

Handler 通过：

- `RetryableTaskError` 表示可重试外部故障。
- `TerminalTaskError` 表示输入、能力或工程错误。

未捕获异常会记录日志并转换为 `UNHANDLED_TASK_ERROR`。

### 14.2 新增任务类型

新增任务时：

1. 定义稳定 task kind，例如 `design.execute_proposal`。
2. 定义 JSON 可序列化且不可变的 payload。
3. 提供 enqueue service，先验证 project 和权限。
4. 编写纯 handler，外部依赖通过构造器注入。
5. 在 Container 注册 handler。
6. 测试正常、可重试、终止失败、未知 kind 和过期租约。
7. 确保重试不会覆盖旧 TaskAttempt 和失败证据。

Handler 不应自行循环领取任务，也不应从全局环境重新构造容器。

### 14.3 Phase 5A 常驻 Worker

常驻 Worker 应增加：

- `worker run` 主循环。
- 空队列有界退避和抖动。
- 周期心跳与续租。
- 取消请求。
- Windows Job Object 和 Linux 进程组终止。
- 优雅关闭。
- Worker 身份、启动时间和最后心跳。
- 并发槽位和 task kind 限流。
- 健康检查与指标。

常驻 Worker 不能仅用 `while True: run_once()` 拼接完成。必须处理租约续期、进程树、数据库断连、关机信号和背压。

## 15. 外部进程

所有外部工具调用必须通过 ProcessRunner 或等价端口：

- 参数数组，不拼 Shell 字符串。
- 显式工作目录。
- 最小环境变量白名单。
- 超时。
- stdout/stderr 字节上限。
- 原始字节制品与 UTF-8 归一化视图。
- 返回码和信号记录。
- 可取消进程树。

禁止：

- `shell=True`。
- 把用户字符串直接拼到命令行。
- 依赖当前工作目录。
- 让外部工具写入注册源目录。
- 在错误消息中暴露 token 或用户主目录。

## 16. KiCad 适配开发

### 16.1 CLI 能力

`kicad-cli` 适合：

- ERC。
- DRC。
- Gerber。
- 钻孔。
- BOM/网表和位置文件。
- 渲染及其他官方批处理能力。

每个命令构造应有单元测试，验证参数顺序、输出路径和工作目录。报告解析使用真实 fixture，不依赖本机语言环境。

### 16.2 IPC

IPC 只能用于启动探测和契约测试已经证明稳定的操作。适配器能力报告必须记录：

- KiCad 版本。
- 可执行文件摘要。
- IPC 合同版本。
- 每个操作是否可用。
- 探测失败原因。

### 16.3 S-expression 文件适配

原理图和 PCB 文件不得用正则修改。受控适配器需要：

- tokenizer 和具体语法树。
- 未知节点保留。
- 无变更字节回环。
- 修改子树规范化输出。
- 深度、节点数、文件大小和字符串长度限制。
- KiCad 主版本兼容矩阵。
- 真实 KiCad 重开和验证。

格式解析成功不代表工程语义有效。修改后仍必须执行 ERC/DRC 和领域规则。

### 16.4 金样策略

每个受支持 KiCad 版本至少测试：

- 空白项目。
- 单页原理图。
- 层次原理图。
- 2 层和 4 层 PCB。
- 多单元器件。
- Unicode。
- Windows 空格路径。
- 底层元件和旋转。
- 铜区、规则区、差分对和槽孔。
- 有意包含 ERC/DRC 错误的工程。

升级 KiCad 版本时先运行无变更回环和语义金样，再允许写入能力。

## 17. Phase 2A：受控设计变更

开发前完整阅读 Phase 2A 规范。核心链路：

```text
project adopt
  -> RequirementSet draft
  -> G1 approval
  -> DesignCommandBatch
  -> proposal task
  -> isolated Git worktree
  -> semantic diff + ERC
  -> accept/reject
```

### 17.1 项目采用

- 创建平台管理的裸 Git 仓库。
- 导入外部项目形成首个 commit。
- 外部目录保持字节不变。
- 数据库保存当前 revision 和快照摘要。
- 后续验证从 managed revision 物化隔离副本。

### 17.2 需求冻结

- 输入使用严格 Pydantic Schema。
- 规范化 JSON 计算摘要。
- 提交时在隔离 worktree 中生成 `requirements/` 文件和候选 Git commit。
- G1 决策签署需求摘要、候选快照和 base revision。
- 批准后推进 current revision，并把该 RequirementSet 设为 active。
- `pending_approval` 和 `frozen` 内容不可修改。
- 修改需求创建新 RequirementSet。

### 17.3 DesignCommand

首批命令：

- `schematic.instantiate_module`
- `schematic.set_property`
- `schematic.assign_footprint`
- `schematic.add_label`

命令必须绑定 project、base revision、frozen requirement set、actor、intent、risk 和 provenance。

### 17.4 候选

- 每批命令最多产生一个候选。
- 候选 commit 使用 proposal ref 保持可达。
- `ready_for_review` 前必须通过强制验证。
- 接受时同时比较 base revision 和用户审阅过的 candidate digest。
- 基线变化后标记 stale，不自动重放。

## 18. 原理图自动化开发

完整原理图自动化应按以下顺序扩展：

1. 层次模块实例化。
2. 受控器件和封装修订。
3. 端口和命名网络绑定。
4. 属性、注释和封装分配。
5. 关键电气规则。
6. 需求追踪和语义 Diff。
7. ERC 修复提案。
8. 新模块审核和验证。

### 18.1 模块不是文本模板

受控模块包含：

- 参数 Schema。
- 明确输入、输出和电源端口。
- KiCad 原理图片段。
- 器件与封装修订引用。
- 计算和仿真证据。
- 支持的替代集合。
- 版本、摘要和已知限制。

AI 建议的新电路不能直接成为 verified 模块。

### 18.2 原理图专项规则

规则至少覆盖：

- IC 去耦。
- MCU 复位、BOOT、时钟、调试和未用引脚。
- 稳压器反馈、补偿和电容。
- MOSFET 默认态和续流。
- CAN/RS-485 端接、偏置和保护。
- 运放共模、摆幅、带宽和稳定性。
- 隔离域和爬电距离。
- 关键节点测试点。

每个 Finding 必须定位到具体对象和证据条款。

## 19. PCB 自动化开发

PCB 自动化不应从全板自动布线开始。推荐分层：

### 19.1 Phase 3A：初始化

- 板框。
- 安装孔。
- 层叠。
- 网络类。
- 规则区。
- 禁布区。
- 铜区。
- 连接器和机械固定对象。

### 19.2 Phase 3B：布局

按约束优先：

1. 固定机械对象。
2. 电源入口和保护。
3. MCU、时钟、复位和去耦。
4. 通信、模拟和驱动模块。
5. courtyard、高度和返修空间。

候选布局使用确定性指标评分：

- 飞线总长。
- 关键网络估计长度。
- 回路面积。
- 跨分割风险。
- 热集中。
- 边界和 courtyard 违例。
- 连接器可达性。
- 可装配性。

AI 可以解释评分和选择策略，但不能直接输出未经验证的任意坐标序列。

### 19.3 Phase 3C：受限布线

- 只处理明确网络集合。
- 先关键网络，再普通数字信号。
- 每个网络类定义线宽、间距、过孔、允许层和拓扑。
- 电源线宽基于电流、铜厚和温升计算。
- 换层必须检查回流路径。
- 每轮执行后立即 DRC 和语义评分。
- 工程师可在 KiCad 中完成复杂全板布线，再受控重新导入。

## 20. 制造输出与嘉立创

### 20.1 通用制造输出

Phase 4 应生成：

- Gerber archive。
- Excellon 钻孔。
- IPC-356，可用时。
- BOM。
- CPL/位置文件。
- 装配图。
- 钢网和 DNP 说明。
- 3D/机械审查证据。
- 发布清单。

导出必须来自冻结 revision、锁定工具链和锁定规则包。不能从用户刚刚手工打开但未提交的 KiCad 工作树导出。

### 20.2 BOM

内部规范字段：

```text
refs
quantity
value
description
manufacturer
mpn
lcsc_part_number
package
footprint
dnp
assembly_side
supplier_snapshot_at
lifecycle
substitution_group
```

厂商 CSV 由映射器生成，不把厂商字段名扩散到核心领域模型。

### 20.3 CPL

CPL 转换必须用金样验证：

- 中心坐标。
- 旋转。
- 顶层和底层。
- 底层镜像。
- 封装方向基准。

不能只依据经验公式。每个已知元件金样都应明确期望输出。

### 20.4 嘉立创规则包

规则包包含：

- 来源 URL 或导入文件。
- 抓取时间、生效时间和过期时间。
- 层数、板厚、铜厚、表面处理。
- 最小线宽、间距、孔径和环宽。
- 板边、阻焊、槽孔和特殊工艺。
- 下单字段映射。
- 状态：example、draft 或 verified。

项目锁定规则包摘要，不能引用“当前最新”。升级规则包必须生成差异并重新运行 G3/G4。

### 20.5 嘉立创 EDA 专业版交换

- 优先使用公开支持的 KiCad 导入/导出能力。
- 不直接修改未公开稳定格式的专有归档。
- KiCad 到嘉立创 EDA 是发布副本。
- 回传先进入隔离区。
- 生成组件、网络、封装、板框和布局语义 Diff。
- 有损字段进入人工处理清单。
- 合并后重跑 ERC、DRC、DFM 和审批。

## 21. AI 辅助开发

### 21.1 模型网关

所有模型调用经过统一网关，负责：

- 供应商无关请求接口。
- 严格 JSON Schema 输出。
- 超时、取消、重试和速率限制。
- 成本预算。
- 提示词、模型、参数和输入摘要审计。
- 内容哈希缓存。
- 敏感字段过滤。
- 离线模式。

业务服务不得直接依赖某家模型 SDK。

### 21.2 AI 角色

建议逻辑角色：

- 需求编译器。
- 系统方案代理。
- 器件候选代理。
- 原理图规划器。
- PCB 规划器。
- 修复规划器。
- 独立审查器。
- 报告解释器。

逻辑角色不要求不同模型或并发进程。首版可共享同一网关和模型，通过版本化提示词区分。

### 21.3 输出约束

AI 输出必须：

- 只使用提供的需求、规则、器件和证据。
- 引用 requirement、evidence、rule 或明确 assumption。
- 缺少关键电压、额定值、封装或制造能力时返回 blocking question。
- 不声称已修改文件或运行工具。
- 不直接输出任意 KiCad 文件文本。
- 不直接输出可执行脚本。
- 只产生允许的 DesignCommand 提案。

### 21.4 AI 测试

- 单元测试使用记录好的结构化响应。
- Schema fuzz 覆盖未知字段、非法单位和超大数组。
- 提示注入样例进入需求、数据手册摘录和工具日志。
- 离线评测固定输入和规则快照。
- 模型升级和提示词升级分别审批。
- 指标包括引用准确率、阻断问题召回率和非法命令率。

## 22. 审批、API 与 Web UI

### 22.1 四个质量门

| Gate | 对象 |
| --- | --- |
| G1 | 冻结需求和系统约束 |
| G2 | 原理图候选、ERC、计算和风险 |
| G3 | PCB、DRC、机械和布局证据 |
| G4 | 制造包、BOM/CPL、规则包和发布清单 |

审批签署具体摘要。候选内容、规则包、工具链或证据集合变化后，旧审批失效。

Phase 2A 的单次候选接受记录为 `DESIGN_CHANGE` 决定，不宣称已经实现完整 G2。完整 G2 需要覆盖整张原理图检查点、计算和审查证据。

### 22.2 REST API

API 设计原则：

- 版本前缀 `/api/v1`。
- 创建操作需要幂等键。
- 领域冲突使用 409。
- 严格拒绝未知字段。
- 内部堆栈不返回客户端。
- 分页、过滤和稳定排序。
- 长任务返回 Task 或 Proposal，而不是保持 HTTP 连接。
- OpenAPI 与 CLI 使用同一领域模型。

### 22.3 CLI

CLI 原则：

- 自动化场景提供 `--json`。
- 人类输出简洁，错误给出下一步动作。
- 退出码稳定。
- 命令名与 REST 领域词汇一致。
- 不能让 CLI 绕过 API 所需的领域验证。

### 22.4 Web UI

推荐 React + TypeScript + Vite：

- TanStack Query 管理服务端状态。
- OpenAPI 生成类型。
- React Hook Form 处理严格 Schema 表单。
- REST 获取权威快照，事件流做增量更新。
- 断线或序号缺口后重新拉取。

首批页面：

1. 项目列表。
2. 项目总览。
3. 需求编辑与 G1。
4. 设计候选和语义 Diff。
5. Findings。
6. 任务和日志。
7. G2/G3/G4 审批。
8. 制造发布。
9. 工具链和模型设置。

原理图和 PCB 首版使用服务器渲染图、对象高亮和语义侧栏，不重写完整 EDA 画布。

## 23. 事件与实时更新

领域事件通过事务 outbox 产生，至少包括：

- `project.adopted`
- `project.revision.accepted`
- `requirements.submitted`
- `requirements.frozen`
- `proposal.created`
- `proposal.ready_for_review`
- `proposal.accepted`
- `proposal.rejected`
- `task.started`
- `task.finished`
- `gate.decided`
- `release.created`

事件具有单调序号、aggregate、时间、actor 和 correlation ID。WebSocket 或 SSE 丢失事件时，客户端用最后序号通过 REST 补拉。

事件处理必须幂等。不能因为 WebSocket 推送失败而回滚已经完成的领域事务。

## 24. 可观测性

结构化日志统一字段：

- `trace_id`
- `correlation_id`
- `project_id`
- `workflow_run_id`
- `task_id`
- `command_batch_id`
- `proposal_id`
- `release_id`

指标：

- 队列等待时间。
- 任务执行时间。
- 重试和终止失败率。
- ERC/DRC Finding 数量。
- 审批等待时间。
- revision 冲突率。
- 模型调用成本和缓存命中。
- Artifact 容量。
- 发布复现率。

外部工具完整输出存为 Artifact；应用日志只保存摘要和引用。

`doctor` 应逐步扩展为可分享诊断包，默认删除用户名、绝对路径、密钥和工程内容。

## 25. 安全与权限

### 25.1 本地安全

- 默认监听 `127.0.0.1`。
- 远程模式必须显式开启认证和 TLS 终止。
- 密钥来自 OS 凭据库或环境注入。
- 工具和规则包记录摘要。
- 上传归档限制数量、大小、压缩比和展开路径。
- 不信任文件名、MIME 和扩展名。
- AI 上下文采用字段白名单。

### 25.2 RBAC

目标角色：

- `viewer`
- `designer`
- `reviewer`
- `release_manager`
- `admin`
- `service_agent`

权限应拆分到需求冻结、命令执行、豁免、G2/G3、G4 和系统设置。不能只提供一个全能管理员开关。

### 25.3 审计

审计事件追加写，包含：

- actor。
- 动作。
- 对象。
- 前后摘要。
- 结果。
- 客户端。
- correlation ID。

敏感值只记录存在性或摘要。审计导出可验证哈希链，但除非使用真实签名基础设施，否则不要宣称等同于硬件签名。

## 26. 测试体系

### 26.1 测试层级

| 层级 | 目标 |
| --- | --- |
| 单元 | 状态机、值对象、规则、Schema、坐标转换 |
| 属性 | 摘要、单位、路径、几何和幂等不变量 |
| 契约 | 仓储、KiCad、模型、供应商和交换端口 |
| 金样 | KiCad 回环、语义 Diff、坐标和制造文件 |
| 集成 | 数据库、Artifact、任务、Git worktree |
| E2E | 从输入到候选或发布的参考板 |
| 故障注入 | 崩溃、锁、超时、磁盘和并发 |

### 26.2 测试命名

- 单元：`tests/unit/test_<module>.py`
- 集成：`tests/integration/test_<workflow>.py`
- 契约：`tests/contract/test_<adapter>.py`
- E2E：`tests/e2e/test_<journey>.py`
- fixture：`tests/fixtures/<domain>/`

测试名称应描述行为和条件，例如：

```text
test_accept_rejects_candidate_when_base_revision_changed
test_expired_lease_cannot_publish_proposal
test_bottom_side_cpl_rotation_matches_golden_fixture
```

### 26.3 测试隔离

- 每个测试使用独立临时目录和数据库。
- 不共享可变全局 Container。
- 不依赖测试执行顺序。
- 普通测试不访问网络。
- 真实 KiCad 测试使用 marker。
- 时钟、UUID 或随机数在需要时注入。
- 故障测试通过端口注入失败，不修改开发者环境。

### 26.4 覆盖率

当前最低命令阈值为 80%，Phase 2A 完成定义要求全量覆盖率不低于 90%。覆盖率不是完成替代品；关键不变量必须有直接断言和故障路径测试。

## 27. 发布与制品

发布候选必须绑定：

- source revision。
- project snapshot digest。
- toolchain lock。
- rulepack digests。
- artifact 列表。
- evidence set。
- gate evaluations。
- G4 决策。

任何文件变化都会改变候选摘要并使旧 G4 失效。

发布目录里的友好文件名只是清单映射。内容真源仍是 SHA-256 Artifact。离线验证 CLI 应能：

1. 读取清单。
2. 校验 Schema。
3. 校验每个文件大小和摘要。
4. 校验候选摘要。
5. 显示工具链和规则包。
6. 检查审批对象匹配。

## 28. 文档维护

每个功能至少检查：

- 设计规范是否需要更新。
- 实施计划是否反映实际文件。
- README 是否只描述可运行能力。
- CLI `--help` 是否准确。
- OpenAPI Schema 是否同步。
- 错误码表是否新增。
- 环境变量是否记录。
- 迁移和恢复说明是否完整。

重要设计决定使用 ADR，包括：

- 数据真源变化。
- 新 EDA 适配方式。
- 坐标或旋转约定。
- 制造规则来源。
- AI 模型和缓存策略。
- 审批对象或摘要算法变化。

## 29. 常见故障排查

### 29.1 `KICAD_CLI_UNAVAILABLE`

检查：

```powershell
.\.venv\Scripts\pcbflow.exe doctor --json
$env:PCBFLOW_KICAD_CLI
```

确认路径指向受支持 KiCad 9.x `kicad-cli.exe`。

### 29.2 `INVALID_KICAD_PROJECT`

当前只读验证要求工程根目录中每种设计文件最多一个。检查是否存在多个 `.kicad_sch` 或 `.kicad_pcb` 根文件。

### 29.3 任务长期处于 leased 或 running

等待租约过期后运行新的 `worker --once`。旧 Worker 即使恢复，也不能使用过期 token 提交。

### 29.4 SQLite locked

- 确认没有手工工具长时间持有写事务。
- 保持事务短小。
- 不在事务中运行 KiCad 或 Git。
- 检查数据目录磁盘和权限。

### 29.5 Artifact 校验失败

不要覆盖对象文件。保留数据库和损坏对象用于诊断，重新执行产生制品的任务，并检查磁盘、同步软件或人工修改。

### 29.6 候选 revision 冲突

不要自动 rebase 已审阅候选。基于新的 current revision 重新生成命令批次和候选。

### 29.7 Git ref 与数据库不一致

Phase 2A 的启动协调器以数据库 current revision 为准。若候选对象缺失或摘要不匹配，应阻断写入并保留诊断，不能猜测选择某个 ref。

## 30. 分阶段开发路线

### Phase 0/1：工程基础和只读验证

状态：最小纵向切片已实现。总体设计中列出的 BOM/网表提取、门禁投影和 Web 状态页尚未全部实现。

### Phase 2A：受控设计变更内核

状态：设计完成，下一步编写实施计划。

### Phase 2B：器件与模块库

- ComponentRevision。
- 数据手册和引脚证据。
- Symbol、Footprint、3D 摘要。
- 模块导入、验证和版本。
- 替代料差异规则。

### Phase 2C：AI 原理图辅助

- 需求编译。
- 器件候选。
- 原理图命令提案。
- 修复规划。
- 独立审查。

### Phase 3：PCB 初始化和协同自动化

- 板框和层叠。
- 网络类和规则区。
- 约束布局。
- 受限布线。
- 外部编辑重新导入。

### Phase 4：制造和嘉立创

- 制造规则包。
- Gerber、钻孔、BOM、CPL。
- DFM 和下单预检。
- EDA 专业版交换。
- G4 发布清单。

### Phase 5：可用性和可靠性

- 常驻 Worker。
- 取消、心跳和并发。
- 完整 Web UI。
- RBAC 和多人审批。
- PostgreSQL 团队模式。
- 备份、恢复、性能和生产部署。

## 31. 功能完成检查表

一个功能只有在以下项目全部满足后才算完成：

- [ ] 书面行为和错误合同。
- [ ] 领域逻辑不依赖 UI 或具体适配器。
- [ ] 正常、边界和失败路径测试。
- [ ] 幂等和并发行为明确。
- [ ] 外部工具版本和输入进入证据。
- [ ] 数据迁移可重复升级。
- [ ] 生成文件有 Schema、摘要和来源。
- [ ] 日志、指标和审计字段完整。
- [ ] CLI、OpenAPI 和文档一致。
- [ ] Windows 本地路径通过。
- [ ] 不含未登记的绕过入口或静默异常。
- [ ] `git diff --check` 通过。

## 32. 提交前快速检查

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pytest --cov=pcbflow --cov-report=term-missing
.\.venv\Scripts\pcbflow.exe doctor --json
git diff --check
git status --short
```

涉及真实 KiCad 合同时再运行：

```powershell
.\.venv\Scripts\python.exe -m pytest -m kicad -v
```

涉及数据库迁移时，从空数据库和上一版数据库各执行一次升级验证。涉及生成文件时，检查 Artifact 摘要、语义 Diff 和源目录未被修改。

## 33. 开发者接手路径

第一次接手项目建议按以下顺序阅读和操作：

1. 阅读根目录 `README.md`。
2. 安装开发依赖。
3. 运行全量测试。
4. 执行 `pcbflow doctor --json`。
5. 阅读总体设计的架构、状态机和路线图。
6. 阅读当前功能的独立设计规范。
7. 找到对应的单元、集成和 E2E 测试。
8. 从一个失败测试开始修改。

如果无法明确说明“这个改动作用于哪个 revision、由什么证据验证、失败时如何恢复”，说明设计边界还不完整，不应直接开始写入型实现。
## 34. Phase 2A controlled design change kernel

This section is the implementation-facing companion to the Phase 2A design
specification and plan. It describes the behavior that the current source,
CLI, REST API, and tests provide.

### 34.1 Architecture and authority boundaries

SQLite is authoritative for workflow state: project mode, `current_revision`,
requirements, commands, leases, proposals, decisions, evidence rows, and
outbox events. Managed Git is authoritative for accepted design contents and
refs. The content-addressed Artifact Store is authoritative for immutable raw
reports, manifests, diffs, logs, and decision evidence. Git refs are repaired
projections of SQLite state by `RevisionReconciler`; a ref must not be selected
as validation input when the database revision is available. The registered
external `source_path` is import provenance and is never written.

### 34.2 Windows/Linux setup and environment

Use Python 3.12 or 3.13, Git, and optionally KiCad 9.x. Create a virtual
environment and install the editable package with `.[dev]`. `PCBFLOW_DATA_DIR`
defaults to `.pcbflow-data`; `PCBFLOW_DATABASE_URL` and
`PCBFLOW_ARTIFACT_DIR` override its database and artifact locations.
`PCBFLOW_KICAD_CLI` selects `kicad-cli`; `PCBFLOW_MODULE_CATALOG_DIR` selects
verified module files. Runtime limits are controlled by
`PCBFLOW_TASK_LEASE_SECONDS`, `PCBFLOW_PROCESS_TIMEOUT_SECONDS`,
`PCBFLOW_MAX_PROCESS_OUTPUT_BYTES`, `PCBFLOW_MAX_PROJECT_FILES`, and
`PCBFLOW_MAX_PROJECT_BYTES`. `PCBFLOW_REMOTE_MODE` is an explicit deployment
mode flag. Windows paths may be absolute; Linux uses the same variables with
POSIX paths.

### 34.3 Migration workflow and schema ownership

Alembic owns schema changes. Run `python -m alembic -c alembic.ini upgrade head`
against a disposable database before testing a migration, then run the full
suite against both a fresh and an upgraded database. SQLAlchemy table modules
describe the current schema; application services, not ad-hoc CLI code, own
transactions and invariants.

### 34.4 Adoption, reconciliation, and source immutability

`project add` registers a path. `project adopt` copies it through the shared
link/path/size policy into a bare managed Git repository, records the first
revision and snapshot digest, and sets the design ref. Startup and explicit
reconciliation verify the database revision object, snapshot digest, G1 facts,
accepted proposal facts, and candidate refs. If a projection is recoverable,
the reconciler repairs the ref from SQLite; missing or contradictory facts stop
writes with a stable reconciliation error. Validation of a managed project
materializes `current_revision`; registered validation uses a safe temporary
copy. Neither path mutates the external source.

### 34.5 RequirementSet, canonicalization, G1, and digest signing

Requirement YAML is parsed into a strict `RequirementSetPayload`. Canonical
JSON uses UTF-8, sorted keys, compact separators, and rejects non-finite
numbers. `canonical_digest` prefixes SHA-256 with `sha256:`. Submission renders
the candidate requirement files in an isolated worktree. G1 binds the complete
subject digest, base revision, candidate revision, actor, comment, and an
approval Artifact; an approved set is frozen and becomes the active set. A
changed digest or base is a conflict, not a silent replay.

### 34.6 DesignCommand schema, preconditions, idempotency, and operations

Design command batches are strict Pydantic models with `extra="forbid"`.
Every batch and command carries a schema version, project, base revision,
requirement set, actor, intent, risk, provenance, and idempotency key.
Preconditions are evaluated against the materialized semantic document and
capability report before any write. The supported operation examples are:

```json
{"type":"schematic.instantiate_module","payload":{"module_revision_id":"modrev_status_led_v1","instance_name":"STATUS_LED","target_sheet_ref":{}}}
{"type":"schematic.set_property","payload":{"subject_ref":{},"property_name":"Value","value":"GREEN","expected_old_value":"RED"}}
{"type":"schematic.assign_footprint","payload":{"subject_ref":{},"footprint":"Package_SO:SOIC-8_3.9x4.9mm_P1.27mm"}}
{"type":"schematic.add_label","payload":{"text":"STATUS_OK","target_sheet_ref":{},"position":{"x":10.0,"y":10.0}}}
```

The full payload schema, not the abbreviated examples, is validated before
queueing. Reusing a key with a different canonical input returns a conflict.

### 34.7 CST, semantic IR, Diff attribution, and prohibited edits

The schematic adapter parses KiCad S-expressions into a loss-preserving CST,
projects supported nodes into a semantic IR, applies typed edits, serializes
the CST, and computes an attributed semantic Diff. Stable object UUIDs,
hierarchy, units, Unicode, custom properties, and unknown inline nodes are
preserved by the CST contract. Regex replacement, line-oriented search/replace,
and unstructured text rewriting of `.kicad_sch` are prohibited.

### 34.8 Verified module and footprint catalog

Catalog entries are YAML metadata plus a KiCad schematic fragment. The catalog
loader enforces the configured file and byte limits and verifies each module's
declared SHA-256 digest before use. A command must reference a module revision
id and include it in provenance. Arbitrary library paths and unverified
footprints are rejected by the adapter contract.

### 34.9 Worker leases, fencing, commits, evidence, and recovery

The Worker claims one SQLite task, starts it with a fencing token, and passes
the current clock to every start, complete, fail, and active-lease check. An
expired token cannot transition or publish a result. Proposal execution uses a
detached Git worktree, mandatory Schema/precondition/path-limit/post-write
parse/semantic-Diff/ERC validations, deterministic commit metadata, and a
digest-bound evidence set. A stale worker may leave a candidate ref or
worktree, but the next attempt replays or repairs it from durable facts.

### 34.10 REST/CLI reference, states, errors, and examples

The CLI command groups are `project`, `requirements`, `approval`, and
`proposal`; top-level commands include `validate`, `worker --once`,
`findings`, `evidence`, `doctor`, and `serve`. The REST API exposes project
adoption, requirement submission, approvals, proposal create/show/diff,
accept/reject, task lookup, and `worker:run-once`. Write requests require the
`Idempotency-Key` header. Proposal states are `queued`, `executing`,
`validation_failed`, `ready_for_review`, `accepted`, `rejected`, and `stale`.
Stable error examples include `DESIGN_COMMAND_SCHEMA_INVALID`,
`DESIGN_COMMAND_PRECONDITION_FAILED`, `KICAD_CLI_UNAVAILABLE`,
`CANDIDATE_VALIDATION_FAILED`, `REVISION_RECONCILIATION_REQUIRED`, and
`STALE_LEASE`.

### 34.11 Test layers and fixture rules

Run unit, property, golden, integration, contract, and E2E tests with
`python -m pytest -q`. Run the KiCad contract marker with
`python -m pytest -m kicad -v`; the locator contract is optional and the single
real-KiCad write/ERC test skips only when KiCad 9 is absent. Golden fixtures are
parsed and byte-round-tripped, copied into paths with spaces, and checked for
stable UUID identity, hierarchy, Unicode, units, custom properties, and ERC
findings. Fault tests use constructor-injected `FaultInjector` instances and
never patch production globals.

### 34.12 Structured logs, audit outbox, metrics, and snapshot boundary

Structured log context is limited to project, requirement, command batch,
proposal, base/candidate revision, task, trace, error code, adapter contract,
and result. Audit outbox payloads contain schema version, trace id, actor,
action, object, before/after digests, result, and domain-specific stable ids.
The eight metric families are proposal execution duration, schematic parse
duration, KiCad ERC duration, proposal validation total, project revision
conflict total, proposal review wait, Git-ref reconciliation retry total, and
adapter contract execution total. Labels are limited to low-cardinality
`result`, `code`, and `contract`; paths, tokens, and project content never enter
logs or metrics. Metrics are process-local snapshots in Phase 2A and are not a
distributed monitoring backend.

### 34.13 Fault injection, troubleshooting, security, and Phase 2B+ non-goals

Inject faults by passing a `FaultInjector` to `build_container`; the five
points are before candidate commit, after proposal ref before the database
write, during diff Artifact save, after execution before the final fence, and
after acceptance database commit before design-ref promotion. A hook exception
is allowed to propagate unchanged. Restart the container, run reconciliation,
and retry the expired task to verify recovery and idempotency. For failures,
check `doctor --json`, task lease expiry, current revision, proposal evidence
digests, and Git ref reconciliation before changing files. Process execution is
shell-free with a controlled environment; source paths, links, reparse points,
tokens, and unbounded output are fenced. AI generation, arbitrary component or
wire editing, PCB layout, manufacturing output, supplier access, Web UI,
PostgreSQL, and resident Workers are outside Phase 2A and remain Phase 2B+ or
later non-goals.
