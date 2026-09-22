from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, ListToolsResult, Tool

logger = logging.getLogger(__name__)

ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class CollaborationPriority(Enum):
    PRIMARY = "primary"
    SECONDARY = "secondary"
    TERTIARY = "tertiary"
    FALLBACK = "fallback"


class ModelEndpoint(Enum):
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    LOCAL = "local"
    AZURE = "azure"


@dataclass(frozen=True, slots=True)
class ModelConfig:
    name: str
    endpoint_type: ModelEndpoint
    endpoint_url: str
    api_key_env: str
    model_name: str
    max_tokens: int = 4000
    temperature: float = 0.7
    priority: CollaborationPriority = CollaborationPriority.PRIMARY
    health_check_url: Optional[str] = None
    capabilities: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CollaborationChain:
    chain_name: str
    primary: ModelConfig
    fallbacks: tuple[ModelConfig, ...]
    max_attempts: int = 3
    retry_delay_seconds: float = 1.0
    enable_circuit_breaker: bool = True
    circuit_breaker_threshold: int = 5
    circuit_breaker_timeout_seconds: int = 300


@dataclass(slots=True)
class ModelHealthState:
    healthy: bool = True
    last_check: float = 0.0
    failure_count: int = 0
    last_failure: Optional[float] = None


@dataclass(slots=True)
class CircuitBreakerState:
    state: str = "closed"
    last_failure: float = 0.0
    failure_count: int = 0
    success_count: int = 0
    timeout_seconds: float = 300.0


