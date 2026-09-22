# 嘉立创专业版主工程 PCB 自动化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为低压两层 STM32 环境监测与负载控制板提供嘉立创 EDA 专业版主工程的、可审查且可恢复的 PCB 候选生成流程，并保持 KiCad 10 的并行语义回归。

**Architecture:** 先以 P0 capability gate 验证嘉立创专业版是否具有公开、稳定、可回读的自动化入口；未通过时，系统只能生成 BoardIR 算法候选、读取原生信息和保存证据，绝不写入专有归档。通过后，`PcbEdaAdapter` 在隔离副本中执行受控 BoardOperation，并以冻结的 BoardIR、规则包、工具能力摘要、原生 DRC 和审批记录驱动 G3/G4；KiCad 仅作为独立的适配与回归端，不进行双主写入。

> **2026-08-10 measured status:** Tasks 11–14 implementation and positive fixture criteria are
> complete. This covers BoardIR/algorithm, candidates, G3/G4 fixtures, API/CLI, KiCad parity, and
> release packaging. It does **not** verify a local official LCEDA write bridge, native DRC, or
> real release; the real pipeline remains `boardir_only` and capability-blocked. The detailed
> measured record is retained in the design specification; no human hardware/manufacturing review
> or official LCEDA bridge verification occurred.

**Tech Stack:** Python 3.12/3.13、SQLAlchemy、Alembic、Pydantic 2、FastAPI、Typer、pytest、Hypothesis、OR-Tools CP-SAT `>=9.14,<10`、现有内容寻址 Artifact Store、现有 Worker/lease/fencing/取消机制、KiCad 10 `kicad-cli`。

## Global Constraints

- 每个 PCB 项目只能冻结一个 `eda_kind`、`eda_profile_id`、`board_profile_id` 和 `rulepack_digest`；既有 KiCad 项目不改变默认行为。
- `lceda_pro` 只有在 P0 报告声明全部所需官方操作可用且保存后可回读时才允许调用写入后端；不得用字符串替换或猜测方式修改嘉立创专有工程归档。
- 所有候选操作在隔离 workspace 内执行；注册的 `source_path` 与已接受的权威工程不得被修改。
- V1 只接受双层、1 oz、约 `100 mm x 80 mm` 的低压控制板；继电器触点最多 `24 V DC / 3 A`，每路低边 MOS 最多 `12/24 V DC / 2 A`。
- 市电、高压隔离、四层及以上、RF 天线匹配、阻抗控制、高速差分对、盲埋孔和任意未受约束全板布线必须被拒绝并产生稳定 finding。
- BoardIR 的坐标、线宽、间距、孔径和面积使用整数微米；算法种子、目标函数版本、输入摘要、规则包和 capability 摘要必须写入证据。
- `PlacementLock`、`RouteLock`、ESP 天线禁区、机械 keepout、晶振近场和低噪声区是硬约束；失败必须产生 finding，不能降低规则以宣称成功。
- G3/G4 由人工签署；原生 DRC、PCBFlow 规则、未连接网络、制造资料清单与冻结摘要均必须匹配。
- 保留现有 SQLite、内容寻址 Artifact、幂等键、任务 fencing、取消和恢复语义；API 写操作仍要求 `Idempotency-Key`。
- 当前工作树已有未提交改动。实施中不得重置、回退、覆盖或提交既有改动；每个检查点以测试和 `git diff --check` 代替提交。

---

## File Structure

| 路径 | 职责 |
| --- | --- |
| `src/pcbflow/eda.py` | 项目级权威 EDA、能力摘要和安全的工具探测契约。 |
| `src/pcbflow/eda_authority_store.py` | 不可变项目 EDA 权威配置的持久化与幂等检查。 |
| `src/pcbflow/lceda_pro.py` | 嘉立创专业版 P0 探测、能力门和拒绝未验证写入。 |
| `src/pcbflow/board/ir.py` | 整数几何、板框、层叠、对象、快照和 opaque node。 |
| `src/pcbflow/board/rulepack.py` | 版本化制造规则包、网类和 V1 范围检查。 |
| `src/pcbflow/board/operations.py` | `PlaceFootprints`、`RouteNets`、`CreateCopperZones`、`AddGroundStitching` 与锁定操作。 |
| `src/pcbflow/board/adapter.py` | `PcbEdaAdapter`、隔离候选和原生 DRC/导出协议。 |
| `src/pcbflow/board/placement.py` | CP-SAT 可行性、确定性多起点模拟退火和布局证据。 |
| `src/pcbflow/board/routing.py` | 45 度 A*/Lee、Pathfinder 拥塞协商、rip-up/retry 与功率网拒绝路径。 |
| `src/pcbflow/board/copper.py` | GND 铜皮、热焊盘、地过孔策略和铜皮连通性。 |
| `src/pcbflow/board/validation.py` | BoardIR 几何、keepout、连通性、未连线和制造规则 finding。 |
| `src/pcbflow/board/kicad_adapter.py` | KiCad 10 只读 BoardIR 投影和语义/算法回归入口。 |
| `src/pcbflow/pcb_candidates.py` | PCB 候选、状态机、持久化、冻结摘要和 G3/G4 决策服务。 |
| `src/pcbflow/pcb_workflow.py` | P0 探测、候选生成、原生验证和发布任务 handler。 |
| `src/pcbflow/manufacturing.py` | Gerber、钻孔、BOM、CPL、装配资料与发布清单的一致性检查。 |
| `alembic/versions/0007_eda_authority.py` | 项目权威 EDA 表。 |
| `alembic/versions/0008_pcb_candidates.py` | PCB 候选、阶段证据与发布字段。 |
| `tests/fixtures/boardir/stm32-environment-controller-2l-v1.json` | STM32F103C8T6、Type-C、ESP-01S、OLED、ADC/I2C、继电器、MOS、端子和禁区的冻结 BoardIR fixture。 |
| `tests/fixtures/boardir/stm32-environment-controller-2l-rulepack.json` | 两层低压 V1 规则包，含 `203 um`、`508 um`、`2032 um` 参考线宽。 |
| `tests/fixtures/lceda-pro/minimal/README.md` | 最小原生 fixture 的冻结要求、预期能力和人工采集步骤；不包含猜测出的专有文件。 |

### Task 1: Persist Immutable Per-Project EDA Authority (P0)

**Files:**
- Create: `src/pcbflow/eda.py`
- Create: `src/pcbflow/eda_authority_store.py`
- Create: `alembic/versions/0007_eda_authority.py`
- Modify: `src/pcbflow/domain.py`
- Modify: `src/pcbflow/design_tables.py`
- Modify: `src/pcbflow/container.py`
- Modify: `src/pcbflow/api.py`
- Modify: `src/pcbflow/cli.py`
- Modify: `tests/integration/test_migrations.py`
- Create: `tests/integration/test_eda_authority.py`
- Modify: `tests/e2e/test_api_cli.py`

**Interfaces:**
- Produces `EdaKind.KICAD`, `EdaKind.LCEDA_PRO`, `ProjectEdaAuthorityInput`, immutable `ProjectEdaAuthority`, `EdaAuthorityConflictError`, and `ProjectEdaAuthorityStore.configure(...) -> ProjectEdaAuthority`.
- Produces `EdaCapability` and `EdaOperation`; later P0-P6 handlers consume only `capability_digest` rather than mutable process-local tool facts.
- Existing projects have no authority row and retain their established KiCad behavior. New `lceda_pro` projects freeze a complete authority tuple in the same create request; a legacy project can receive exactly one authority tuple before its first PCB candidate. Every PCB candidate requires one immutable authority row.

