# PCBFlow 项目整理报告

## 2026-09-29 文档整理与候选 store 边缘覆盖

- README 全面重排：项目定位移到开头，两段日志式记录（2026-09-24、
  2026-08-10）移出（本页已有对应条目），环境变量合并为与 `config.py` 逐一
  对应的单张表（补齐此前遗漏的 5 个 `PCBFLOW_MAX_KICAD_*`/`PCBFLOW_TASK_RETRY_*`
  变量），原先散落的英文章节统一为中文，新增 MCP 服务器章节。
- QUICK_REFERENCE 去除与 README 重复的环境变量与工作流内容，保留故障排查、
  部署与性能调优。
- 删除文件下半部分 2026-08-03 的历史自动质量报告：其中"316 个测试""无需
  重构"等结论早已失效，与顶部按日期的增量记录并存容易误导。
- 新增 `tests/integration/test_pcb_candidate_store_edges.py`（15 个测试）：
  覆盖候选 store 的公开输入解析（无 base revision、无 LCEDA authority、
  BoardIR digest 缺失、capability digest 从证据解析）、创建路径守卫
  （非元组操作、未验证 output_kind、算法证据内 seed 校验、前置与事务内
  stale 双防线）以及 IntegrityError 竞态恢复（隐藏预查询后的事务内重放、
  强制候选行 flush 失败后的重放与冲突映射）。`pcb_candidate_store` 覆盖率
  82% → 87%（剩余缺口集中在发布状态迁移与取消镜像分支）。
- 已知怪癖记录：pytest-cov 子模块覆盖目标（如 `--cov=pcbflow.approvals`）
  在 coverage 7.15.2 下于收集阶段触发 numpy
  "cannot load module more than once per process"；完整包目标
  `--cov=pcbflow` 不受影响。

## 2026-09-28 MCP 契约与覆盖硬化

- 修复两个 P0：所有 MCP 工具调用因 `json.dumps(CallToolResult)` 抛
  `TypeError`（处理器改为返回 payload dict，由工具函数序列化）；工具包装器
  把省略的参数传成显式 `None`，使文档默认值永远不生效（改为真值判断）。
- 证据关键模块覆盖率：`pcb_candidate_codec` 58% → 100%，`mcp_server`
  66% → 96%，`worker_service` 75% → 99%；全量覆盖率门禁从 88 提到 90
  （实测 91.72%）。计划与验证数据见
  `docs/superpowers/plans/2026-09-28-mcp-contract-and-coverage-hardening.md`
  与 `docs/OPTIMIZATION_GUIDE.md`。

## 2026-09-25 CI 修复与 mypy 全量清零

- 修复 CI 首跑失败的两个根因：`from tests.component_fixtures import ...` 在
  pytest 命令行入口下因 cwd 不在 sys.path 而收集失败（pyproject 增加
  `pythonpath = ["."]`）；typer 在 GITHUB_ACTIONS 下强制 rich 彩色渲染导致
  `--once` 等字面量被 ANSI 转义码拆断（测试改为断言去样式文本）。
- mypy 215 个历史 findings 全部清零（70 个源文件），无新增 type: ignore 除
  fcntl/POSIX 分支与 pydantic 元类桩缺口两处定向标注；ortools 求解器调用从
  已移除的驼峰 API（NewIntVar/Add/Minimize）迁移到官方 snake_case API，
  属正确性修复。mypy 现为 CI lint job 的强制门禁。

## 2026-09-24 仓库整理与工具链补全

- 修复 d57f3df 提交引入的导入崩溃：pcb_candidate_store/execution 从不存在的
  位置导入 ArtifactRow/ProjectRow/TaskRow/TaskCancelledError/StaleLeaseError，
  且 pcb_candidate_validation 中存在无限自递归的 validate_candidate_digest。
  该批次提交后全量测试从未运行过。
- 完成 pcb_candidates.py（2155 行）向 pcb_candidate_validation / codec / store /
  execution 四个模块的逐字拆分，原模块保留为兼容 re-export 门面；测试
  monkeypatch 目标同步迁移到 pcbflow.pcb_candidate_store.utc_now。