class MultiModelRouter:
    def __init__(self, chains: Dict[str, CollaborationChain]) -> None:
        self._chains = chains
        self._health: Dict[str, ModelHealthState] = {}
        self._circuit_breakers: Dict[str, CircuitBreakerState] = {}
        self._lock = asyncio.Lock()

    async def route_task(
        self,
        chain_name: str,
        task: Dict[str, Any],
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if chain_name not in self._chains:
            raise ValueError(f"Collaboration chain not found: {chain_name}")

        chain = self._chains[chain_name]

        if not await self._is_chain_available(chain_name):
            raise RuntimeError(f"Collaboration chain unavailable: {chain_name}")

        last_exception: Optional[Exception] = None
        for attempt in range(chain.max_attempts):
            model = self._select_model(chain, attempt)
            try:
                result = await self._call_model(model, task, context)
                await self._record_success(model.name, chain_name)
                return result
            except Exception as exc:
                last_exception = exc
                await self._record_failure(model.name, chain_name, exc)
                logger.warning(
                    "Model %s failed for chain %s (attempt %d): %s",
                    model.name,
                    chain_name,
                    attempt,
                    exc,
                )
                continue

        raise RuntimeError(
            f"All models in chain '{chain_name}' failed after {chain.max_attempts} attempts. "
            f"Last error: {last_exception}"
        )

    async def _is_chain_available(self, chain_name: str) -> bool:
        chain = self._chains[chain_name]
        if not chain.enable_circuit_breaker:
            return True

        async with self._lock:
            state = self._circuit_breakers.get(chain_name)
        if state is None:
            return True
        if state.state == "open":
            elapsed = time.monotonic() - state.last_failure
            if elapsed < state.timeout_seconds:
                return False
            async with self._lock:
                self._circuit_breakers[chain_name] = CircuitBreakerState(
                    state="half-open",
                    last_failure=state.last_failure,
                    failure_count=state.failure_count,
                    success_count=state.success_count,
                    timeout_seconds=state.timeout_seconds,
                )
        return True

    def _select_model(self, chain: CollaborationChain, attempt: int) -> ModelConfig:
        if attempt == 0:
            return chain.primary
        all_models: list[ModelConfig] = [chain.primary] + list(chain.fallbacks)
        index = min(attempt, len(all_models) - 1)
        return all_models[index]

    async def _call_model(
        self,
        model: ModelConfig,
        task: Dict[str, Any],
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if not await self._is_model_healthy(model):
            raise RuntimeError(f"Model {model.name} is not healthy")

        if model.endpoint_type == ModelEndpoint.OPENAI:
            return await self._call_openai(model, task, context)
        if model.endpoint_type == ModelEndpoint.ANTHROPIC:
            return await self._call_anthropic(model, task, context)
        if model.endpoint_type == ModelEndpoint.LOCAL:
            return await self._call_local(model, task, context)
        if model.endpoint_type == ModelEndpoint.AZURE:
            return await self._call_azure(model, task, context)
        raise ValueError(f"Unsupported model endpoint type: {model.endpoint_type}")

    async def _call_openai(
        self,
        model: ModelConfig,
        task: Dict[str, Any],
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:
            raise RuntimeError("openai package is not installed") from exc

        api_key = os.environ.get(model.api_key_env, "")
        client = AsyncOpenAI(api_key=api_key)
        messages = self._build_messages(model, task, context)

        response = await client.chat.completions.create(
            model=model.model_name,
            messages=messages,
            max_tokens=model.max_tokens,
            temperature=model.temperature,
        )

        return {
            "content": response.choices[0].message.content,
            "model": model.name,
            "usage": {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            },
            "finish_reason": response.choices[0].finish_reason,
        }

    async def _call_anthropic(
        self,
        model: ModelConfig,
        task: Dict[str, Any],
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        try:
            from anthropic import AsyncAnthropic
        except ImportError as exc:
            raise RuntimeError("anthropic package is not installed") from exc

        api_key = os.environ.get(model.api_key_env, "")
        client = AsyncAnthropic(api_key=api_key)
        messages = self._build_messages(model, task, context)

        response = await client.messages.create(
            model=model.model_name,
            max_tokens=model.max_tokens,
            temperature=model.temperature,
            messages=messages,
        )

        return {
            "content": response.content[0].text,
            "model": model.name,
            "usage": {
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
                "total_tokens": response.usage.input_tokens + response.usage.output_tokens,
            },
            "stop_reason": response.stop_reason,
        }

    async def _call_local(
        self,
        model: ModelConfig,
        task: Dict[str, Any],
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        url = model.endpoint_url
        api_key = os.environ.get(model.api_key_env, "default")
        payload: Dict[str, Any] = {
            "model": model.model_name,
            "messages": self._build_messages(model, task, context),
            "max_tokens": model.max_tokens,
            "temperature": model.temperature,
        }
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

        loop = asyncio.get_event_loop()
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers, timeout=30.0) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise RuntimeError(f"Local model API error {resp.status}: {text}")
                result: Dict[str, Any] = await resp.json()
                result["model"] = model.name
                return result

    async def _call_azure(
        self,
        model: ModelConfig,
        task: Dict[str, Any],
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        import aiohttp

        url = model.endpoint_url
        api_key = os.environ.get(model.api_key_env, "")
        payload: Dict[str, Any] = {
            "model": model.model_name,
            "messages": self._build_messages(model, task, context),
            "max_tokens": model.max_tokens,
            "temperature": model.temperature,
        }
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers, timeout=30.0) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise RuntimeError(f"Azure model API error {resp.status}: {text}")
                result: Dict[str, Any] = await resp.json()
                result["model"] = model.name
                return result

    def _build_messages(
        self,
        model: ModelConfig,
        task: Dict[str, Any],
        context: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, str]]:
        system_prompt = self._get_system_prompt(model)
        user_prompt = self._build_user_prompt(model, task, context)
        messages: List[Dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_prompt})
        return messages

    def _build_user_prompt(
        self,
        model: ModelConfig,
        task: Dict[str, Any],
        context: Optional[Dict[str, Any]] = None,
    ) -> str:
        return json.dumps({"task": task, "context": context}, ensure_ascii=False)

    def _get_system_prompt(self, model: ModelConfig) -> str:
        return ""

    async def _is_model_healthy(self, model: ModelConfig) -> bool:
        async with self._lock:
            state = self._health.get(model.name)
        if state is None:
            healthy = await self._check_health(model)
            async with self._lock:
                self._health[model.name] = ModelHealthState(healthy=healthy, last_check=time.monotonic())
            return healthy

        if not state.healthy:
            elapsed = time.monotonic() - state.last_check
            if elapsed > 300.0:
                healthy = await self._check_health(model)
                async with self._lock:
                    self._health[model.name] = ModelHealthState(healthy=healthy, last_check=time.monotonic())
                return healthy

        return state.healthy

    async def _check_health(self, model: ModelConfig) -> bool:
        if model.health_check_url:
            try:
                import aiohttp
                async with aiohttp.ClientSession() as session:
                    async with session.get(model.health_check_url, timeout=5.0) as resp:
                        return resp.status == 200
            except Exception:
                return False
        return True

    async def _record_success(self, model_name: str, chain_name: str) -> None:
        async with self._lock:
            self._health.pop(model_name, None)
            cb = self._circuit_breakers.get(chain_name)
            if cb is not None and cb.state == "half-open":
                self._circuit_breakers[chain_name] = CircuitBreakerState(
                    state="closed",
                    last_failure=cb.last_failure,
                    failure_count=cb.failure_count,
                    success_count=cb.success_count + 1,
                    timeout_seconds=cb.timeout_seconds,
                )

    async def _record_failure(
        self, model_name: str, chain_name: str, error: Exception
    ) -> None:
        now = time.monotonic()
        async with self._lock:
            state = self._circuit_breakers.get(chain_name)
            if state is None:
                state = CircuitBreakerState()
            state.failure_count += 1
            state.last_failure = now
            if state.failure_count >= chain.max_attempts:
                state.state = "open"
            self._circuit_breakers[chain_name] = state


class CollaborationPersistence:
    def __init__(self) -> None:
        self._states: Dict[str, Dict[str, Any]] = {}
        self._history: List[Dict[str, Any]] = []
        self._lock = asyncio.Lock()

    async def save_state(self, key: str, value: Dict[str, Any]) -> None:
        async with self._lock:
            self._states[key] = value
        logger.info("Persisted state: %s", key)

    async def load_state(self, key: str) -> Dict[str, Any]:
        async with self._lock:
            if key not in self._states:
                raise KeyError(f"State not found: {key}")
            return dict(self._states[key])

    async def append_history(self, entry: Dict[str, Any]) -> None:
        async with self._lock:
            self._history.append(entry)

    async def get_history(
        self, collaboration_id: Optional[str] = None, limit: int = 10
    ) -> List[Dict[str, Any]]:
        async with self._lock:
            if collaboration_id is None:
                return self._history[-limit:]
            return [
                entry
                for entry in self._history
                if entry.get("collaboration_id") == collaboration_id
            ][-limit:]


class MCPCollaborationServer:
    def __init__(self, settings: Dict[str, Any]) -> None:
        self._settings = settings
        self._router = MultiModelRouter(self._load_chains())
        self._persistence = CollaborationPersistence()

    def _load_chains(self) -> Dict[str, CollaborationChain]:
        return {
            "task_planning": CollaborationChain(
                chain_name="task_planning",
                primary=ModelConfig(
                    name="claude-3-5-sonnet",
                    endpoint_type=ModelEndpoint.ANTHROPIC,
                    endpoint_url="https://api.anthropic.com/v1/messages",
                    api_key_env="ANTHROPIC_API_KEY",
                    model_name="claude-3-5-sonnet-20241022",
                    capabilities=("reasoning", "analysis", "code"),
                ),
                fallbacks=(
                    ModelConfig(
                        name="gpt-4o",
                        endpoint_type=ModelEndpoint.OPENAI,
                        endpoint_url="https://api.openai.com/v1/chat/completions",
                        api_key_env="OPENAI_API_KEY",
                        model_name="gpt-4o",
                        capabilities=("reasoning", "code"),
                    ),
                    ModelConfig(
                        name="mistral-large",
                        endpoint_type=ModelEndpoint.LOCAL,
                        endpoint_url="http://localhost:8080/v1/chat/completions",
                        api_key_env="LOCAL_API_KEY",
                        model_name="mistral-large",
                        capabilities=("code",),
                    ),
                ),
                max_attempts=3,
                retry_delay_seconds=1.0,
            ),
            "code_generation": CollaborationChain(
                chain_name="code_generation",
                primary=ModelConfig(
                    name="codellama-34b",
                    endpoint_type=ModelEndpoint.LOCAL,
                    endpoint_url="http://localhost:8081/v1/chat/completions",
                    api_key_env="LOCAL_API_KEY",
                    model_name="codellama-34b",
                    capabilities=("code",),
                ),
                fallbacks=(
                    ModelConfig(
                        name="gpt-4o",
                        endpoint_type=ModelEndpoint.OPENAI,
                        endpoint_url="https://api.openai.com/v1/chat/completions",
                        api_key_env="OPENAI_API_KEY",
                        model_name="gpt-4o",
                        capabilities=("code",),
                    ),
                ),
                max_attempts=2,
            ),
            "design_review": CollaborationChain(
                chain_name="design_review",
                primary=ModelConfig(
                    name="claude-3-opus",
                    endpoint_type=ModelEndpoint.ANTHROPIC,
                    endpoint_url="https://api.anthropic.com/v1/messages",
                    api_key_env="ANTHROPIC_API_KEY",
                    model_name="claude-3-opus-20240229",
                    capabilities=("analysis", "review"),
                ),
                fallbacks=(
                    ModelConfig(
                        name="gpt-4o",
                        endpoint_type=ModelEndpoint.OPENAI,
                        endpoint_url="https://api.openai.com/v1/chat/completions",
                        api_key_env="OPENAI_API_KEY",
                        model_name="gpt-4o",
                        capabilities=("analysis",),
                    ),
                ),
                max_attempts=3,
            ),
        }

    async def process_task(
        self,
        chain_name: str,
        task_data: Dict[str, Any],
        project_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        collaboration_id = f"collab_{int(time.monotonic())}_{chain_name}"

        try:
            result = await self._router.route_task(chain_name, task_data, project_context)

            await self._persistence.append_history(
                {
                    "collaboration_id": collaboration_id,
                    "chain_name": chain_name,
                    "model": result.get("model", "unknown"),
                    "task_type": chain_name,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "result": result,
                }
            )

            return {
                "collaboration_id": collaboration_id,
                "status": "completed",
                "result": result,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        except Exception as exc:
            logger.error("Collaboration failed for %s: %s", collaboration_id, exc)

            await self._persistence.append_history(
                {
                    "collaboration_id": collaboration_id,
                    "chain_name": chain_name,
                    "model": "failed",
                    "task_type": chain_name,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "error": str(exc),
                }
            )

            return {
                "collaboration_id": collaboration_id,
                "status": "failed",
                "error": str(exc),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }


def _make_enhanced_tools() -> List[Tool]:
    return [
        Tool(
            name="pcbflow_register_project",
            description="Register a local PCB project with enhanced AI collaboration support",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "type": {"type": "string", "enum": ["kicad", "lceda", "easyeda", "zhongan", "others"]},
                    "description": {"type": "string"},
                    "collaboration_mode": {"type": "string", "enum": ["auto", "primary_only", "fallback_enabled"]},
                },
                "required": ["path", "type"],
            },
        ),
        Tool(
            name="pcbflow_run_workflow",
            description="Run a PCB workflow with intelligent AI model selection and fallback",
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": {"type": "string"},
                    "workflow_name": {"type": "string"},
                    "parameters": {"type": "object"},
                    "collaboration_chain": {"type": "string"},
                    "use_primary_only": {"type": "boolean"},
                    "language": {"type": "string"},
                },
                "required": ["project_id", "workflow_name"],
            },
        ),
        Tool(
            name="pcbflow_query_chains",
            description="Query available AI collaboration chains and their models",
            inputSchema={
                "type": "object",
                "properties": {"chain_name": {"type": "string"}},
            },
        ),
        Tool(
            name="pcbflow_list_history",
            description="List collaboration history with intelligent model usage",
            inputSchema={
                "type": "object",
                "properties": {
                    "collaboration_id": {"type": "string"},
                    "limit": {"type": "number", "default": 10},
                },
            },
        ),
        Tool(
            name="pcbflow_list_projects",
            description="List registered PCB projects",
            inputSchema={"type": "object", "properties": {"eda_type": {"type": "string"}}},
        ),
        Tool(
            name="pcbflow_read_artifacts",
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
            name="pcbflow_query_eda_capabilities",
            description="Query EDA capabilities",
            inputSchema={"type": "object", "properties": {"eda_type": {"type": "string"}}},
        ),
    ]