- [ ] **Step 1: Write authority replay and public-contract tests**

```python
def test_authority_is_immutable_and_replays_identical_input(container, registered_project) -> None:
    authority = ProjectEdaAuthorityInput(
        eda_kind=EdaKind.LCEDA_PRO,
        eda_profile_id="lceda-pro-v1",
        board_profile_id="stm32-environment-controller-2l-v1",
        rulepack_digest="sha256:" + "1" * 64,
    )
    first = container.eda_authorities.configure(
        project_id=registered_project.id, authority=authority, idempotency_key="authority-v1"
    )
    assert container.eda_authorities.configure(
        registered_project.id, authority, "authority-v1"
    ) == first
    with pytest.raises(EdaAuthorityConflictError):
        container.eda_authorities.configure(
            registered_project.id,
            ProjectEdaAuthorityInput(
                eda_kind=EdaKind.LCEDA_PRO,
                eda_profile_id="lceda-pro-v2",
                board_profile_id="stm32-environment-controller-2l-v1",
                rulepack_digest="sha256:" + "1" * 64,
            ),
            "authority-v2",
        )
```

Also cover absent authority on an existing KiCad project, the `POST /api/v1/projects/{project_id}/eda-authority` strict body, the matching CLI command, unknown enum rejection, and migration `0006 -> 0007 -> head`.

- [ ] **Step 2: Run the authority tests and record the red state**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/integration/test_eda_authority.py tests/integration/test_migrations.py tests/e2e/test_api_cli.py -q
```

Expected: failures because no authority table, domain model, API route, or CLI command exists.

- [ ] **Step 3: Add the authority value objects, table, store, and adapters**

```python
class EdaKind(StrEnum):
    KICAD = "kicad"
    LCEDA_PRO = "lceda_pro"

@dataclass(frozen=True, slots=True)
class ProjectEdaAuthorityInput:
    eda_kind: EdaKind
    eda_profile_id: str
    board_profile_id: str
    rulepack_digest: str

class EdaAuthorityConflictError(RequestInvalidError):
    pass

@dataclass(frozen=True, slots=True)
class ProjectEdaAuthority:
    project_id: str
    eda_kind: EdaKind
    eda_profile_id: str
    board_profile_id: str
    rulepack_digest: str
    canonical_digest: str
    created_at: datetime

class EdaOperation(StrEnum):
    SNAPSHOT = "snapshot"
    CREATE_CANDIDATE = "create_candidate"
    APPLY_OPERATIONS = "apply_operations"
    RUN_DRC = "run_drc"
    EXPORT_RELEASE = "export_release"
```

Create `project_eda_authorities` with a primary-key `project_id`, a unique `(project_id, idempotency_key)` replay key, canonical digest, and foreign key cascade to `projects`. `configure()` must reject a second non-identical authority even with a new idempotency key. Register the store in `Container`, map domain errors to HTTP 409/CLI `EDA_AUTHORITY_CONFLICT`, and add the optional complete authority tuple to `project add` and `POST /api/v1/projects`; callers omitting it preserve the existing KiCad registration syntax, while an incomplete or `lceda_pro` tuple is rejected.

- [ ] **Step 4: Run focused authority and migration verification**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/integration/test_eda_authority.py tests/integration/test_migrations.py tests/e2e/test_api_cli.py -q
.\.venv\Scripts\python.exe -m alembic -c alembic.ini upgrade head
```

Expected: all selected tests pass and Alembic exits 0 without changing preexisting projects.

### Task 2: Implement the 嘉立创专业版 Capability Gate (P0)

**Files:**
- Create: `src/pcbflow/lceda_pro.py`
- Modify: `src/pcbflow/config.py`
- Modify: `src/pcbflow/container.py`
- Modify: `src/pcbflow/cli.py`
- Modify: `src/pcbflow/api.py`
- Create: `tests/unit/test_lceda_pro.py`
- Create: `tests/fixtures/lceda-pro/minimal/README.md`
- Modify: `README.md`

**Interfaces:**
- Produces `LcedaProAdapter.probe() -> EdaCapability` with version, executable digest, profile id, discovered official operations, reason, and `write_verified`.
- Produces `EdaCapabilityError` and its `LcedaProCapabilityError` subtype with stable code `LCEDA_PRO_WRITE_CAPABILITY_UNVERIFIED` whenever an operation needs an unverified write path.
- Produces `pcbflow doctor --json` fields `lceda_pro` and `pcbflow eda probe lceda-pro --json`; neither command writes a native project.

- [ ] **Step 1: Write probe and fail-closed tests**

```python
def test_probe_reports_an_installed_gui_without_claiming_write_support(tmp_path: Path) -> None:
    executable = tmp_path / "lceda-pro.exe"
    executable.write_bytes(b"fixture")
    adapter = LcedaProAdapter(FakeRunner(version="3.2.166"), executable)

    capability = adapter.probe()

    assert capability.available is True
    assert capability.profile_id == "lceda-pro-v1"
    assert capability.write_verified is False
    assert capability.operations == frozenset()
    with pytest.raises(LcedaProCapabilityError, match="LCEDA_PRO_WRITE_CAPABILITY_UNVERIFIED"):
        adapter.require(EdaOperation.APPLY_OPERATIONS, capability)
```

Also test missing executable, changing executable digest, unparseable version, unsupported version, and a test-only official automation bridge that verifies the complete create/save/reopen/snapshot contract before returning `write_verified=True`.

- [ ] **Step 2: Run the probe tests and record the red state**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_lceda_pro.py tests/unit/test_config.py -q
```

Expected: failures because the LCEDA adapter and configuration variables do not exist.

- [ ] **Step 3: Implement discovery, evidence fields, and fail-closed operation checks**

```python
@dataclass(frozen=True, slots=True)
class EdaCapability:
    available: bool
    executable: Path | None
    version: str | None
    executable_digest: str | None
    profile_id: str | None
    profile_revision: int | None
    operations: frozenset[EdaOperation]
    write_verified: bool
    reason: str | None

