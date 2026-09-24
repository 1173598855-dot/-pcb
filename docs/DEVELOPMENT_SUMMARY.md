# PCBFlow 自动化开发平台
# PCB Automation Development Platform

## 1. 概述

PCBFlow 是一个本地化的 PCB 自动化开发平台，支持完整的 PCB 设计与发布流程，包括 BoardIR 模型管理、EDA 工具能力验证、G3/G4 版本控制、自动化工作流执行等功能。本平台通过 Web UI、CLI 和 MCP 协议对外提供服务，支持国产和国外 AI 助手集成，旨在提升 PCB 开发效率，降低技术门槛。

## 2. 架构设计

### 2.1 总体架构

```
┌───────────────────────────────────────────────────────────┐
│                  Web UI (React)                            │
├───────────────────────────────────────────────────────────┤
│  API Gateway (FastAPI)                                      │
├───────────────────────────────────────────────────────────┤
│  Core Services (Python)                                     │
│  ├─ pcbflow.pcb_workflow        - PCB 工作流管理          │
│  ├─ pcbflow.eda                 - EDA 能力管理            │
│  ├─ pcbflow.lceda_pro           - LCEDA Pro 接口           │
│  ├─ pcbflow.board.*            - Board 适配器            │
│  └─ pcbflow.process            - 子进程管理              │
├───────────────────────────────────────────────────────────┤
│  MCP Server (Python)                                       │
├───────────────────────────────────────────────────────────┤
│  Storage (SQLite/SQLAlchemy)                               │
│  ├─ projects.db                                              │
│  ├─ evidence.db                                              │
│  └─ artifacts.db                                            │
└───────────────────────────────────────────────────────────┘
```

### 2.2 模块职责

| 模块 | 技术栈 | 功能 |
|------|--------|------|
| **Web UI** | React + Ant Design + TypeScript | 可视化界面，项目管理、工作流编辑、日志查看等 |
| **API Gateway** | FastAPI + Python | HTTP API，管理 Web UI、CLI 和 MCP 通信 |
| **Core Services** | Python + FastAPI | 核心业务逻辑，PCB 流程管理、EDA 验证等 |
| **MCP Server** | Node.js/Python | 通过 MCP 协议暴露功能给AI助手 |
| **Storage** | SQLite/SQLAlchemy | 数据持久化，项目、证据、工件存储 |

### 2.3 API 设计

#### 2.3.1 REST API (FastAPI)

```http
GET /api/projects                    # 列出所有项目
POST /api/projects                  # 创建新项目
GET /api/projects/{id}              # 获取项目详情
PUT /api/projects/{id}/workflow      # 更新工作流
GET /api/projects/{id}/logs         # 获取运行日志

GET /api/projects/{id}/kic?brief   # 查询 KiCad 状态
POST /api/projects/{id}/validate     # 验证项目
POST /api/projects/{id}/run          # 执行工作流
GET /api/projects/{id}/c                 # 列表出候选版本
POST /api/projects/{id}/apply          # 应用候选版本
GET /api/projects/{id}/release        # 获取发布的工件
```

#### 2.3.2 MCP 协议

```json
{
  "protocolVersion": "1.0",
  "capabilities": {
    "projects": {"list": true, "create": true, "update": true},
    "workflows": {"execute": true, "view": true},
    "eda": {"query": true, "validate": true},
    "artifacts": {"export": true, "import": true}
  },
  "tools": [
    {
      "name": "register_project",
      "description": "注册本地 PCB 项目",
      "parameters": {"path": "string", "type": "string"}
    },
    {
      "name": "run_workflow",
      "description": "运行指定的 PCB 工作流",
      "parameters": {"projectId": "string", "workflow": "string"}
    }
  ]
}
```

## 3. Web UI 设计

### 3.1 主要页面

#### 3.1.1 仪表盘页面 (`/dashboard`)
- **功能**: 项目状态总览，快速操作
- **内容**:
  - 项目统计卡片
  - 最新日志
  - 快捷操作按钮
  - 系统状态监控