- 引入 ruff（E9/F/I，行宽 200）、mypy（仅 informational，现存约 215 个历史
  findings）、pytest-timeout（每测试 600s）、pytest-xdist、pre-commit 和
  GitHub Actions CI；新增 requirements-lock.txt 固定已验证依赖版本。
- 行尾全面钉为 LF（KiCad 字节敏感 fixture 除外），.gitignore 补齐
  .hypothesis/.mypy_cache/.ruff_cache。
- 历史遗留的 DEVELOPMENT_SUMMARY.md 确认为未实现的愿景文档，移入 docs/ 并
  加注声明。

## 2026-08-10 PCB 自动化实测完成记录

BoardIR/算法、候选、G3/G4 正向 fixture、API/CLI、KiCad parity 和 release packaging 的
实现已完成。全量 gate：Python 3.13.9、pytest 8.4.2，`832 passed, 1 skipped`，589.92 s，
总覆盖率 `90.00%`（12611 statements，1261 misses），`--cov-fail-under=90` exit 0。

真实 LCEDA 发布能力未完成：官方写桥与原生 DRC 未验证，probe/doctor 返回
`available:false`、`write_verified:false`、`operations:[]`、`reason:lceda_pro_not_found`；
没有 executable、profile、version 或 executable digest。唯一全量 skip 为
`tests/contract/test_lceda_pro_adapter.py:16`，原因同上。未进行人工硬件/制造复审，也未
验证官方 bridge；因此 fixture 发布成功不能标示为真实 LCEDA release readiness。

## 2026-08-08 PCB 候选复审修复

- 任务取消现在在同一数据库事务中镜像关联候选；即使候选任务仍在 queued/retry_wait，
  候选也不会遗留为非终态。
- 候选状态写入同时以 task fence 作为 SQL 条件，损坏的 task↔candidate 关联以
  `PCB_CANDIDATE_NOT_FOUND` 终止；进入 `ready_for_g3` 必须回传与冻结 BoardIR 和
  operation digest 匹配的结果。
- capability gate 返回已验证 artifact digest，候选拒绝冻结与该证据不一致的 digest。
  REST/CLI 对 KiCad authority 稳定返回 `PCB_CAPABILITY_GATE_BLOCKED`，并共用严格的
  seed、net id 和 digest 校验。
- 当前公开候选统一标注 `boardir_only`；状态或调用方 JSON 不会把它提升为 native/release。
- 本次新鲜验证：候选/迁移/任务 `64 passed`；LCEDA capability gate `16 passed`；
  API/CLI E2E `44 passed`；`compileall` 与 `git diff --check` 退出码均为 0。

## 2026-08-07 PCB 候选增量状态

本节是对 2026-08-03 Phase 5A 历史整理报告的增量记录。

- 已实现 `pcb_candidates` 持久化状态机、项目级候选幂等键、冻结输入比较和
  `pcb.generate_candidate` Worker 入口。
- 候选与内部任务同事务写入；候选写入失败不会留下可领取的孤立任务。
- Worker 按项目和候选幂等键查找候选。取消竞争会镜像为 `cancelled`，状态写入后
  检测到租约失效会用 optimistic version 恢复旧状态；损坏任务返回
  `PCB_CANDIDATE_NOT_FOUND`。
- REST 和 CLI 对不存在候选返回 `PCB_CANDIDATE_NOT_FOUND`；同一候选幂等键下
  改变 BoardIR、capability 或 seed 返回 `IDEMPOTENCY_CONFLICT`。
- 本轮验证：候选/迁移/任务 `58 passed`；BoardIR、LCEDA gate 和相关契约
  `68 passed, 1 skipped`；API/CLI E2E `41 passed`；unit `371 passed`；全部
  contract `2 passed, 3 skipped`；候选模块定向覆盖率 `92%`；隔离 SQLite 上的
  Alembic `upgrade -> downgrade base -> upgrade` 通过。
- 原生 LCEDA Pro 写入仍未通过官方 bridge capability gate，当前 PCB 候选明确为
  `boardir_only`，不宣称原生写入、DRC 或制造发布能力已经完成。

> 2026-08-03 及更早的 Phase 5A 自动质量报告已移除：其测试数量、代码行数与
> "无需整理"等结论仅反映当时状态，保留会与上方的日期增量记录冲突。