async def _handle_register_project(args: Dict[str, Any]) -> CallToolResult:
    project_id = f"proj_{hash(str(args.get('path', ''))) % 10000}"
    result = {
        "project_id": project_id,
        "name": args.get("path", "").rsplit("\\", 1)[-1],
        "eda_type": args.get("type", "kicad"),
        "description": args.get("description"),
        "status": "created",
        "collaboration_mode": args.get("collaboration_mode", "auto"),
        "created_at": datetime.now(timezone.utc).strftime(ISO_FORMAT),
        "supported_ai": ["claude-3-5-sonnet", "gpt-4o", "mistral-large", "codellama-34b"],
    }
    return CallToolResult(content=[{"type": "text", "text": json.dumps(result, ensure_ascii=False)}])


async def _handle_run_workflow(args: Dict[str, Any], server: MCPCollaborationServer) -> CallToolResult:
    chain_name = args.get("collaboration_chain", "task_planning")
    project_context = {"project_id": args.get("project_id"), "workflow_name": args.get("workflow_name")}
    task_data = {
        "parameters": args.get("parameters", {}),
        "language": args.get("language", "en"),
        "use_primary_only": args.get("use_primary_only", False),
    }
    result = await server.process_task(chain_name, task_data, project_context)
    return CallToolResult(content=[{"type": "text", "text": json.dumps(result, ensure_ascii=False)}])