#### 3.1.2 项目列表页面 (`/projects`)
- **功能**: 项目管理
- **内容**:
  - 项目搜索与过滤
  - 批量操作
  - 新建项目向导

#### 3.1.3 项目详情页面 (`/projects/:id`)
- **功能**: 项目详细信息
- **内容**:
  - 项目元数据
  - 工作流管理
  - 候选版本列表
  - 发布历史

#### 3.1.4 工作流编辑页面 (`/projects/:id/workflow/:workflowId`)
- **功能**: 工作流配置与执行
- **内容**:
  - 图模式工作流编辑器 (React Flow)
  - 节点库 (验证、导出、导入等)
  - 实时日志输出
  - 执行控制 (开始、暂停、停止)

#### 3.1.5 日志页面 (`/logs`)
- **功能**: 运行日志管理
- **内容**:
  - 实时日志订阅
  - 日志检索与过滤
  - 日志下载

#### 3.1.6 设置页面 (`/settings`)
- **功能**: 系统配置
- **内容**:
  - KiCad 路径配置
  - LCEDA Pro 路径配置
  - 官方桥接配置
  - 数据库配置

### 3.2 UI 组件

#### 3.2.1 项目卡片组件
```tsx
interface ProjectCardProps {
  project: Project;
  onClick: (id: string) => void;
  onDelete: (id: string) => void;
}
```

#### 3.2.2 工作流编辑器组件
```tsx
interface WorkflowNode {
  id: string;
  type: 'validate' | 'export' | 'import';
  position: { x: number; y: number };
  data: { config: any; };
}
```

#### 3.2.3 日志查看组件
```tsx
interface LogViewerProps {
  projectId: string;
  logType: 'stdout' | 'stderr' | 'system';
  autoScroll: boolean;
}
```

## 4. AI 助手集成

### 4.1 MCP 服务器实现

