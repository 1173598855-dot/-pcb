# 嘉立创专业版主工程 PCB 自动化设计

**状态：** 2026-08-10 实测：BoardIR/算法、候选、G3/G4 fixture、API/CLI、KiCad parity 与
release packaging 实现完成；真实 LCEDA native write/DRC/release 仍受未验证的官方 bridge
能力阻断，release-capable LCEDA pipeline 未完成。

**参考需求：** C:\GitHub\单片机项目\stm32-pcb-project-design.md

## 2026-08-10 实测完成记录

### 回归与工具合同

- Full gate 使用 Python 3.13.9、pytest 8.4.2：`832 passed, 1 skipped`，589.92 s；总覆盖率
  `90.00%`（12611 statements，1261 misses）。带 `--cov-fail-under=90` 的命令退出 0。
- 唯一全量 skip 是 `tests/contract/test_lceda_pro_adapter.py:16`，原因为
  `LCEDA Pro write capability unverified: lceda_pro_not_found`。
- 真实 KiCad marker contract：KiCad 10.0.4、profile `kicad-10-v1` revision 1、executable
  digest `sha256:e2fa37ddc9119a4b3c40cd61f5282c70ffc02c0190670d2fdb46dc11f3c95a93`；
  `3 passed, 830 deselected`。
- 真实 LCEDA contract：`1 passed, 1 skipped`。probe/doctor 返回 `available:false`、
  `write_verified:false`、`operations:[]`、`reason:lceda_pro_not_found`，没有 executable、
  profile、version 或 executable digest。
- 隔离 Alembic SQLite 往返 `upgrade head -> downgrade base -> upgrade head` exit 0，最终
  `0009_pcb_candidates (head)`。它没有使用默认数据库；迁移往返必须以编程方式覆盖
  `sqlalchemy.url` 指向隔离数据库，绝不对默认 `alembic.ini` URL 执行 downgrade。

### Fixture、gate 与发布边界

- Focused gate audit 的四个测试均通过：完整 candidate evidence 与幂等 G3、完整 release
  manifest、匹配且幂等的 G4，以及 API/CLI `boardir_only` 可见性。
- 冻结 fixture digest：BoardIR
  `sha256:888129a2afd5261c5cb5f308ec1b7346eb07498f8b592a268aa98072a59910d5`；rulepack
  `sha256:5a1c8217ce2237e7186d5415a83f33b145082af548d7a18b76108f90c43cf190`；正向 fixture
  capability Artifact
  `sha256:c247771b86f304d90cd08d612e9a90aeb8781419df05469ed7d8f73cc2f1f628`；board profile
  `stm32-environment-controller-2l-v1`。
- 一次隔离正向 fixture run 产生 `g3_approved -> ready_for_g4 -> released`，candidate digest
  `sha256:776387090fff96dd62930a75a670e40e28eeae4eb16e401259df12bf1c4e9487`，release manifest
  digest `sha256:7de577a3cea7b4e845f0e8938b6180f55cfdf6e8b6269ca43b1ea3d8ba02a58e`。这些结果均为
  fixture-only，绝非真实 LCEDA proof。
- 隔离真实 CLI audit 返回 queued `boardir_only` candidate，并暴露 authority、base snapshot、
  BoardIR、rulepack、capability 和 operations digests；默认数据库及 authoritative project
  未改变，故不记录随机 candidate/project/task IDs。
- 本次 release audit 修复了损坏的 `pcb.export_release` task 引用，使其映射为稳定的终态
  task error 而非 `UNHANDLED_TASK_ERROR`；回归在 `tests/integration/test_pcb_release.py`。

未进行人工硬件/制造复审，也没有官方 LCEDA bridge verification。因此尽管实现和正向
fixture path 已完成，release-capable LCEDA pipeline 仍不完整；不得声称部署、硬件验证或
真实 LCEDA release readiness。

## 1. 目标

为 PCBFlow 增加以嘉立创 EDA 专业版为权威工程格式的低压两层控制板
自动化能力。首个端到端参考工程是基于 STM32F103C8T6 的智能环境监测
与负载控制主板。系统必须生成可审查的 PCB 候选，而不是直接覆盖权威
工程；候选只有在真实工具检查、规则检查和人工 G3 批准后才能被接受。

平台同时保持 KiCad 10 并行适配。KiCad 与嘉立创专业版不会对同一工程
形成双主写入，也不会自动合并彼此的原生工程文件。