async def _handle_query_chains(args: Dict[str, Any], server: MCPCollaborationServer) -> CallToolResult:
    chain_name = args.get("chain_name")
    chains = server._router._chains

    if chain_name:
        if chain_name not in chains:
            return CallToolResult(
                content=[{"type": "text", "text": json.dumps({"error": f"Chain not found: {chain_name}"}, ensure_ascii=False)}]
            )
        chain = chains[chain_name]
        result = {
            "chain_name": chain.chain_name,
            "primary_model": {
                "name": chain.primary.name,
                "endpoint": chain.primary.endpoint_type.value,
                "model": chain.primary.model_name,
                "priority": chain.primary.priority.value,
                "capabilities": list(chain.primary.capabilities),
            },
            "fallback_models": [
                {
                    "name": m.name,
                    "endpoint": m.endpoint_type.value,
                    "model": m.model_name,
                    "priority": m.priority.value,
                    "capabilities": list(m.capabilities),
                }
                for m in chain.fallbacks
            ],
            "max_attempts": chain.max_attempts,
            "retry_delay_seconds": chain.retry_delay_seconds,
            "enable_circuit_breaker": chain.enable_circuit_breaker,
        }
    else:
        result = {
            "chains": {
                name: {
                    "chain_name": chain.chain_name,
                    "primary_model": {
                        "name": chain.primary.name,
                        "endpoint": chain.primary.endpoint_type.value,
                        "model": chain.primary.model_name,
                        "priority": chain.primary.priority.value,
                    },
                    "fallback_count": len(chain.fallbacks),
                    "max_attempts": chain.max_attempts,
                }
                for name, chain in chains.items()
            }
        }
    return CallToolResult(content=[{"type": "text", "text": json.dumps(result, ensure_ascii=False)}])


