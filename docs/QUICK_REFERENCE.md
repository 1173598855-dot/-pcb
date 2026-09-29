# PCBFlow 快速参考指南

环境变量、CLI 工作流与 REST 端点的权威清单在根目录
[README.md](../README.md)；本页只保留故障排查、部署与性能调优速查。

## 常用命令速查

```powershell
# 项目
pcbflow project add <项目路径> --name "<名称>" --idempotency-key "<key>" --json
pcbflow project list --json
pcbflow project adopt <项目ID> --idempotency-key "<key>" --json

# 验证与 Worker
pcbflow validate <项目ID> --idempotency-key "<key>" --json
pcbflow worker --run      # 常驻模式（生产）
pcbflow worker --once     # 单任务模式（开发）
pcbflow worker health --json

# 结果
pcbflow task show <任务ID> --json
pcbflow findings <项目ID> --json
pcbflow evidence <项目ID> --json

# 服务
pcbflow serve --host 127.0.0.1 --port 8765
pcbflow doctor --json
```

## 测试与数据库

```powershell
# 运行所有测试
.\.venv\Scripts\python.exe -m pytest -q

# 运行特定测试
.\.venv\Scripts\python.exe -m pytest tests/unit/test_worker_service.py -v

# 完整覆盖率门禁（只支持完整包目标；子模块目标见 README 已知怪癖）
.\.venv\Scripts\python.exe -m pytest --cov=pcbflow --cov-report=term-missing --cov-fail-under=90

# 创建新的迁移
.\.venv\Scripts\python.exe -m alembic -c alembic.ini revision --autogenerate -m "描述"

# 应用迁移
.\.venv\Scripts\python.exe -m alembic -c alembic.ini upgrade head

# 在隔离临时数据库验证升级/降级往返；不要降级默认数据库
.\.venv\Scripts\python.exe -m pytest tests/integration/test_migrations.py -q
```

## 故障排查

### Worker 无法启动
1. 检查数据库连接：`pcbflow project list`
2. 检查 KiCad 路径：`$env:PCBFLOW_KICAD_CLI`
3. 查看日志输出

### 任务一直处于 LEASED 状态
- 原因：Worker 崩溃但任务未释放
- 解决：等待租约过期（默认 180 秒），新 Worker 会用新的 fencing token 自动接管

### 并发任务冲突
- 确保 `PCBFLOW_WORKER_SLOTS` 设置合理（1-10）
- 检查系统资源（CPU、内存）
- 查看 Worker 健康状态：`pcbflow worker health`

### 数据库锁定错误
- SQLite WAL 模式已启用
- 增加 busy timeout（默认 5000 ms）
- 考虑降低并发槽位数

## 生产部署建议

### 系统服务配置（Windows）
```powershell
# 创建任务计划程序任务
$action = New-ScheduledTaskAction -Execute "C:\path\to\.venv\Scripts\pcbflow.exe" -Argument "worker --run"
$trigger = New-ScheduledTaskTrigger -AtStartup
Register-ScheduledTask -TaskName "PCBFlow Worker" -Action $action -Trigger $trigger
```

### 监控和告警
- 定期运行 `pcbflow worker health` 检查状态
- 监控数据库大小和增长率
- 设置任务失败率告警阈值

### 备份策略
- 使用 SQLite 在线备份 API 创建一致的数据库快照；不要在 WAL 模式下只复制
  `pcbflow.db`。
- 在同一恢复点备份 `artifacts/`，并验证数据库中的 Artifact 摘要对应的对象存在。
- 恢复演练必须使用隔离数据目录，不能覆盖正在运行的默认数据目录。

## 性能调优

### Worker 配置
- **单核心 CPU**: `PCBFLOW_WORKER_SLOTS=1`
- **多核心 CPU**: `PCBFLOW_WORKER_SLOTS=<CPU核心数-1>`
- **I/O 密集型**: 可以设置为 CPU 核心数的 1.5-2 倍

### 数据库优化
- 仅在维护窗口且没有活动写入时对 `.pcbflow-data/pcbflow.db` 执行 `VACUUM`。
- 监控 WAL 文件大小
- 考虑定期归档旧数据

### 任务调度优化
- 调整轮询间隔平衡延迟和负载
- 使用退避策略减少空闲时的数据库访问
- 根据任务平均执行时间调整租约时长

## 更多资源

- **开发指南**: `docs/DEVELOPMENT_GUIDE.md`
- **优化指南**: `docs/OPTIMIZATION_GUIDE.md`
- **项目状态**: `docs/PROJECT_STATUS.md`
- **设计文档**: `docs/superpowers/specs/`
- **实现计划**: `docs/superpowers/plans/`

## 获取帮助

```powershell
# 查看命令帮助
pcbflow --help
pcbflow worker --help
pcbflow project --help
```

---

**最后更新**: 2026-09-29
**状态**: BoardIR-only PCB 工作流已实现；真实 LCEDA 发布仍受能力门阻断