## 2. 决策和范围

### 2.1 工程权威性

每个 PCBFlow 项目新增不可变的工程种类和适配器 profile：

| 项目字段 | 参考板值 | 含义 |
| --- | --- | --- |
| eda_kind | lceda_pro | 嘉立创专业版是该项目的权威工程 |
| eda_profile_id | lceda-pro-v1 | 锁定已验证的适配器契约 |
| board_profile_id | stm32-environment-controller-2l-v1 | 锁定板框、层叠、网类和约束 |
| rulepack_digest | sha256:... | 锁定制造和设计规则包 |

既有 eda_kind 为 kicad 的项目保持原有行为。跨 EDA 的内容只能进入
隔离交换区，生成语义差异和变更提案；任何一方都不能静默覆盖另一方。

### 2.2 V1 支持边界

V1 仅支持以下范围：

- 两层、默认 1 oz 铜厚、约 100 mm x 80 mm 的低压控制板。
- STM32F103C8T6、Type-C 5 V 输入、3.3 V LDO、I2C、ADC、UART、
  SWD、ESP-01S、OLED、按键、蜂鸣器、状态 LED。
- 两路 5 V 继电器线圈驱动，继电器触点最大 24 V DC、3 A。
- 两路低边 N 沟道 MOS 输出，每路最大 12 V 或 24 V DC、2 A。
- 规则驱动的元件摆放、受限网络集自动布线、GND 铜皮和制造输出。
- 嘉立创专业版 DRC、PCBFlow 规则检查、Gerber、钻孔、BOM、CPL 和
  装配资料的候选验证。

以下内容不在 V1 自动接受范围：

- 220 VAC 或其他市电、高压隔离、认证级安规和高压浪涌设计。
- 射频天线匹配、阻抗控制、DDR、PCIe、千兆链路或任意高速差分对。
- 四层及以上层叠、柔性板、刚挠板、盲埋孔、金手指和特殊工艺。
- 未公开、未版本化的嘉立创专有工程归档的直接写入。
- 任意未受约束 PCB 的一次性全板自动布线。

市电或高压控制必须通过独立的 high_voltage_profile，包含明确的爬电
距离、间隙、保险、隔离槽、端子、外壳和人工安规审查规则，不能复用
本规格的低压 profile。

## 3. 参考板约束

### 3.1 功能分区

参考板至少有以下不可重叠的功能区域：

| 区域 | 固定或优先对象 | 主要约束 |
| --- | --- | --- |
| 电源入口区 | Type-C、保险、TVS、LDO | 靠近板边，输入和稳压回路最小 |
| 数字核心区 | STM32、晶振、复位、SWD、去耦 | MCU 居中；晶振和去耦与相关引脚相邻 |
| 低噪声区 | ADC、I2C、传感器接口 | 远离继电器、MOS、WiFi 和负载电流回路 |
| 无线区 | ESP-01S | 天线朝向板边；定义禁布和禁铜区域 |
| 功率输出区 | 继电器、MOS、续流二极管、负载端子 | 靠近板边；与低噪声区保持隔离 |
| 人机与调试区 | OLED、按键、LED、蜂鸣器、UART、SWD | 易操作、易观察、易插拔 |

板框、安装孔、连接器边缘位置、ESP 天线禁区和工程师显式锁定的对象是
硬约束。其余器件只能在各自允许区域内优化。

### 3.2 网类和规则

规则包定义所有数值，不把制造默认值散落到算法代码中。参考 rulepack
至少包含：

| 网类 | 典型网络 | 规则方向 |
| --- | --- | --- |
| signal | GPIO、UART、I2C | 默认最小线宽和间距；允许两层和受控过孔 |
| quiet_signal | ADC、晶振 | 最短路径、隔离功率区、限制过孔和邻近噪声源 |
| logic_power | 3V3、5V | 根据电流、铜厚和温升计算最小宽度 |
| load_power | 继电器触点、MOS 负载 | 专用宽线或铜皮；限制过孔和回路面积 |
| ground | GND | 顶底层连续参考面、受控热焊盘和地过孔缝合 |

V1 的参考规则采用 8 mil 普通信号、20 mil 逻辑电源和至少 80 mil 的
3 A 负载铜线作为保守 fixture 值。实际生产候选必须锁定嘉立创规则包的
摘要，并按板厚、铜厚、温升和工艺能力重新计算。