async def _handle_list_history(args: Dict[str, Any], server: MCPCollaborationServer) -> CallToolResult:
    collab_id = args.get("collaboration_id")
    limit = int(args.get("limit", 10))
    history = await server._persistence.get_history(collab_id, limit)
    result = {"collaboration_id": collab_id, "total": len(history), "limit": limit, "entries": history}
    return CallToolResult(content=[{"type": "text", "text": json.dumps(result, ensure_ascii=False)}])


async def _handle_list_projects(args: Dict[str, Any]) -> CallToolResult:
    result = {"projects": [], "total": 0, "note": "No persistent project store in this MCP server"}
    return CallToolResult(content=[{"type": "text", "text": json.dumps(result, ensure_ascii=False)}])


async def _handle_read_artifacts(args: Dict[str, Any]) -> CallToolResult:
    result = {"project_id": args.get("project_id"), "artifacts": [], "total": 0}
    return CallToolResult(content=[{"type": "text", "text": json.dumps(result, ensure_ascii=False)}])


async def _handle_query_capabilities(args: Dict[str, Any]) -> CallToolResult:
    capabilities = {
        "kicad": {"supported": True, "ai_assisted": True, "features": ["DRC", "ERC", "3D", "gerber"]},
        "lceda": {"supported": True, "ai_assisted": False, "features": ["DFM", "simulation", "import-export"]},
        "easyeda": {"supported": True, "ai_assisted": True, "features": ["cloud collaboration", "component library"]},
        "zhongan": {"supported": False, "ai_assisted": True, "features": ["AI algorithm", "patent detection"]},
        "others": {"supported": True, "ai_assisted": True, "features": ["third-party EDA integration"]},
    }
    eda_type = args.get("eda_type", "all")
    result = {"eda_capabilities": capabilities if eda_type == "all" else {eda_type: capabilities.get(eda_type, {})}}
    return CallToolResult(content=[{"type": "text", "text": json.dumps(result, ensure_ascii=False)}])