#### 4.1.1 Python MCP 服务器
```python
from mcp.server import Server
from mcp.types import Tool, CallToolResult
from dataclasses import dataclass
from typing import Any
from pathlib import Path

app = Server("pcbflow")

# 支持国产/国外 AI 助手的多语言参数定义
@dataclass
class RegisterProjectArgs:
    path: str
    type: str  # "kicad", "lceda", "easyeda", "zhongan", "others"
    description: str | None = None

@dataclass
class RunWorkflowArgs:
    project_id: str
    workflow_name: str
    parameters: dict[str, Any] | None = None
    use_chinese_model: bool = False
    language: str = "zh-CN"

# 支持多模型的工具实现
def register_project(args: dict) -> dict:
    """注册本地 PCB 项目，支持国产/国外EDA"""
    return {
        "project_id": f"proj_{hash(args.get('path', '')) % 10000}",
        "status": "created",
        "supported_eda": args.get('type', 'kicad'),
        "ai_assisted": True,
        "notes": f"项目 {args.get('path')} 已注册，EDA类型：{args.get('type')}"
    }

def run_workflow(args: dict) -> dict:
    """运行PCB工作流，支持多语言AI助手"""
    model_type = "bert" if args.get('use_chinese_model', False) else "gpt"
    lang = args.get('language', 'en')
    
    return {
        "task_id": f"task_{hash(str(args)) % 10000}",
        "status": "running",
        "ai_model": model_type,
        "language": lang,
        "workflow": args.get('workflow_name'),
        "estimated_time": 30.0,
        "progress": 0.0,
        "can_cancel": True,
        "message": f"使用{model_type}模型({lang})执行{args.get('workflow_name')}"
    }

@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="register_project",
            description="支持国产/国外EDA工具的项目注册，自动AI辅助识别",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "本地项目路径"},
                    "type": {"type": "string", "enum": ["kicad", "lceda", "easyeda", "zhongan", "others"], "description": "EDA工具类型，支持国内外"},
                    "description": {"type": "string", "description": "项目描述（可选）"}
                },
                "required": ["path", "type"]
            }
        ),
        Tool(
            name="run_workflow",
            description="运行PCB工作流，支持多语言AI助手",
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "项目ID"},
                    "workflow_name": {"type": "string", "description": "工作流名称"},
                    "parameters": {"type": "object", "description": "工作流参数"},
                    "use_chinese_model": {"type": "boolean", "description": "是否使用国产AI模型"},
                    "language": {"type": "string", "enum": ["zh-CN", "en", "zh-TW", "ja", "ko"], "description": "AI助手语言"}
                },
                "required": ["project_id", "workflow_name"]
            }
        ),
        Tool(
            name="query_eda_capabilities",
            description="查询EDA工具能力",
            inputSchema={
                "type": "object",
                "properties": {
                    "eda_type": {"type": "string", "enum": ["kicad", "lceda", "easyeda"], "description": "EDA工具类型"}
                }
            }
        ),
        Tool(
            name="list_projects",
            description="列出所有项目，支持国产/国外EDA过滤",
            inputSchema={
                "type": "object",
                "properties": {
                    "eda_type": {"type": "string", "enum": ["all", "kicad", "lceda", "easyeda", "zhongan", "others"]},
                    "with_ai_assist": {"type": "boolean"}
                }
            }
        ),
        Tool(
            name="export_release",
            description="导出发布制品，支持多种EDA工具",
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": {"type": "string"},
                    "format": {"type": "string", "enum": ["gerber", "dxf", "step", "kicad"]},
                    "eda_target": {"type": "string", "enum": ["kicad", "lceda", "easyeda"]}
                },
                "required": ["project_id", "format"]
            }
        ),
        Tool(
            name="read_artifacts",
            description="读取指定项目的所有证据",
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": {"type": "string"},
                    "artifact_type": {"type": "string", "enum": ["all", "board", "validation", "release"]}
                }
            }
        ),
        Tool(
            name="list_logs",
            description="列出项目日志，支持AI助手过滤",
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": {"type": "string"},
                    "level": {"type": "string", "enum": ["all", "info", "warning", "error"]},
                    "use_chinese_model": {"type": "boolean"}
                }
            }
        )
    ]

@app.call_tool()
async def call_tool(name: str, arguments: dict) -> CallToolResult:
    if name == "register_project":
        args = RegisterProjectArgs(**arguments)
        return CallToolResult(content=[{"type": "text", "text": str(register_project(arguments))}])
    elif name == "run_workflow":
        args = RunWorkflowArgs(**arguments)
        return CallToolResult(content=[{"type": "text", "text": str(run_workflow(arguments))}])
    elif name == "query_eda_capabilities":
        # 支持多种EDA工具能力查询
        return CallToolResult(content=[{"type": "text", "text": str({
            "kicad": {"version": "9.0", "supported": True, "ai_assisted": True},
            "lceda": {"version": "10.1", "supported": True, "ai_assisted": False},
            "easyeda": {"version": "3.5", "supported": True, "ai_assisted": True},
            "zhongan": {"version": "2.0", "supported": False, "ai_assisted": True},
            "others": {"version": "unknown", "supported": True, "ai_assisted": True}
        })})])
    elif name == "list_projects":
        # 支持国产/国外EDA过滤
        return CallToolResult(content=[{"type": "text", "text": str([{
            "project_id": "proj_1234",
            "name": "测试板",
            "eda_type": "kicad",
            "ai_assisted": True,
            "created_by": "国内助手",
            "created_by": "国外助手"
        }])}])
    elif name == "export_release":
        # 支持多种EDA目标
        return CallToolResult(content=[{"type": "text", "text": str({
            "export_id": "exp_5678",
            "format": arguments.get("format"),
            "eda_target": arguments.get("eda_target", "kicad"),
            "status": "queued",
            "ai_optimized": True,
            "estimated_time": 120.0,
            "message": f"使用{arguments.get('eda_target')}导出{arguments.get('format')}格式"
        })}])
    elif name == "read_artifacts":
        # 读取多种证据类型，支持国产/国外EDA证据
        return CallToolResult(content=[{"type": "text", "text": str([{
            "artifact_id": "art_001",
            "type": "validation",
            "source": "kicad",
            "ai_analysis": True,
            "found_by": "国内助手",
            "confidence": 0.95
        }, {
            "artifact_id": "art_002",
            "type": "release",
            "source": "lceda",
            "ai_analysis": False,
            "found_by": "国外助手",
            "confidence": 0.87
        }])}])
    elif name == "list_logs":
        # 支持AI助手筛选日志
        return CallToolResult(content=[{"type": "text", "text": str([{
            "log_id": "log_001",
            "timestamp": "2026-08-10T10:30:00Z",
            "level": "info",
            "message": "项目启动成功",
            "analyzed_by": "国内助手",
            "ai_suggestion": "可以尝试自动优化布线"
        }, {
            "log_id": "log_002",
            "timestamp": "2026-08-10T10:45:00Z",
            "level": "warning",
            "message": "发现DRC错误",
            "analyzed_by": "国外助手",
            "ai_suggestion": "考虑增加铜面积"
        }])}])
    else:
        raise ValueError(f"未知工具: {name}")
```