class EdaCapabilityError(RequestInvalidError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code

class LcedaProCapabilityError(EdaCapabilityError):
    pass

def require_lceda_operation(capability: EdaCapability, operation: EdaOperation) -> None:
    if not capability.write_verified or operation not in capability.operations:
        raise LcedaProCapabilityError("LCEDA_PRO_WRITE_CAPABILITY_UNVERIFIED")
```

Add optional `PCBFLOW_LCEDA_PRO_EXECUTABLE` and `PCBFLOW_LCEDA_PRO_OFFICIAL_BRIDGE` settings. The bridge must identify an installed official CLI, API, or plugin by version and complete a frozen minimal create/save/reopen/readback contract; a GUI executable, file association, or undocumented archive alone never marks an operation verified. The README fixture must list exactly one board outline, two copper layers, one footprint, one net, one keepout, one GND zone, and one mounting hole as the data required for that bridge test.

- [ ] **Step 4: Run unit and local capability checks**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_lceda_pro.py tests/unit/test_config.py -q
.\.venv\Scripts\pcbflow.exe doctor --json
.\.venv\Scripts\pcbflow.exe eda probe lceda-pro --json
```

Expected: tests pass. On a machine without a verified official bridge, probe output is explicit `write_verified: false` with a reason; it is not a passing write capability.

### Task 3: Persist P0 Probe Evidence and Gate PCB Work (P0)

**Files:**
- Create: `src/pcbflow/pcb_workflow.py`
- Modify: `src/pcbflow/container.py`
- Modify: `src/pcbflow/tasks.py`
- Modify: `src/pcbflow/api.py`
- Modify: `src/pcbflow/cli.py`
- Create: `tests/integration/test_lceda_capability_gate.py`
- Modify: `tests/e2e/test_api_cli.py`

**Interfaces:**
- Produces task kind `pcb.lceda_pro_capability_probe` and `CapabilityGateService.enqueue(project_id, idempotency_key) -> Task`.
- Persists the canonical `EdaCapability` as `application/vnd.pcbflow.eda-capability+json` Artifact and `lceda_pro_capability` Evidence.
- Produces stable terminal failure `LCEDA_PRO_WRITE_CAPABILITY_UNVERIFIED` for a request that requires a write-capable LCEDA candidate.

- [ ] **Step 1: Write capability-task lifecycle tests**

```python
def test_capability_task_persists_a_digest_bound_negative_result(container, lceda_project) -> None:
    task = container.capability_gate.enqueue(lceda_project.id, "probe-v1")
    assert container.worker.run_once() is True

    completed = container.tasks.get(task.id)
    assert completed.status is TaskStatus.SUCCEEDED
    assert completed.result["write_verified"] is False
    evidence = container.evidence.list_for_project(lceda_project.id)
    assert evidence[-1].kind == "lceda_pro_capability"
    assert container.artifacts.verify(evidence[-1].artifact_digest)
```

Add cases for an authority of kind `kicad`, idempotent enqueue replay, cancellation before an external bridge call, and blocking a candidate request that names `lceda_pro` while the saved capability has no verified write operations.

- [ ] **Step 2: Run the capability-task tests and record the red state**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/integration/test_lceda_capability_gate.py tests/e2e/test_api_cli.py -q
```

Expected: failures because the task handler, evidence type, and public entry points do not exist.

- [ ] **Step 3: Register the handler and attach content-addressed evidence**

```python
LCEDA_CAPABILITY_TASK_KIND = "pcb.lceda_pro_capability_probe"

def __call__(self, lease: TaskLease) -> dict[str, object]:
    capability = self._adapter.probe()
    data = canonical_json_bytes(asdict(capability))
    descriptor = self._artifacts.put_bytes(
        data, "application/vnd.pcbflow.eda-capability+json"
    )
    self._evidence.add_report(
        project_id=str(lease.payload["project_id"]),
        task_id=lease.task_id,
        descriptor=descriptor,
        kind="lceda_pro_capability",
        subject="lceda-pro-v1",
        verdict="pass" if capability.write_verified else "blocked",
    )
    return {"capability_digest": descriptor.digest, "write_verified": capability.write_verified}
```

Use the existing Worker task cancellation scope and fencing methods before publishing Artifact metadata or Evidence. The API and CLI enqueue this task only for a `lceda_pro` project, and they return the normal task resource instead of holding an HTTP request open.

- [ ] **Step 4: Run P0 completion checks**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/integration/test_lceda_capability_gate.py tests/unit/test_lceda_pro.py tests/e2e/test_api_cli.py -q
git diff --check
```

Expected: all selected tests pass. A negative capability result is a correctly completed P0 result; it blocks native writes while preserving read-only and algorithm work.

### Task 4: Define BoardIR, Rule Packs, and the STM32 Reference Fixture (P1)

**Files:**
- Create: `src/pcbflow/board/__init__.py`
- Create: `src/pcbflow/board/ir.py`
- Create: `src/pcbflow/board/rulepack.py`
- Create: `src/pcbflow/board/operations.py`
- Create: `src/pcbflow/board/validation.py`
- Create: `tests/fixtures/boardir/stm32-environment-controller-2l-v1.json`
- Create: `tests/fixtures/boardir/stm32-environment-controller-2l-rulepack.json`
- Create: `tests/unit/test_board_ir.py`
- Create: `tests/unit/test_board_rulepack.py`
- Create: `tests/unit/test_board_validation.py`

**Interfaces:**
- Produces `BoardSnapshot`, `BoardObjectId`, `PointUm`, `RectUm`, `Keepout`, `Footprint`, `Pad`, `Net`, `NetClass`, `RouteSegment`, `Via`, `CopperZone`, and `OpaqueNode`.
- Produces `ManufacturingRulePack.load_json(data: bytes) -> ManufacturingRulePack` and `BoardRuleChecker.check(snapshot, rulepack) -> tuple[NormalizedFinding, ...]`.
- Produces typed `BoardOperation` variants `PlaceFootprints`, `RouteNets`, `CreateCopperZones`, `AddGroundStitching`, and `LockBoardObjects`.

- [ ] **Step 1: Write BoardIR validation and fixture tests**

```python
def test_stm32_fixture_preserves_all_hard_constraints() -> None:
    snapshot = BoardSnapshot.load_json(STM32_FIXTURE.read_bytes())
    rulepack = ManufacturingRulePack.load_json(RULEPACK_FIXTURE.read_bytes())

    assert snapshot.board_size_um == (100_000, 80_000)
    assert snapshot.footprint("U_WIFI").keepout_ids == ("ko_esp_antenna",)
    assert snapshot.footprint("U_MCU").placement_lock is False
    assert snapshot.net("GND").net_class == "ground"
    assert BoardRuleChecker().check(snapshot, rulepack) == ()
```

Add strict-schema cases for duplicate native ids, coordinates outside the outline, invalid layers, a route inside `ko_esp_antenna`, a locked object move, a power trace thinner than its net-class rule, and an opaque node omitted by a proposed write.

- [ ] **Step 2: Run BoardIR tests and record the red state**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_board_ir.py tests/unit/test_board_rulepack.py tests/unit/test_board_validation.py -q
```

Expected: failures because BoardIR models, rule pack parsing, and fixtures do not exist.

- [ ] **Step 3: Implement strict, integer, canonical BoardIR**

```python
@dataclass(frozen=True, slots=True)
class PointUm:
    x: int
    y: int

@dataclass(frozen=True, slots=True)
class BoardSnapshot:
    schema_version: Literal["1.0"]
    profile_id: str
    outline: tuple[PointUm, ...]
    layers: tuple[str, ...]
    footprints: tuple[Footprint, ...]
    nets: tuple[Net, ...]
    keepouts: tuple[Keepout, ...]
    routes: tuple[RouteSegment, ...]
    vias: tuple[Via, ...]
    copper_zones: tuple[CopperZone, ...]
    opaque_nodes: tuple[OpaqueNode, ...]

    def canonical_digest(self) -> str:
        return canonical_digest(self.to_canonical_dict())
```

Encode the reference fixture with fixed IDs for `J_USB_C`, `U_MCU`, `U_WIFI`, `OLED1`, `K1`, `K2`, `Q1`, `Q2`, `J_RELAY_OUT`, `J_MOS_OUT`, `J_SWD`, `J_UART`, four mounting holes, ESP antenna keepout, crystal keepout, ADC/I2C quiet zone, power zone, and the listed V1 net classes. Encode 8 mil as `203`, 20 mil as `508`, and 80 mil as `2032` micrometres.

- [ ] **Step 4: Run BoardIR regression and property checks**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_board_ir.py tests/unit/test_board_rulepack.py tests/unit/test_board_validation.py -q
.\.venv\Scripts\python.exe -m pytest tests/unit/test_board_ir.py --hypothesis-show-statistics -q
```

Expected: all tests pass; canonical digests are stable and invalid geometry is rejected before any adapter is called.

### Task 5: Add Safe Board Adapter and Isolated Candidate Contracts (P1)

**Files:**
- Create: `src/pcbflow/board/adapter.py`
- Modify: `src/pcbflow/lceda_pro.py`
- Create: `src/pcbflow/board/fixture_adapter.py`
- Create: `tests/unit/test_board_adapter.py`
- Create: `tests/contract/test_lceda_pro_adapter.py`
- Create: `tests/fixtures/lceda-pro/minimal/expected-boardir.json`

**Interfaces:**
- Produces `PcbEdaAdapter`, `CandidateWorkspace`, `ReleaseArtifacts`, `BoardSemanticDiff`, `BoardSemanticMismatchError`, and `UnsupportedEdaOperationError`.
- `create_candidate()` may only copy into an isolated directory; `apply_operations()` must re-read a snapshot and fail when an unannounced native change or dropped opaque node is observed.
- `FixtureBoardAdapter` permits algorithm tests without claiming native LCEDA write support.

- [ ] **Step 1: Write adapter isolation and semantic-diff tests**

```python
def test_adapter_rejects_unannounced_native_changes(tmp_path: Path) -> None:
    adapter = FixtureBoardAdapter(extra_native_change=True)
    candidate = adapter.create_candidate(tmp_path / "source", tmp_path / "candidate")
    before = adapter.load_snapshot(candidate.path)

    with pytest.raises(BoardSemanticMismatchError):
        adapter.apply_operations(
            candidate,
            (PlaceFootprints((Placement("U_MCU", PointUm(50_000, 40_000), 0),)),),
            before,
        )
```

Add contract cases for output outside source, an unchanged `PlacementLock`, unmodified `OpaqueNode`, failed capability requirement, save/reopen snapshot equivalence, and a real-tool test marked `lceda_pro` that skips unless P0 exposes a verified official bridge.

- [ ] **Step 2: Run adapter tests and record the red state**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_board_adapter.py tests/contract/test_lceda_pro_adapter.py -q
```

Expected: failures because the board adapter protocol and fixture adapter do not exist.

- [ ] **Step 3: Implement the protocol and fail-closed LCEDA methods**

```python
class PcbEdaAdapter(Protocol):
    def probe(self) -> EdaCapability: ...
    def load_snapshot(self, project_dir: Path) -> BoardSnapshot: ...
    def create_candidate(self, source_dir: Path, destination_dir: Path) -> CandidateWorkspace: ...
    def apply_operations(
        self, candidate: CandidateWorkspace,
        operations: tuple[BoardOperation, ...], expected_snapshot: BoardSnapshot,
    ) -> BoardSnapshot: ...
    def run_drc(self, candidate: CandidateWorkspace) -> tuple[ValidationReport, ...]: ...
    def export_release(
        self, candidate: CandidateWorkspace, rulepack: ManufacturingRulePack,
    ) -> ReleaseArtifacts: ...

@dataclass(frozen=True, slots=True)
class CandidateWorkspace:
    path: Path
    output_dir: Path

@dataclass(frozen=True, slots=True)
class BoardSemanticDiff:
    changed_object_ids: tuple[str, ...]
    unexpected_object_ids: tuple[str, ...]

    @property
    def is_empty(self) -> bool:
        return not self.changed_object_ids and not self.unexpected_object_ids

@dataclass(frozen=True, slots=True)
class ReleaseArtifacts:
    files: tuple[tuple[str, Path], ...]

class BoardSemanticMismatchError(ValueError):
    pass

class UnsupportedEdaOperationError(ValueError):
    pass
```

Implement `FixtureBoardAdapter` with copied JSON fixtures and deterministic readback. `LcedaProAdapter` must provide only methods actually proven by its P0 `EdaCapability`; until then `apply_operations()` and `export_release()` raise the stable capability error before touching native data. Preserve all opaque objects in snapshots and compare `BoardSemanticDiff` against the requested operation set.

- [ ] **Step 4: Run adapter contracts**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_board_adapter.py tests/contract/test_lceda_pro_adapter.py -q
git diff --check
```

Expected: fixture contracts pass; the real LCEDA contract is skipped with an explicit capability reason unless official automation has passed P0.

### Task 6: Persist PCB Candidates and Execute the P1 Lifecycle (P1)

**Files:**
- Create: `alembic/versions/0009_pcb_candidates.py`
- Create: `src/pcbflow/pcb_candidates.py`
- Modify: `src/pcbflow/design_tables.py`
- Modify: `src/pcbflow/container.py`
- Modify: `src/pcbflow/pcb_workflow.py`
- Modify: `src/pcbflow/api.py`
- Modify: `src/pcbflow/cli.py`
- Create: `tests/integration/test_pcb_candidates.py`
- Modify: `tests/integration/test_migrations.py`

**Interfaces:**
- Produces `PcbCandidateStatus`, `PcbCandidate`, `PcbCandidateNotReviewableError`, `PcbCandidateStore`, `PcbCandidateService.create(...)`, and task kind `pcb.generate_candidate`.
- A candidate freezes project authority, base revision/snapshot, BoardIR snapshot, rule pack, capability evidence, ordered operations, algorithm evidence and task id.
- Produces `PCB_CAPABILITY_GATE_BLOCKED`, `PCB_CANDIDATE_STALE`, and `PCB_CANDIDATE_NOT_REVIEWABLE` errors without updating an accepted project revision.

> **实施状态（2026-08-08）**：候选持久化、Worker、API/CLI、事务性任务入队、
> 冻结输入幂等、任务事务内取消镜像、SQL 条件 fence 和租约失效补偿已实现。候选
> capability digest 绑定已验证 artifact，`ready_for_g3` 结果核对冻结 digest，公开输出
> 始终为 `boardir_only`；原生 LCEDA Pro 写入仍由未验证 capability gate 阻断。候选/
> 迁移/任务回归为 `64 passed`，API/CLI E2E 为 `44 passed`，隔离 SQLite Alembic 往返已通过。

- [x] **Step 1: Write candidate state and fencing tests**

```python
def test_candidate_cannot_publish_after_its_task_lease_is_lost(container, managed_lceda_project) -> None:
    candidate = container.pcb_candidates.create(
        project_id=managed_lceda_project.id,
        base_revision=managed_lceda_project.current_revision,
        capability_digest="sha256:" + "2" * 64,
        board_snapshot_digest="sha256:" + "3" * 64,
        rulepack_digest="sha256:" + "4" * 64,
        idempotency_key="candidate-v1",
    )
    lease = container.tasks.claim_next("worker-a", NOW, 1)
    assert lease is not None
    with pytest.raises(StaleLeaseError):
        container.pcb_candidates.mark_ready_for_g3(
            candidate.id, lease.task_id, lease.lease_token, NOW + timedelta(seconds=2)
        )
```

Add legal and illegal transitions, idempotent candidate creation, cancellation, stale base revision, exact operation digest matching, and P0-negative capability block tests.

- [x] **Step 2: Run candidate and migration tests and record the red state**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/integration/test_pcb_candidates.py tests/integration/test_migrations.py -q
```

Expected: failures because the candidate table, transition store, and handler do not exist.

- [x] **Step 3: Add the durable state machine and Worker handler**

```python
class PcbCandidateStatus(StrEnum):
    QUEUED = "queued"
    EXECUTING = "executing"
    READY_FOR_G3 = "ready_for_g3"
    G3_APPROVED = "g3_approved"
    RELEASE_PENDING = "release_pending"
    READY_FOR_G4 = "ready_for_g4"
    RELEASED = "released"
    VALIDATION_FAILED = "validation_failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"

class PcbCandidateNotReviewableError(RequestInvalidError):
    pass
```

`pcb_candidates` must contain immutable input digests, mutable status/result/error fields, `task_id`, timestamps, and an optimistic `version`. Use the same `TaskRepository.assert_active()` fence before every candidate state, Evidence, or Artifact publication. `PcbCandidateService.create()` verifies the authority and most recent capability Artifact before queuing work; it never selects an adapter by guessing a file extension.

- [x] **Step 4: Run P1 lifecycle verification**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/integration/test_pcb_candidates.py tests/integration/test_migrations.py tests/integration/test_tasks.py -q
.\.venv\Scripts\python.exe -m alembic -c alembic.ini downgrade base
.\.venv\Scripts\python.exe -m alembic -c alembic.ini upgrade head
```

Expected: candidate state survives restart, migrations round-trip, and stale/cancelled workers cannot publish a candidate.

### Task 7: Solve Hard-Constrained Placement Before Optimization (P2)

**Status:** Complete — independently reviewed; see `.superpowers/sdd/task-7-report.md` for measured verification.

**Files:**
- Modify: `pyproject.toml`
- Create: `src/pcbflow/board/placement.py`
- Modify: `src/pcbflow/board/operations.py`
- Modify: `src/pcbflow/pcb_workflow.py`
- Create: `tests/unit/test_board_placement.py`
- Create: `tests/integration/test_pcb_placement.py`

**Interfaces:**
- Produces `PlacementSolver.solve(snapshot, rulepack, *, seed: int, starts: int, iterations: int) -> PlacementResult`.
- Produces deterministic `PlacementEvidence` and a `PlaceFootprints` operation whose placements never move a `PlacementLock`.
- CP-SAT selects discrete legal regions; seeded simulated annealing only refines CP-SAT-feasible placements.

- [x] **Step 1: Write feasible, infeasible, and deterministic placement tests**

```python
def test_solver_keeps_esp_antenna_and_locked_connectors_fixed(stm32_board, rulepack) -> None:
    result = PlacementSolver().solve(
        stm32_board, rulepack, seed=7, starts=4, iterations=400
    )

    assert result.operations[0].placements_by_id["J_USB_C"] == stm32_board.footprint("J_USB_C").placement
    assert not result.footprint_bounds("U_WIFI").intersects(stm32_board.keepout("ko_esp_antenna").bounds)
    assert result.findings == ()
    assert PlacementSolver().solve(stm32_board, rulepack, seed=7, starts=4, iterations=400) == result
```

Add tests for courtyard overlap, quiet-zone intrusion, unavailable legal region, power-area separation, fixed MCU crystal proximity, and retained result evidence for every seed.

- [x] **Step 2: Run placement tests and record the red state**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_board_placement.py tests/integration/test_pcb_placement.py -q
```

Expected: failures because CP-SAT dependency and placement solver do not exist.

- [x] **Step 3: Implement legal-region CP-SAT and seeded refinement**

```python
@dataclass(frozen=True, slots=True)
class PlacementResult:
    operations: tuple[PlaceFootprints, ...]
    score: int
    evidence: PlacementEvidence
    findings: tuple[NormalizedFinding, ...]

def score_layout(snapshot: BoardSnapshot, placements: Mapping[str, Placement]) -> int:
    return (
        100 * weighted_ratsnest_length(snapshot, placements)
        + 250 * critical_net_length(snapshot, placements)
        + 400 * power_loop_area(snapshot, placements)
        + 500 * quiet_zone_noise_penalty(snapshot, placements)
        + 150 * thermal_cluster_penalty(snapshot, placements)
        + 75 * connector_access_penalty(snapshot, placements)
    )
```

Generate candidate anchor cells from each footprint's allowed region, reject all cells intersecting keepouts/courtyards/locks before CP-SAT, and use no-overlap constraints. Annealing moves only one unlocked footprint within its legal cells, uses the declared seed, and records `objective_version="placement-v1"`, all start scores, chosen score, and placement digest.

- [x] **Step 4: Run P2 placement verification**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_board_placement.py tests/integration/test_pcb_placement.py -q
.\.venv\Scripts\python.exe -m pytest tests/unit/test_board_placement.py --hypothesis-show-statistics -q
```

Expected: all results obey hard constraints; infeasible boards end as explicit findings rather than relaxed placements.

### Task 8: Route Only the V1 Network Set With Negotiated Congestion (P3)

**Status:** Completed — deterministic BoardIR-only P3 routing is verified against the frozen fixture and rule pack; see `.superpowers/sdd/task-8-report.md`.

**Files:**
- Create: `src/pcbflow/board/routing.py`
- Modify: `src/pcbflow/board/operations.py`
- Modify: `src/pcbflow/board/validation.py`
- Modify: `src/pcbflow/pcb_workflow.py`
- Create: `tests/unit/test_board_routing.py`
- Create: `tests/integration/test_pcb_routing.py`

**Interfaces:**
- Produces `Autorouter.route(snapshot, rulepack, net_ids, *, seed: int) -> RoutingResult`.
- Critical signal nets use a 45-degree A*/Lee search; ordinary signal nets use Pathfinder-style historical congestion and deterministic rip-up/retry; `RouteLock` objects cannot be removed.
- Logic/load power nets return a `PowerRoutePlan` or `PCB_POWER_ROUTE_REQUIRES_TOPOLOGY` finding, never a narrow signal trace.

- [x] **Step 1: Write constrained routing tests**

```python
def test_router_never_crosses_esp_keepout_or_rips_a_locked_trace(stm32_board, rulepack) -> None:
    result = Autorouter().route(
        stm32_board, rulepack, ("I2C_SCL", "I2C_SDA", "UART_TX"), seed=11
    )

    assert all(segment.angle_degrees in {0, 45, 90, 135} for segment in result.segments)
    assert all(not segment.intersects(stm32_board.keepout("ko_esp_antenna").bounds) for segment in result.segments)
    assert result.locked_route_ids == frozenset({"rt_swd_swo"})
```

Add routes blocked by a courtyard, a negotiated congestion success after one rip-up, an unrouteable net finding, a quiet-zone detour, via-count limits, and thin 3 A load power rejection.

- [x] **Step 2: Run routing tests and record the red state**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_board_routing.py tests/integration/test_pcb_routing.py -q
```

Expected: failures because route-grid, router, and power topology policy do not exist.

- [x] **Step 3: Implement priority routing and bounded negotiation**

```python
ROUTING_PRIORITY = ("USB_5V", "3V3_DECOUPLING", "HSE", "SWD", "I2C", "ADC")

@dataclass(frozen=True, slots=True)
class RoutingResult:
    operations: tuple[RouteNets, ...]
    segments: tuple[RouteSegment, ...]
    findings: tuple[NormalizedFinding, ...]
    evidence: RoutingEvidence
```

Build obstacles from board edge clearance, pads, footprint courtyards, keepouts, existing route locks, and copper exclusions. The A* cost must sum length, vias, quiet-zone crossing, keepout proximity, continuous-GND disruption, and historical congestion. Sort net IDs before each retry round, cap retries from the rule pack, and persist every selected path, removed unlocked route, round score, and final unconnected set in `RoutingEvidence`.

- [x] **Step 4: Run P3 routing verification**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_board_routing.py tests/integration/test_pcb_routing.py tests/unit/test_board_validation.py -q
```

Expected: permitted V1 signal nets are routed or explicitly reported; locks, keepouts, net-class widths, and power topology boundaries remain intact.

### Task 9: Plan GND Copper, Thermal Pads, and Stitching Vias (P4)

> **实施状态（2026-08-08）**：已完成 BoardIR-only 的确定性矩形 tile 铜皮规划、
> rule-pack 铜皮策略、热焊盘与地过孔操作回放、铜皮净空/边缘/孤岛/连通性检查。
> `pcb_workflow.py` 保持不变：候选/native DRC workflow 属于 Task 10；Task 9
> 在 planner 和 fixture adapter 回放前后运行 `BoardRuleChecker`，不会宣称原生写入。

**Files:**
- Create: `src/pcbflow/board/copper.py`
- Modify: `src/pcbflow/board/operations.py`
- Modify: `src/pcbflow/board/validation.py`
- Modify: `src/pcbflow/pcb_workflow.py`
- Create: `tests/unit/test_board_copper.py`
- Create: `tests/integration/test_pcb_copper.py`

**Interfaces:**
- Produces `CopperPlanner.plan(snapshot, rulepack) -> CopperResult`, `ThermalPolicy`, and `StitchingPolicy`.
- Produces top and bottom explicit GND zones, reports islands/dead copper, and adds vias only in continuous, allowed ground areas.
- Runs BoardIR connectivity/geometry checks after copper refill before requesting native DRC.

- [x] **Step 1: Write copper policy tests**

```python
def test_copper_plan_excludes_antenna_and_reports_islands(stm32_board, rulepack) -> None:
    result = CopperPlanner().plan(stm32_board, rulepack)

    assert {zone.layer for zone in result.zones} == {"F.Cu", "B.Cu"}
    assert all(zone.net_id == "GND" for zone in result.zones)
    assert all(not zone.bounds.intersects(stm32_board.keepout("ko_esp_antenna").bounds) for zone in result.zones)
    assert "PCB.COPPER.ISLAND" in {finding.rule_id for finding in result.findings}
```

Add tests for thermal-relief selection, no stitching via through a quiet-zone/board-edge keepout, a valid stitching grid, and a zone that disconnects GND after route changes.

- [x] **Step 2: Run copper tests and record the red state**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_board_copper.py tests/integration/test_pcb_copper.py -q
```

Expected: failures because copper planning and post-fill validation do not exist.

- [x] **Step 3: Implement explicit zone and stitching policies**

```python
@dataclass(frozen=True, slots=True)
class CopperResult:
    operations: tuple[CreateCopperZones | AddGroundStitching, ...]
    zones: tuple[CopperZone, ...]
    vias: tuple[Via, ...]
    findings: tuple[NormalizedFinding, ...]
    evidence: CopperEvidence
```

Subtract every copper keepout, antenna keepout, mechanical cutout, board-edge clearance, and sensitive-area exclusion from GND zone polygons. Apply the rulepack thermal policy per pad, flood-fill connected copper to identify islands, and add only ground vias whose annulus and clearance satisfy the rule pack. Persist zone polygon digests, island IDs, excluded regions, via coordinates, and connectivity results.

- [x] **Step 4: Run P4 copper verification**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_board_copper.py tests/integration/test_pcb_copper.py tests/unit/test_board_validation.py -q
```

Expected: copper results cannot enter native validation when antenna/keepout/copper-connectivity checks fail.

### Task 10: Execute Candidate Operations, Native DRC, and G3 Approval (P4)

**Files:**
- Modify: `src/pcbflow/pcb_candidates.py`
- Modify: `src/pcbflow/pcb_workflow.py`
- Modify: `src/pcbflow/approvals.py`
- Modify: `src/pcbflow/api.py`
- Modify: `src/pcbflow/cli.py`
- Create: `tests/integration/test_pcb_g3.py`
- Modify: `tests/e2e/test_api_cli.py`

**Interfaces:**
- Produces task kind `pcb.generate_candidate` that creates a candidate workspace, applies typed operations, re-reads BoardIR, runs the native adapter DRC, and publishes complete evidence only under an active task fence.
- Produces `PcbApprovalService.decide_g3(candidate_id, candidate_digest, ...) -> PcbCandidate` and gate `G3_PCB`.
- A candidate may be `READY_FOR_G3` only when native DRC has no blocking finding, BoardIR finding set is empty for blocking severity, unconnected nets are zero, and all frozen digests match.

- [x] **Step 1: Write G3 happy-path and rejection tests**

```python
def test_g3_rejects_a_candidate_when_native_drc_has_a_blocker(container, ready_candidate) -> None:
    container.pcb_adapter = FixtureBoardAdapter(drc_findings=(
        NormalizedFinding("LCEDA.DRC.CLEARANCE", "error", "seg_1", "clearance"),
    ))

    candidate = run_candidate_task(container, ready_candidate.id)

    assert candidate.status is PcbCandidateStatus.VALIDATION_FAILED
    with pytest.raises(PcbCandidateNotReviewableError):
        container.pcb_approvals.decide_g3(candidate.id, "sha256:" + "5" * 64, "g3-v1", "reviewer", "approve")
```

Add a capability-blocked result that never invokes `apply_operations`, a source-tree digest check before/after execution, an evidence-set omission failure, a stale candidate digest, idempotent G3 replay, and a cancellation after native DRC but before publication.

- [x] **Step 2: Run G3 tests and record the red state**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/integration/test_pcb_g3.py tests/e2e/test_api_cli.py -q
```

Expected: failures because candidate execution, G3 records, and public APIs do not exist.

- [x] **Step 3: Implement evidence-bound execution and G3 decisions**

```python
G3_REQUIRED_EVIDENCE = frozenset({
    "pcb_input_snapshot", "eda_capability", "rulepack", "placement_evidence",
    "routing_evidence", "copper_evidence", "boardir_validation", "native_drc",
    "board_semantic_diff", "candidate_summary",
})

def eligible_for_g3(candidate: PcbCandidate) -> bool:
    return (
        candidate.status is PcbCandidateStatus.READY_FOR_G3
        and candidate.blocking_finding_count == 0
        and candidate.unconnected_net_count == 0
        and candidate.evidence_kinds == G3_REQUIRED_EVIDENCE
    )
```

Use `GateDecisionStore.add()` with gate `G3_PCB`, subject type `pcb_candidate`, base revision equal to the frozen candidate base, and an approval Artifact built with canonical JSON. Do not promote an accepted project revision merely because G3 is approved; a release task and G4 are still required.

- [x] **Step 4: Run P4/G3 verification**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/integration/test_pcb_g3.py tests/integration/test_pcb_candidates.py tests/e2e/test_api_cli.py -q
git diff --check
```

Expected: only complete, capability-proven, DRC-clean candidates become G3-reviewable; G3 evidence is immutable and idempotent.

> **Verified implementation status (2026-08-08):** Task 10 now uses a lease-fenced batch transaction for the complete evidence set, rechecks the transition clock after acquiring the database write lock, rolls back newly linked CAS objects when publication is cancelled or fails before evidence commit, recovers stale candidate transitions without leaving an error code, and converges unexpected execution failures to `VALIDATION_FAILED`. Fresh scoped verification: `test_pcb_g3.py` 32 passed, `test_pcb_candidates.py` 21 passed, `test_lceda_capability_gate.py` 17 passed, and `test_api_cli.py` 46 passed. `compileall` and `git diff --check` pass. The real LCEDA Pro bridge remains fail-closed until its official native DRC/write capability is verified; fixture adapters do not change that boundary.

### Task 11: Produce and Validate the Manufacturing Candidate Package (P5)

**Files:**
- Create: `src/pcbflow/manufacturing.py`
- Modify: `src/pcbflow/pcb_candidates.py`
- Modify: `src/pcbflow/pcb_workflow.py`
- Modify: `src/pcbflow/api.py`
- Modify: `src/pcbflow/cli.py`
- Create: `tests/unit/test_manufacturing.py`
- Create: `tests/integration/test_pcb_release.py`
- Modify: `README.md`
- Modify: `docs/DEVELOPMENT_GUIDE.md`

**Interfaces:**
- Produces `ReleaseManifest`, `ManufacturingValidator.validate(...) -> ManufacturingValidation`, task kind `pcb.export_release`, and gate `G4_RELEASE`.
- A release package contains only Artifact digests for Gerber, drills, BOM, CPL, assembly drawing, native DRC, rulepack, candidate summary, and manifest.
- Produces `PCB_RELEASE_CAPABILITY_BLOCKED`, `MANUFACTURING_REFERENCE_SET_MISMATCH`, and `MANUFACTURING_ARTIFACT_MISSING` failures.

- [x] **Step 1: Write release manifest and BOM/CPL cross-check tests**

```python
def test_release_rejects_mismatched_bom_and_cpl_references() -> None:
    validation = ManufacturingValidator().validate(
        bom=b"Designator,Comment\nR1,10k\n",
        cpl=b"Designator,Mid X,Mid Y\nR2,10,10\n",
        required_artifacts={"gerber", "drill", "assembly"},
    )

    assert validation.ok is False
    assert validation.findings[0].rule_id == "PCB.MFG.REFERENCE_SET_MISMATCH"
```

Add missing Gerber/drill checks, DNP/hand-solder declaration validation, manifest digest determinism, generation from the exact G3-approved candidate, native export capability rejection, and artifact tamper detection.

- [x] **Step 2: Run manufacturing tests and record the red state**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_manufacturing.py tests/integration/test_pcb_release.py -q
```

Expected: failures because manufacturing models, release task, and validations do not exist.

- [x] **Step 3: Implement content-addressed release validation and G4**

```python
@dataclass(frozen=True, slots=True)
class ReleaseManifest:
    schema_version: Literal["1.0"]
    candidate_id: str
    candidate_digest: str
    authority_digest: str
    capability_digest: str
    rulepack_digest: str
    artifacts: tuple[ReleaseArtifact, ...]

    def canonical_digest(self) -> str:
        return canonical_digest(asdict(self))
```

Call `adapter.export_release()` only after `G3_APPROVED` and `EdaOperation.EXPORT_RELEASE` verification. Store every returned file by digest, validate the reference-designator sets from BOM/CPL, require the DRC/rulepack/candidate hashes to match the candidate row, and create the manifest last. G4 uses `GateDecisionStore.add()` with the manifest digest as the subject digest and changes only `READY_FOR_G4 -> RELEASED`.

- [x] **Step 4: Run P5 manufacturing verification**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_manufacturing.py tests/integration/test_pcb_release.py tests/e2e/test_api_cli.py -q
```

Expected: a release can be reproduced from its manifest; unverified native export paths remain blocked rather than producing incomplete files.

### Task 12: Add KiCad 10 BoardIR Parity Without Dual-Write Sync (P6)

**Files:**
- Create: `src/pcbflow/board/kicad_adapter.py`
- Modify: `src/pcbflow/board/adapter.py`
- Modify: `src/pcbflow/container.py`
- Create: `tests/unit/test_kicad_board_adapter.py`
- Create: `tests/contract/test_kicad_board_adapter.py`
- Modify: `tests/contract/test_kicad_cli.py`

**Interfaces:**
- Produces `KicadBoardAdapter.load_snapshot(project_dir) -> BoardSnapshot` for the V1 projection of `.kicad_pcb` data and reuses `KicadCli` only for native DRC.
- Produces `compare_board_snapshots(left, right) -> BoardSemanticDiff` with stable categories for outline, layer, footprint, pad/net, keepout, route, via, copper-zone, and opaque-object differences.
- Does not implement native KiCad BoardIR write operations or automatic merge of KiCad and LCEDA projects.

- [x] **Step 1: Write KiCad parity tests**

```python
def test_kicad_fixture_projects_to_the_same_algorithm_contract(kicad_board_dir: Path) -> None:
    adapter = KicadBoardAdapter(kicad_cli=FakeKicadCli())
    snapshot = adapter.load_snapshot(kicad_board_dir)
    result = PlacementSolver().solve(snapshot, V1_RULEPACK, seed=17, starts=2, iterations=100)

    assert snapshot.profile_id == "kicad-10-v1"
    assert result.findings == ()
    assert compare_board_snapshots(snapshot, snapshot).is_empty
```

Add parser rejection for unsupported native nodes that cannot be preserved, geometry/net mapping cases, real KiCad 10 contract marked `kicad`, and a test proving the adapter has no `apply_operations` implementation.

- [x] **Step 2: Run KiCad parity tests and record the red state**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_kicad_board_adapter.py tests/contract/test_kicad_board_adapter.py -q
```

Expected: failures because the BoardIR projection and semantic diff helper do not exist.

- [x] **Step 3: Implement the read-only projection and comparison**

```python
class KicadBoardAdapter:
    def load_snapshot(self, project_dir: Path) -> BoardSnapshot:
        """Project supported KiCad 10 board primitives into BoardIR; retain unknown data as opaque."""

    def run_drc(self, candidate: CandidateWorkspace) -> tuple[ValidationReport, ...]:
        return tuple(parse_kicad_report(raw.kind, raw.data) for raw in self._kicad.validate(candidate.path, candidate.output_dir))
```

Parse only V1 board primitives already named in the interface, retain unsupported but locatable nodes as opaque, and reject an attempted write with `KICAD_BOARD_WRITE_NOT_IMPLEMENTED`. The parity suite compares the common BoardIR fixture and algorithm evidence, never native project bytes across EDA tools.

- [x] **Step 4: Run P6 compatibility verification**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_kicad_board_adapter.py tests/contract/test_kicad_board_adapter.py tests/contract/test_kicad_cli.py -q
.\.venv\Scripts\python.exe -m pytest -m kicad -v
```

Expected: unit contracts pass; the real KiCad test passes with the installed KiCad 10 profile or skips only when the executable is unavailable.

### Task 13: Expose the End-to-End PCB Workflow and Document Exact Operational Boundaries

**Files:**
- Modify: `src/pcbflow/api.py`
- Modify: `src/pcbflow/cli.py`
- Modify: `README.md`
- Modify: `docs/DEVELOPMENT_GUIDE.md`
- Modify: `docs/superpowers/specs/2026-08-04-lceda-pro-pcb-automation-design.md`
- Modify: `docs/superpowers/plans/2026-08-04-lceda-pro-pcb-automation.md`
- Modify: `tests/e2e/test_api_cli.py`

**Interfaces:**
- Produces `pcbflow eda probe lceda-pro`, `pcbflow pcb candidate create`, `pcbflow pcb candidate show`, `pcbflow pcb candidate approve-g3`, `pcbflow pcb release export`, and `pcbflow pcb release approve-g4`.
- Produces matching REST resources under `/api/v1/projects/{project_id}/pcb-candidates` and `/api/v1/pcb-candidates/{candidate_id}`.
- Public output always states whether a candidate is `boardir_only`, `native_candidate`, or `release_candidate`; it never labels a capability-blocked result as a native board.

- [x] **Step 1: Write end-to-end API/CLI contract tests**

```python
response = await client.post(
    f"/api/v1/projects/{project_id}/pcb-candidates",
    headers={"Idempotency-Key": "pcb-candidate-v1"},
    json={"seed": 7, "net_ids": ["I2C_SCL", "I2C_SDA"]},
)
assert response.status_code == 202
assert response.json()["status"] == "queued"

blocked = runner.invoke(app, [
    "pcb", "release", "export", candidate_id,
    "--idempotency-key", "release-v1", "--json",
])
assert blocked.exit_code == 2
assert "PCB_RELEASE_CAPABILITY_BLOCKED" in blocked.output
```

Add strict-body validation, required idempotency keys, remote-mode path restrictions, cancellation, G3/G4 transition errors, and a fixture-adapter success path covering candidate creation through release manifest approval.

- [x] **Step 2: Run end-to-end tests and record the red state**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/e2e/test_api_cli.py -q
```

Expected: failures until the CLI groups, REST endpoints, error mappings, and workflow responses are wired.

- [x] **Step 3: Add thin transports and operational documentation**

```text
POST /api/v1/projects/{project_id}/pcb-candidates
GET  /api/v1/pcb-candidates/{candidate_id}
POST /api/v1/pcb-candidates/{candidate_id}:approve-g3
POST /api/v1/pcb-candidates/{candidate_id}:export-release
POST /api/v1/pcb-candidates/{candidate_id}:approve-g4
```

Keep scheduling, authority validation, gate decisions, and release checks in services; API/CLI only parse strict input and return serialized domain objects. Document the P0 probe procedure, the official-bridge requirement, the BoardIR-only fallback, the exact G3/G4 evidence requirements, and CLI/API workflows. Update the design-spec status only after each verified phase, not when code merely compiles.

- [x] **Step 4: Run public workflow verification**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/e2e/test_api_cli.py tests/integration/test_pcb_candidates.py tests/integration/test_pcb_g3.py tests/integration/test_pcb_release.py -q
.\.venv\Scripts\pcbflow.exe --help
.\.venv\Scripts\pcbflow.exe pcb --help
git diff --check
```

Expected: commands and endpoints expose only backed contracts, and the diff check emits no output.

### Task 14: Run the Full Regression, Tool Contracts, and Release Readiness Audit

**Files:**
- Modify: `README.md`
- Modify: `docs/DEVELOPMENT_GUIDE.md`
- Modify: `docs/superpowers/specs/2026-08-04-lceda-pro-pcb-automation-design.md`
- Modify: `docs/superpowers/plans/2026-08-04-lceda-pro-pcb-automation.md`

**Interfaces:**
- Produces a verification record containing test counts, coverage, Alembic base/head check, KiCad contract result, LCEDA capability result, and the G3/G4 fixture result.
- The final status differentiates an implementation-complete BoardIR/algorithm pipeline from a release-capable LCEDA pipeline when P0 lacks a verified official write bridge.

- [x] **Step 1: Run static and migration checks**

Run:

```powershell
.\.venv\Scripts\python.exe -m compileall -q src
.\.venv\Scripts\python.exe -m alembic -c alembic.ini downgrade base
.\.venv\Scripts\python.exe -m alembic -c alembic.ini upgrade head
git diff --check
```

Expected: all commands exit 0 and `git diff --check` has no output.

- [x] **Step 2: Run complete automated regression**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q --tb=short -p no:cacheprovider --cov=pcbflow --cov-report=term-missing --cov-fail-under=90 --basetemp .pytest-tmp-lceda-final
.\.venv\Scripts\python.exe -m pytest -m kicad -v
.\.venv\Scripts\python.exe -m pytest tests/contract/test_lceda_pro_adapter.py -v
```

Expected: unit/integration/e2e suite has no failures and coverage remains at least 90%; KiCad contract passes or has the documented unavailable-tool skip; LCEDA write contract passes only with a P0-verified official bridge and otherwise reports the explicit skip reason.

- [x] **Step 3: Audit the P0 decision and fixture exit criteria**

Run:

```powershell
.\.venv\Scripts\pcbflow.exe doctor --json
.\.venv\Scripts\pcbflow.exe eda probe lceda-pro --json
.\.venv\Scripts\pcbflow.exe pcb candidate show <candidate-id> --json
```

Expected: the capability digest, rulepack digest, BoardIR digest, algorithm evidence, native DRC state, and G3/G4 status are visible. Without verified operations, output clearly remains `boardir_only` and no native source or release artifact was changed.

- [x] **Step 4: Update the completion record with measured results only**

Record the actual command results, capability state, tested profile versions, Artifact digests, known manual-review work, and any intentional capability skip in the design specification and this plan. Do not mark P0 or release capability passed from an unverified GUI installation.

## Plan Self-Review

| Approved design requirement | Covered by |
| --- | --- |
| 项目级单一权威 EDA、无双主写入 | Tasks 1, 2, 5, 12, 13 |
| 嘉立创专业版 capability gate 和禁止猜测专有格式 | Tasks 2, 3, 5, 10, 14 |
| BoardIR、规则包、STM32 低压两层约束 | Tasks 4, 5, 6 |
| CP-SAT 与多起点模拟退火摆放 | Task 7 |
| A*/Lee、Pathfinder、rip-up/retry、功率网策略 | Task 8 |
| GND 铜皮、热焊盘、地过孔与铜皮检查 | Task 9 |
| 隔离候选、取消、fencing、原生 DRC、G3 | Tasks 5, 6, 10 |
| Gerber、钻孔、BOM、CPL、装配资料、G4 | Task 11 |
| KiCad 10 语义与算法并行回归 | Task 12 |
| REST/CLI、文档与全量验证 | Tasks 13, 14 |

- 路径、接口、任务种类、错误码和状态均在首次定义处给出，后续任务引用同一名称。
- P0 失败被设计为可验证的降级状态，不会跳过 native 写入、DRC 或制造出口的能力证明。
- 计划不包含未定义的后续占位工作；后续每个阶段都可通过其对应的 focused tests、迁移检查和证据摘要独立验收。
