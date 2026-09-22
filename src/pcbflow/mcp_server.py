"""
PCBFlow MCP Server
支持国产/国外 AI 助手，多模型适配，支持多种 EDA 工具
"""

import json
import logging
from typing import Any, Dict, List

from mcp.server.mcpserver import MCPServer
from mcp.types import Tool, CallToolResult, ListToolsResult

app = MCPServer(
    name="pcbflow",
    title="PCBFlow MCP Server",
    instructions=(
        "Expose PCBFlow project, workflow, EDA capability, artifact, "
        "and log operations to compatible AI assistants."
    ),
    tools=[],
    debug=False,
)

projects_db: Dict[str, Dict[str, Any]] = {}
artifacts_db: Dict[str, Dict[str, Any]] = {}
logs_db: List[Dict[str, Any]] = []


def _json_result(value: Any) -> CallToolResult:
    return CallToolResult(
        content=[{"type": "text", "text": json.dumps(value, ensure_ascii=False)}]
    )


def _tools() -> List[Tool]:
    return [
        Tool(
            name="register_project",
            description="Register a local PCB project",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "type": {"type": "string", "enum": ["kicad", "lceda", "easyeda", "zhongan", "others"]},
                    "description": {"type": "string"},
                },
                "required": ["path", "type"],
            },
        ),
        Tool(
            name="run_workflow",
            description="Run a PCB workflow",
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": {"type": "string"},
                    "workflow_name": {"type": "string"},
                    "parameters": {"type": "object"},
                    "use_chinese_model": {"type": "boolean"},
                    "language": {"type": "string"},
                },
                "required": ["project_id", "workflow_name"],
            },
        ),
        Tool(
            name="query_eda_capabilities",
            description="Query EDA capabilities",
            inputSchema={"type": "object", "properties": {"eda_type": {"type": "string"}}},
        ),
        Tool(
            name="list_projects",
            description="List registered PCB projects",
            inputSchema={"type": "object", "properties": {"eda_type": {"type": "string"}}},
        ),
        Tool(
            name="export_release",
            description="Export release artifacts",
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": {"type": "string"},
                    "format": {"type": "string"},
                    "eda_target": {"type": "string"},
                },
                "required": ["project_id", "format"],
            },
        ),
        Tool(
            name="read_artifacts",
            description="Read project evidence artifacts",
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": {"type": "string"},
                    "artifact_type": {"type": "string"},
                },
                "required": ["project_id"],
            },
        ),
        Tool(
            name="list_logs",
            description="List project logs",
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": {"type": "string"},
                    "level": {"type": "string"},
                },
                "required": ["project_id"],
            },
        ),
    ]


async def _on_list_tools() -> ListToolsResult:
    return ListToolsResult(tools=_tools())


async def _on_call_tool(name: str, arguments: Dict[str, Any]) -> CallToolResult:
    if name == "register_project":
        return _json_result(_register_project(arguments))
    if name == "run_workflow":
        return _json_result(_run_workflow(arguments))
    if name == "list_projects":
        return _json_result(_list_projects(arguments))
    if name == "read_artifacts":
        return _json_result(_read_artifacts(arguments))
    if name == "list_logs":
        return _json_result(_list_logs(arguments))
    if name == "query_eda_capabilities":
        return _json_result(_query_eda_capabilities(arguments))
    if name == "export_release":
        return _json_result(_export_release(arguments))
    return _json_result({"error": f"Unknown tool: {name}"})


app.on_list_tools = _on_list_tools
app.on_call_tool = _on_call_tool


def _register_project(args: Dict[str, Any]) -> Dict[str, Any]:
    project_id = f"proj_{hash(str(args.get('path', ''))) % 10000}"
    return {
        "project_id": project_id,
        "name": args.get("path", "").rsplit("\\", 1)[-1],
        "eda_type": args.get("type", "kicad"),
        "description": args.get("description"),
        "status": "created",
        "supported_ai": [],
        "created_at": "2026-08-10T10:30:00Z",
    }


def _run_workflow(args: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "task_id": f"task_{hash(str(args)) % 10000}",
        "project_id": args.get("project_id"),
        "workflow": args.get("workflow_name"),
        "status": "running",
        "ai_model": "gpt" if args.get("use_chinese_model") else "bert",
        "language": args.get("language", "en"),
        "parameters": args.get("parameters", {}),
        "progress": 0.0,
    }


def _list_projects(args: Dict[str, Any]) -> Dict[str, Any]:
    eda_type = args.get("eda_type", "all")
    projects = [project for project in projects_db.values() if eda_type in {"all", project.get("eda_type")}]
    return {"projects": projects, "total": len(projects)}


def _read_artifacts(args: Dict[str, Any]) -> Dict[str, Any]:
    project_id = args.get("project_id")
    artifacts = artifacts_db.get(project_id, {}).get("artifacts", [])
    return {"project_id": project_id, "artifacts": artifacts, "total": len(artifacts)}


def _list_logs(args: Dict[str, Any]) -> Dict[str, Any]:
    project_id = args.get("project_id")
    logs = logs_db if project_id is None else [log for log in logs_db if log.get("project_id") == project_id]
    return {"project_id": project_id, "logs": logs, "total": len(logs)}


def _query_eda_capabilities(args: Dict[str, Any]) -> Dict[str, Any]:
    capabilities = {
        "kicad": {"supported": True, "ai_assisted": True, "features": ["DRC", "ERC", "3D", "gerber"]},
        "lceda": {"supported": True, "ai_assisted": False, "features": ["DFM", "simulation", "import-export"]},
        "easyeda": {"supported": True, "ai_assisted": True, "features": ["cloud collaboration", "component library"]},
        "zhongan": {"supported": False, "ai_assisted": True, "features": ["AI algorithm", "patent detection"]},
        "others": {"supported": True, "ai_assisted": True, "features": ["third-party EDA integration"]},
    }
    eda_type = args.get("eda_type", "all")
    return {"eda_capabilities": capabilities if eda_type == "all" else {eda_type: capabilities.get(eda_type, {})}}


def _export_release(args: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "export_id": f"exp_{hash(str(args)) % 10000}",
        "project_id": args.get("project_id"),
        "format": args.get("format"),
        "eda_target": args.get("eda_target", "kicad"),
        "status": "queued",
        "progress": 0.0,
    }


def main() -> None:
    app.run()


if __name__ == "__main__":
    main()