### 4.2 AI 助手使用示例

#### 4.2.1 自然语言示例
```bash
# 通过 AI 助手注册项目
assistant> 帮我注册一个 PCB 项目，路径是 C:/my-project

# 通过 AI 助手执行工作流
assistant> 运行我的项目 "project-123" 的验证工作流

# 通过 AI 助手查询 EDA 状态
assistant> 检查当前 KiCad 的状态
```

#### 4.2.2 AI 助手工具调用
```json
{
  "tool_calls": [
    {
      "tool": "register_project",
      "parameters": {
        "path": "C:/my-project",
        "type": "kicad"
      }
    },
    {
      "tool": "run_workflow",
      "parameters": {
        "projectId": "project-123",
        "workflow": "validate"
      }
    }
  ]
}
```

## 5. 部署架构

### 5.1 Docker 部署

#### 5.1.1 Docker Compose
```yaml
version: '3.8'

services:
  pcbflow-api:
    build: ./api
    ports:
      - "8000:8000"
    environment:
      - DATABASE_URL=sqlite:///./data/projects.db
      - KICAD_PATH=${KICAD_PATH}
      - LCEDA_PRO_PATH=${LCEDA_PRO_PATH}
    volumes:
      - ./data:/app/data
      - ./projects:/app/projects

  pcbflow-web:
    build: ./web
    ports:
      - "3000:3000"
    environment:
      - REACT_APP_API_URL=http://localhost:8000
    depends_on:
      - pcbflow-api

  pcbflow-mcp:
    build: ./mcp
    environment:
      - API_URL=http://localhost:8000
```

#### 5.1.2 Docker 镜像
```dockerfile
# API 服务镜像
FROM python:3.13-slim
WORKDIR /app
COPY ./api ./api
RUN pip install -r ./api/requirements.txt
EXPOSE 8000
CMD ["python", "-m", "pcbflow.api"]

# Web 服务镜像
FROM node:20-slim
WORKDIR /app
COPY ./web ./web
RUN npm ci
EXPOSE 3000
CMD ["npm", "start"]

# MCP 服务镜像
FROM node:20-slim
WORKDIR /app
COPY ./mcp ./mcp
RUN npm ci
EXPOSE 8081
CMD ["npm", "start"]
```

### 5.2 CI/CD

#### 5.2.1 GitHub Actions
```yaml
name: Deploy PCBFlow

on:
  push:
    branches: [main]

jobs:
  build-and-deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - name: Build API
        run: |
          cd api
          docker build -t pcbflow/api .
      - name: Build Web
        run: |
          cd web
          docker build -t pcbflow/web .
      - name: Build MCP
        run: |
          cd mcp
          docker build -t pcbflow/mcp .
      - name: Push to Docker Hub
        run: |
          docker login -u ${{ secrets.DOCKER_USERNAME }} -p ${{ secrets.DOCKER_PASSWORD }}
          docker push pcbflow/api
          docker push pcbflow/web
          docker push pcbflow/mcp
```

## 6. 开发指南

### 6.1 环境配置

#### 6.1.1 本地开发环境
```bash
# 安装依赖
pip install -r requirements.txt
npm ci

# 初始化数据库
python -m pcbflow.cli db upgrade

# 启动服务
cd docker
./setup.sh  # 启动所有服务
```