铜皮规则也必须冻结在 rule pack 的 `copper` 对象中，包含板边净距、地过孔
间距与上限、热焊盘 spokes、热间隙和最小孤岛面积。由于 V1 `CopperZone`
目前只表达 `RectUm` bounds，铜皮规划器以确定性安全矩形 tile 集合表示
keepout 扣除结果；它不得伪造未在 BoardIR 中定义的 polygon 布尔几何。

### 3.3 明确禁区

BoardIR 必须表达下列区域，所有算法和适配器都必须强制遵守：

- ESP-01S 天线区：禁止走线、铺铜、过孔和金属器件。
- 晶振近场：仅允许相关晶振和负载电容。
- ADC 和 I2C 安静区：禁止负载电流铜线、开关节点和继电器邻近路径。
- 连接器、端子、安装孔、丝印和 courtyard 的机械 keepout。
- 工程师锁定的放置、走线、过孔和铜皮对象。

## 4. 适配器与能力检测

### 4.1 适配器边界

新增 PCB EDA 端口。其职责是将工具相关工程转换为 BoardIR 快照，并在
隔离副本中执行已经验证的受控操作。

~~~python
class PcbEdaAdapter(Protocol):
    def probe(self) -> EdaCapability: ...
    def load_snapshot(self, project_dir: Path) -> BoardSnapshot: ...
    def create_candidate(self, source_dir: Path, destination_dir: Path) -> CandidateWorkspace: ...
    def apply_operations(
        self,
        candidate: CandidateWorkspace,
        operations: tuple[BoardOperation, ...],
        expected_snapshot: BoardSnapshot,
    ) -> BoardSnapshot: ...
    def run_drc(self, candidate: CandidateWorkspace) -> tuple[ValidationReport, ...]: ...
    def export_release(
        self,
        candidate: CandidateWorkspace,
        rulepack: ManufacturingRulePack,
    ) -> ReleaseArtifacts: ...
~~~

LcedaProAdapter 和 KicadPcbAdapter 均实现该端口，但一个项目只绑定其中
一个写入适配器。BoardIR fixture 可以被两个适配器读取，用于算法和
语义差异回归；这不是原生工程文件的双向同步。

### 4.2 嘉立创专业版能力门

在任何写入实现前，必须完成 lceda-pro-v1 capability gate：

1. 记录已安装嘉立创专业版的版本、二进制摘要、操作系统、可用 CLI、
   插件、导入导出格式和 DRC/制造导出入口。
2. 冻结最小原生工程 fixture，包括板框、两层 stackup、一个封装、一个
   网络、一个禁区、一个铜皮和一个安装孔。
3. 对每个候选自动化入口执行创建、保存、重新打开和字节/语义回读测试。
4. 只有公开支持且可稳定回读的 API、命令或导入格式可以成为写入后端。
5. 若当前版本没有稳定写入入口，适配器只提供读取、DRC、导出和隔离交换；
   不得通过字符串替换或猜测专有归档格式实现写入。

EdaCapability 必须包含版本、可执行文件摘要、支持的读写操作、DRC 与
导出能力、失败原因和 profile revision。项目候选冻结 capability 摘要，
工具变化会使旧验证结果失效并要求重新执行 G3。

## 5. BoardIR 与候选操作

BoardIR 是一个最小、版本化的领域模型，不试图复制任一 EDA 的全部文件
格式。V1 至少包含：

- BoardOutline、LayerStack、MountingHole、Keepout 和 CopperKeepout。
- Footprint、Pad、Net、NetClass、ComponentPlacement 和 PlacementLock。
- RouteSegment、Via、RouteLock、CopperZone、ThermalPolicy 和 StitchingPolicy。
- RuleViolation、BoardFinding、BoardSnapshot 和 SemanticBoardDiff。
- PlaceFootprints、RouteNets、CreateCopperZones、AddGroundStitching 和
  LockBoardObjects 五类 BoardOperation。

每个对象有稳定、来源可追溯的标识。未知工具对象必须保留为 opaque node，
或在适配器能力不足时使写入失败；不得丢弃后再重新序列化。

所有 BoardOperation 都携带项目、基线 revision、风险、规则包摘要、目标
对象、幂等键和预期快照摘要。候选写入前后均生成 BoardSnapshot；任何
不属于操作声明的变化都使候选失败。

## 6. 优化与路由策略

### 6.1 摆放

摆放分两层执行：

1. 硬约束求解：固定板框、安装孔、边缘连接器、ESP 天线、功率区和
   禁区；任何越界、重叠、courtyard 冲突或锁定对象移动立即失败。