app = MCPServer(
    name="pcbflow-enhanced",
    title="PCBFlow Enhanced MCP Server",
    instructions=(
        "Expose PCBFlow project, workflow, EDA capability, artifact, "
        "and log operations to compatible AI assistants with multi-model "
        "routing and intelligent fallback."
    ),
    tools=[],
    debug=False,
)

_collab_server: Optional[MCPCollaborationServer] = None


def _init_collab_server() -> MCPCollaborationServer:
    global _collab_server
    if _collab_server is None:
        settings: Dict[str, Any] = {
            "anthropic_api_key": os.environ.get("ANTHROPIC_API_KEY", ""),
            "openai_api_key": os.environ.get("OPENAI_API_KEY", ""),
        }
        _collab_server = MCPCollaborationServer(settings)
    return _collab_server


async def _on_list_tools() -> ListToolsResult:
    return ListToolsResult(tools=_make_enhanced_tools())


async def _on_call_tool(name: str, arguments: Dict[str, Any]) -> CallToolResult:
    server = _init_collab_server()
    if name == "pcbflow_register_project":
        return await _handle_register_project(arguments)
    if name == "pcbflow_run_workflow":
        return await _handle_run_workflow(arguments, server)
    if name == "pcbflow_query_chains":
        return await _handle_query_chains(arguments, server)
    if name == "pcbflow_list_history":
        return await _handle_list_history(arguments, server)
    if name == "pcbflow_list_projects":
        return await _handle_list_projects(arguments)
    if name == "pcbflow_read_artifacts":
        return await _handle_read_artifacts(arguments)
    if name == "pcbflow_query_eda_capabilities":
        return await _handle_query_capabilities(arguments)
    return CallToolResult(content=[{"type": "text", "text": json.dumps({"error": f"Unknown tool: {name}"}, ensure_ascii=False)}])


app.on_list_tools = _on_list_tools
app.on_call_tool = _on_call_tool


def main() -> None:
    app.run()


if __name__ == "__main__":
    main()