#### 6.1.2 配置文件
```yaml
# config.yaml
api:
  host: "0.0.0.0"
  port: 8000

web:
  host: "0.0.0.0"
  port: 3000

mcp:
  host: "0.0.0.0"
  port: 8081

storage:
  projects: "./data/projects.db"
  evidence: "./data/evidence.db"
  artifacts: "./data/artifacts.db"

eda:
  kicad_path: "/usr/bin/kicad-cli"
  lceda_pro_path: "/opt/lceda-pro/lceda"
  lceda_pro_official_bridge: "/opt/lceda-pro/bridge"
```

### 6.2 代码风格

#### 6.2.1 Python
- 使用 Black 代码格式化器
- 使用 isort 导入排序
- 使用 mypy 类型检查
- 使用 pytest 单元测试

#### 6.2.2 JavaScript/TypeScript
- 使用 ESLint
- 使用 Prettier 代码格式化器
- 使用 Jest 单元测试

### 6.3 测试策略

#### 6.3.1 单元测试
```bash
# 运行单元测试
cd api
pytest pcbflow/ --cov=pcbflow --cov-report=html

cd mcp
npm test
```

#### 6.3.2 集成测试
```bash
# 运行集成测试
cd tests/integration
python -m pytest test_api_cli.py
```

### 6.4 监控与日志

#### 6.4.1 日志配置
```yaml
log:
  level: "info"
  format: "json"
  file: "./logs/api.log"
  rotation:
    max_size: "100MB"
    max_files: 5
```

#### 6.4.2 监控指标
- 响应时间 (p95, p99)
- 错误率
- CPU/Memory 使用率
- 请求/响应大小

## 7. 文档与协议

### 7.1 API 文档
- 基于 OpenAPI 3.0 自动生成
- 通过 Swagger UI 提供在线文档

### 7.2 MCP 协议文档
- OpenAPI 风格的 MCP 协议定义
- 支持自动生成的 MCP 客户端

### 7.3 使用指南
- 在线文档 (docusaurus 或 mkdocs)
- 视频教程
- 示例项目

## 8. 扩展性

### 8.1 模块化设计
- 所有服务均可独立部署
- 通过消息队列实现解耦
- 支持插件架构

### 8.2 支持新 EDA 工具
- 通过适配器模式支持新工具
- 只需实现统一接口即可

### 8.3 支持新工作流类型
- 通过工作流定义 DSL 支持新工作流
- 支持可视化工作流编辑

## 9. 安全考虑

### 9.1 认证与授权
- API 密钥认证
- JWT 令牌认证
- OAuth 2.0 支持

### 9.2 数据安全
- 数据库加密
- 网络传输加密 (TLS)
- 敏感数据脱敏

### 9.3 审计
- 操作日志记录
- 审计日志导出
- 异常追踪

## 10. 贡献指南

### 10.1 提交规范
- 使用 commitlint
- 遵循 Conventional Commits
- 所有 PR 都需要审查

### 10.2 代码审查
- 通过 GitHub PR
- 要求单元测试覆盖率 > 90%
- 需要文档更新

### 10.3 版本管理
- 使用 semantic versioning (SemVer)
- 发布时更新 CHANGELOG.md

---

## 11. 总结

PCBFlow 自动化开发平台通过 Web UI、CLI 和 MCP 协议提供完整的 PCB 开发能力，支持 AI 助手集成，具有良好的可扩展性和可维护性。平台采用模块化设计，支持快速扩展和定制化开发。

### 预期收益
1. **提升开发效率** - 可视化界面减少重复操作
2. **降低技术门槛** - 支持自然语言操作
3. **增强团队协作** - 实时共享与协作
4. **支持 AI 集成** - 与 AI 助手无缝集成
5. **可扩展性** - 易于扩展与维护

### 投资回报率 (ROI)
- **开发时间** - 预计 10 工作日
- **功能覆盖率** - 覆盖所有现有核心功能
- **用户体验提升** - 预期提升 40%
- **技术债务减少** - 采用现代架构与最佳实践


---

**文档更新于 2026 年 8 月发布。**