2. 多目标优化：在合法区域内以多起点模拟退火优化可移动器件。评分函数
   同时计入飞线长度、关键网长度、5 V/3V3/负载回路面积、ADC 噪声邻近、
   热集中、连接器可达性、返修空间和对称性。

离散区域分配可使用 OR-Tools CP-SAT；连续微调使用确定性种子、多起点
模拟退火。每个种子、目标函数版本、权重、初始快照和最终得分均写入证据。
算法不能移动 PlacementLock 对象，也不能以总线最短为由违反区域规则。

### 6.2 布线

路由器不是一次性处理整板：

1. 先布置 Type-C 到保护和稳压的电源回路、3V3 去耦、晶振、SWD、I2C、
   ADC 和工程师指定的关键网络。
2. 逻辑信号使用 45 度几何约束的 A-star 或 Lee 搜索，代价函数包含长度、
   过孔、靠近禁区、跨越低噪声区和损坏连续 GND 回流面的惩罚。
3. 普通信号使用 Pathfinder 式拥塞协商、rip-up 和 retry；每轮只处理
   明确的 RouteNets 网络集合，并在轮次后运行几何 DRC。
4. 逻辑电源和负载电源不使用普通细线搜索。它们由拓扑规划生成宽线或
   铜皮，并针对电流、温升、过孔数和回流路径单独检查。
5. 手工 RouteLock 绝不被 rip-up；无法在约束内布通时产生 finding 和
   需要人工处理的候选，而不是放宽规则或伪造成功。

V1 不实现差分对长度匹配、阻抗控制或任意角度自由布线。

### 6.3 铺铜

铜皮在关键走线完成后执行：

- 顶层和底层 GND zone 由 BoardIR 明确声明，而非隐式全板填充。
- 天线禁区、板边、安装孔、敏感模拟区和工程师指定避让对象必须生效。
- 连接焊盘使用锁定的 ThermalPolicy；孤岛、死铜和未连接 copper island
  必须报告。
- StitchingPolicy 只允许在连续 GND 区和规则许可的位置插入地过孔。
- 铜皮重填后必须重新运行连通性检查、几何 DRC、回流路径检查和原生工具
  DRC。

## 7. 工作流、审批与恢复

~~~text
冻结原理图和规则包
  -> PCB_SETUP
  -> PCB_PLACEMENT
  -> PCB_ROUTING
  -> PCB_VERIFY
  -> G3_PCB_PENDING
  -> MANUFACTURING_PRECHECK
  -> RELEASE_CANDIDATE
  -> G4_RELEASE_PENDING
~~~

PCBFlow 为每次自动操作创建隔离工作目录、任务尝试、输入摘要、工具能力
摘要、算法证据、原生 DRC 报告、语义差异和候选摘要。取消、崩溃、超时、
规则不满足或适配器不支持都只能终止候选，不能修改嘉立创专业版权威工程。

G3 必须由人工批准，至少要求：

- 原生嘉立创专业版 DRC 没有阻断错误。
- 未连接网络为零，未解释的豁免为零。
- 天线、低噪声、功率和机械 keepout 零违规。
- 规则包、工具 capability、BoardIR 快照和候选摘要全部一致。
- 布局、功率回路、关键接口方向和铜皮策略有可读证据。

G4 必须由人工批准，至少要求：

- Gerber、钻孔、BOM、CPL、装配图和发布清单来自同一冻结候选。
- 制造规则包摘要与目标嘉立创工艺参数一致。
- BOM 与 CPL 的参考标号集合一致，DNP 和手焊件明确标记。
- 文件哈希、工具版本、规则包和审批记录可离线验证。

## 8. 验证策略

### 8.1 Fixture

新增三组固定输入：

1. 最小嘉立创专业版工程，用于 capability 和安全读写契约。
2. STM32 参考板的阶段 fixture，覆盖电源、MCU、ESP 天线、ADC、继电器、
   MOS、端子和 GND zone。
3. 故障 fixture，覆盖未知对象、禁止写入入口、天线区走线、过窄负载线、
   未连接网络、铜皮孤岛、DRC 失败、工具崩溃和候选取消。

### 8.2 测试层次

- 单元测试：BoardIR 校验、规则包、评分函数、CP-SAT 可行性、路由代价、
  copper policy、状态转换和拒绝路径。
