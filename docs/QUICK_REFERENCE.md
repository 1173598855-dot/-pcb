# PCBFlow 快速参考指南

## 快速开始

### 安装和设置
```powershell
# 克隆项目
git clone <repository-url>
cd pcbflow

# 创建虚拟环境
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 安装依赖
pip install -e .

# 初始化数据库
pcbflow project list  # 自动运行迁移
```

### 基本工作流

#### 1. 注册项目
```powershell
pcbflow project register <项目路径> --idempotency-key "reg-$(Get-Date -Format yyyyMMdd)"
```

#### 2. 验证设计
```powershell
pcbflow validate <项目ID> --idempotency-key "val-$(Get-Date -Format yyyyMMdd-HHmmss)"
```

#### 3. 运行 Worker
```powershell
# 常驻模式（生产）
pcbflow worker --run

# 单任务模式（开发）
pcbflow worker --once
```

#### 4. 查看结果
```powershell
# 查看任务状态
pcbflow task show <任务ID> --json

# 查看发现的问题
pcbflow findings <项目ID> --json

# 查看证据
pcbflow evidence <项目ID> --json
```

## 常用命令

### 项目管理
```powershell
# 列出所有项目
pcbflow project list --json

# 采纳项目（版本控制）
pcbflow project <项目ID> adopt --idempotency-key "adopt-key"

# 创建快照
pcbflow project <项目ID> snapshot
```

### Worker 管理
```powershell
# 检查 Worker 健康状态
pcbflow worker health --json

# 配置 Worker
$env:PCBFLOW_WORKER_SLOTS = "3"              # 并发槽位
$env:PCBFLOW_WORKER_POLL_SECONDS = "5"       # 轮询间隔
$env:PCBFLOW_WORKER_HEARTBEAT_SECONDS = "30" # 租约续期

# 启动 Worker
pcbflow worker --run
```

### API 服务
```powershell
# 启动本地服务器
pcbflow serve --host 127.0.0.1 --port 8765

# 启动远程模式（需要认证）
$env:PCBFLOW_REMOTE_MODE = "true"
$env:PCBFLOW_API_TOKEN = "<your-secret-token>"
pcbflow serve --host 127.0.0.1 --port 8765
```

## 环境变量配置

### 核心配置
| 变量 | 默认值 | 说明 |
|------|--------|------|
| `PCBFLOW_DATA_DIR` | `./.pcbflow-data` | 数据目录 |
| `PCBFLOW_DATABASE_URL` | `sqlite:///<DATA_DIR>/pcbflow.db` | 数据库 URL |
| `PCBFLOW_KICAD_CLI` | `kicad-cli` | KiCad CLI 路径 |

### Worker 配置
| 变量 | 默认值 | 说明 |
|------|--------|------|
| `PCBFLOW_WORKER_SLOTS` | `1` | 并发任务槽位数 |
| `PCBFLOW_WORKER_POLL_SECONDS` | `5` | 空闲轮询间隔（秒）|
| `PCBFLOW_WORKER_POLL_MAX_SECONDS` | `60` | 最大退避间隔（秒）|
| `PCBFLOW_WORKER_HEARTBEAT_SECONDS` | `30` | 租约续期间隔（秒）|
| `PCBFLOW_WORKER_SHUTDOWN_TIMEOUT_SECONDS` | `300` | 优雅关闭超时（秒）|
| `PCBFLOW_WORKER_ID` | 自动生成 | Worker 唯一标识符 |

### 任务配置
| 变量 | 默认值 | 说明 |
|------|--------|------|
| `PCBFLOW_TASK_LEASE_SECONDS` | `180` | 任务租约时长（秒）|
| `PCBFLOW_PROCESS_TIMEOUT_SECONDS` | `120` | 进程超时时间（秒）|
| `PCBFLOW_MAX_PROCESS_OUTPUT_BYTES` | `10485760` | 最大输出大小（10MB）|

### 资源限制
| 变量 | 默认值 | 说明 |
|------|--------|------|
| `PCBFLOW_MAX_PROJECT_FILES` | `1000` | 项目最大文件数 |
| `PCBFLOW_MAX_PROJECT_BYTES` | `104857600` | 项目最大大小（100MB）|
| `PCBFLOW_MAX_API_BODY_BYTES` | `10485760` | API 请求最大大小（10MB）|

### 远程模式
| 变量 | 默认值 | 说明 |
|------|--------|------|
| `PCBFLOW_REMOTE_MODE` | `false` | 启用远程模式 |
| `PCBFLOW_API_TOKEN` | - | API 认证令牌 |
| `PCBFLOW_API_ACTOR_ID` | `service` | 服务 Actor ID |

## 开发和测试

### 运行测试
```powershell
# 运行所有测试
pytest tests/unit/ -v

# 运行特定测试
pytest tests/unit/test_worker_service.py -v

# 运行测试并生成覆盖率报告
pytest tests/unit/ --cov=src/pcbflow --cov-report=html
```

### 数据库管理
```powershell
# 创建新的迁移
alembic revision --autogenerate -m "描述"

# 应用迁移
alembic upgrade head

# 回滚迁移
alembic downgrade -1
```

### 调试技巧
```powershell
# 启用详细日志
$env:PYTHONPATH = "src"
python -m pcbflow worker --run

# 使用 JSON 输出便于解析
pcbflow task show <任务ID> --json | ConvertFrom-Json

# 检查数据库
sqlite3 .pcbflow-data/pcbflow.db
```

## 故障排查

### Worker 无法启动
1. 检查数据库连接：`pcbflow project list`
2. 检查 KiCad 路径：`$env:PCBFLOW_KICAD_CLI`
3. 查看日志输出

### 任务一直处于 LEASED 状态
- 原因：Worker 崩溃但任务未释放
- 解决：等待租约过期（默认 180 秒），新 Worker 会自动接管

### 并发任务冲突
- 确保 `PCBFLOW_WORKER_SLOTS` 设置合理
- 检查系统资源（CPU、内存）
- 查看 Worker 健康状态：`pcbflow worker health`

### 数据库锁定错误
- SQLite WAL 模式已启用
- 增加 busy timeout（默认 5000ms）
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
```powershell
# 备份数据库
Copy-Item .pcbflow-data/pcbflow.db "backup/pcbflow-$(Get-Date -Format yyyyMMdd).db"

# 备份 artifacts
Copy-Item -Recurse .pcbflow-data/artifacts "backup/artifacts-$(Get-Date -Format yyyyMMdd)"
```

## 性能调优

### Worker 配置
- **单核心 CPU**: `PCBFLOW_WORKER_SLOTS=1`
- **多核心 CPU**: `PCBFLOW_WORKER_SLOTS=<CPU核心数-1>`
- **I/O 密集型**: 可以设置为 CPU 核心数的 1.5-2 倍

### 数据库优化
- 定期 VACUUM：`sqlite3 pcbflow.db "VACUUM;"`
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

# 查看版本信息
pcbflow --version
```

---

**最后更新**: 2026-08-03  
**版本**: Phase 5A 完成