- 性质测试：随机放置和网络集不会突破 keepout、锁定对象或净空规则。
- 适配器契约：导入、快照、隔离候选、受控操作、保存回读、DRC、导出。
- 真实工具契约：固定嘉立创专业版 profile 上执行真实 DRC、Gerber、BOM
  和 CPL 导出；不具备稳定自动化能力时明确跳过写入契约而非伪造通过。
- 集成测试：任务、fencing、取消、恢复、审批、工件内容寻址和 G3/G4。
- 端到端测试：参考板从冻结原理图到完整制造候选，要求输出可复验。

### 8.3 V1 退出标准

1. 嘉立创专业版 capability gate 已通过，profile 与 fixture 固定。
2. STM32 参考板可生成可审阅的布局候选；固定对象、禁区、courtyard 和
   所有锁定对象无违规。
3. 受限网络集自动布通且没有未连接网络；无法布通的网络以明确 finding
   结束，不放宽规则。
4. GND 铜皮和地过孔策略通过连通性与规则检查，ESP 天线避铜零违规。
5. 真实工具 DRC 零阻断错误，PCBFlow 规则和制造预检零阻断错误。
6. 输出 Gerber、钻孔、BOM、CPL、装配图和哈希清单；所有文件可从冻结
   候选重建。
7. KiCad 适配器在同一 BoardIR fixture 上通过语义和算法回归，不要求
   原生工程的自动双向合并。

## 9. 分阶段交付

| 阶段 | 交付 | 退出条件 |
| --- | --- | --- |
| P0 | 嘉立创能力探测和 fixture | 无稳定写入入口时安全降级为只读，不进入写入阶段 |
| P1 | BoardIR、规则包和 PCB 初始化 | 可从冻结项目生成板框、层叠、禁区和网类快照 |
| P2 | 约束摆放 | 参考板生成可审阅布局且硬约束零违规 |
| P3 | 受限自动布线 | 关键与普通 V1 网络按规则布通或产生明确 finding |
| P4 | 铜皮和 G3 | GND zone、热焊盘、地过孔和真实 DRC 通过 |
| P5 | 制造和 G4 | 可重现的嘉立创候选制造包与审批证据 |
| P6 | KiCad 并行适配 | 同一 BoardIR fixture 的算法与语义回归稳定 |

**2026-08-08 P1 候选实现状态。** 候选生命周期的持久化切片已完成：项目级幂等键
冻结 authority、base revision/snapshot、BoardIR、规则包、capability、操作和算法
证据；候选与内部任务原子写入，取消在任务事务中镜像，SQL 条件 fence 和版本补偿禁止
失效 Worker 发布状态。候选 capability digest 必须等于 gate 已验证的 artifact digest，
`ready_for_g3` 必须匹配冻结 BoardIR/operation digest。该实现仅产生且公开为
`boardir_only` 候选。P1 的原生嘉立创专业版工程生成、真实 DRC 和发布交付仍取决于
P0 官方 bridge capability gate，当前不能视为已完成。

## 10. 风险与决策

| 风险 | 处理 |
| --- | --- |
| 嘉立创专业版缺少稳定写入入口 | 在 capability gate 停止写入；不修改专有归档，保留读取、DRC、导出和交换能力 |
| 自动路由质量不足 | 限定 V1 网络和板类，使用硬规则、finding、G3 人审和锁线协同 |
| 双 EDA 漂移 | 每项目单一权威工程；交换只能生成隔离变更提案 |
| 制造规则变化 | 规则包版本化，候选锁定摘要，规则升级强制重新跑 G3/G4 |
| 低压规则误用于市电 | 以 board_profile 和 high_voltage_profile 进行类型隔离，缺少高压 profile 时拒绝候选 |
| 算法不可复现 | 固定随机种子、目标函数版本、规则包、工具 capability 和输入快照 |

## 11. 与既有设计的关系

该规格保留 PCBFlow 的隔离候选、内容寻址证据、幂等键、fencing、任务取消、
G3/G4 审批和制造规则包不变量。它将总体设计中“KiCad 是唯一主工程”的
默认策略扩展为项目级单一权威工程策略：既有 KiCad 项目继续以 KiCad 为
权威；明确选择 lceda_pro 的新项目以嘉立创专业版为权威。两者仍禁止双主
写入和自动双向合并。

此变化只在 P0 的嘉立创专业版 capability gate 通过后才允许进入实际写入
实施。若 gate 不通过，PCBFlow 不会把未验证的文件操作包装成自动化能力。
