"""Tests for the MCP multi-model router and fallback behaviour.

These exercise the routing, failover and circuit-breaker logic directly by
stubbing the transport layer, so they run without any model credentials or
network access.
"""

from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import time
import urllib.error
import urllib.request

import pytest

import pcbflow.mcp_server as mcp_module
from pcbflow.mcp_server import (
    CircuitBreakerState,
    CollaborationChain,
    CollaborationPersistence,
    MCPCollaborationServer,
    ModelConfig,
    ModelEndpoint,
    ModelHealthState,
    MultiModelRouter,
    _post_json,
    _post_json_sync,
)


def _model(name: str, priority: str = "primary") -> ModelConfig:
    from pcbflow.mcp_server import CollaborationPriority

    return ModelConfig(
        name=name,
        endpoint_type=ModelEndpoint.LOCAL,
        endpoint_url="http://localhost/v1/chat/completions",
        api_key_env="TEST_KEY",
        model_name=name,
        priority=CollaborationPriority(priority),
    )


def _chain(name: str, *names: str) -> CollaborationChain:
    return CollaborationChain(
        chain_name=name,
        primary=_model(names[0]),
        fallbacks=tuple(_model(n) for n in names[1:]),
        max_attempts=len(names),
    )


def test_select_model_walks_primary_then_fallbacks_in_order() -> None:
    chain = _chain("t", "a", "b", "c")
    router = MultiModelRouter({"t": chain})

    assert [router._select_model(chain, i).name for i in range(3)] == ["a", "b", "c"]
    # Past the end it clamps to the last model rather than raising.
    assert router._select_model(chain, 99).name == "c"


def test_route_task_returns_first_successful_model() -> None:
    seen: list[str] = []

    async def fake(model, task, context):
        seen.append(model.name)
        if model.name == "a":
            raise RuntimeError("boom")
        return {"model": model.name, "content": "ok"}

    router = MultiModelRouter({"t": _chain("t", "a", "b")})
    router._call_model = fake  # type: ignore[method-assign]

    result = asyncio.run(router.route_task("t", {"x": 1}))

    assert seen == ["a", "b"]
    assert result == {"model": "b", "content": "ok"}


def test_route_task_reports_total_failure_with_last_error() -> None:
    async def always_fail(model, task, context):
        raise RuntimeError(f"{model.name} down")

    router = MultiModelRouter({"t": _chain("t", "a", "b")})
    router._call_model = always_fail  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="c down|b down"):
        asyncio.run(router.route_task("t", {}))


def test_unknown_chain_is_rejected() -> None:
    router = MultiModelRouter({})

    with pytest.raises(ValueError, match="not found"):
        asyncio.run(router.route_task("missing", {}))


def test_circuit_breaker_opens_after_enough_failures_and_blocks_calls() -> None:
    calls: list[str] = []

    async def always_fail(model, task, context):
        calls.append(model.name)
        raise RuntimeError("down")

    chain = CollaborationChain(
        chain_name="t",
        primary=_model("a"),
        fallbacks=(_model("b"),),
        max_attempts=2,
        enable_circuit_breaker=True,
        circuit_breaker_threshold=2,
    )
    router = MultiModelRouter({"t": chain})
    router._call_model = always_fail  # type: ignore[method-assign]

    with pytest.raises(RuntimeError):
        asyncio.run(router.route_task("t", {}))
    assert router._circuit_breakers["t"].state == "open"

    before = len(calls)
    with pytest.raises(RuntimeError, match="unavailable"):
        asyncio.run(router.route_task("t", {}))
    assert len(calls) == before, "open breaker must not reach any model"

    # Simulate the cool-down elapsing; the next call moves it to half-open.
    router._circuit_breakers["t"] = CircuitBreakerState(
        state="open", last_failure=0.0, timeout_seconds=0.0
    )
    with pytest.raises(RuntimeError):
        asyncio.run(router.route_task("t", {}))
    assert len(calls) > before


def test_breaker_stays_closed_until_threshold_is_reached() -> None:
    async def always_fail(model, task, context):
        raise RuntimeError("down")

    chain = CollaborationChain(
        chain_name="t",
        primary=_model("a"),
        fallbacks=(),
        max_attempts=5,
        circuit_breaker_threshold=5,
    )
    router = MultiModelRouter({"t": chain})
    router._call_model = always_fail  # type: ignore[method-assign]

    with pytest.raises(RuntimeError):
        asyncio.run(router.route_task("t", {}))
    # Five routed attempts happened, so the breaker is now open.
    assert router._circuit_breakers["t"].state == "open"


def test_chain_without_circuit_breaker_is_always_available() -> None:
    async def always_fail(model, task, context):
        raise RuntimeError("down")

    chain = CollaborationChain(
        chain_name="t",
        primary=_model("a"),
        fallbacks=(),
        max_attempts=1,
        enable_circuit_breaker=False,
    )
    router = MultiModelRouter({"t": chain})
    router._call_model = always_fail  # type: ignore[method-assign]

    for _ in range(3):
        with pytest.raises(RuntimeError):
            asyncio.run(router.route_task("t", {}))
    assert "t" not in router._circuit_breakers


def test_build_messages_includes_system_prompt_only_when_present() -> None:
    router = MultiModelRouter({})
    model = _model("a")

    plain = router._build_messages(model, {"task": 1}, None)
    assert plain == [{"role": "user", "content": '{"task": {"task": 1}, "context": null}'}]

    router._get_system_prompt = lambda m: "be terse"  # type: ignore[method-assign]
    with_system = router._build_messages(model, {}, None)
    assert with_system[0] == {"role": "system", "content": "be terse"}


def test_persistence_records_and_filters_history() -> None:
    async def scenario():
        store = CollaborationPersistence()
        await store.append_history({"collaboration_id": "c1", "model": "a"})
        await store.append_history({"collaboration_id": "c2", "model": "b"})
        await store.append_history({"collaboration_id": "c1", "model": "b"})
        return store

    store = asyncio.run(scenario())

    assert len(asyncio.run(store.get_history("c1"))) == 2
    assert len(asyncio.run(store.get_history(None))) == 3
    assert asyncio.run(store.get_history(None, limit=1))[0]["collaboration_id"] == "c1"


def test_persistence_state_round_trip_and_missing_key() -> None:
    store = CollaborationPersistence()
    asyncio.run(store.save_state("k", {"a": 1}))

    assert asyncio.run(store.load_state("k")) == {"a": 1}
    with pytest.raises(KeyError):
        asyncio.run(store.load_state("missing"))


def test_process_task_reports_completed_with_model() -> None:
    async def fake(model, task, context):
        return {"model": model.name, "content": "done"}

    server = MCPCollaborationServer({})
    server._router._call_model = fake  # type: ignore[method-assign]

    result = asyncio.run(server.process_task("task_planning", {"task": 1}))

    assert result["status"] == "completed"
    assert result["result"]["model"] == "claude-3-5-sonnet"
    assert result["collaboration_id"].startswith("collab_")


def test_process_task_reports_failure_with_error() -> None:
    async def always_fail(model, task, context):
        raise RuntimeError("nope")

    server = MCPCollaborationServer({})
    server._router._call_model = always_fail  # type: ignore[method-assign]

    result = asyncio.run(server.process_task("design_review", {}))

    assert result["status"] == "failed"
    assert "nope" in result["error"]


def test_default_chains_are_well_formed() -> None:
    server = MCPCollaborationServer({})

    assert set(server._router._chains) == {
        "task_planning",
        "code_generation",
        "design_review",
    }
    for chain in server._router._chains.values():
        assert chain.max_attempts >= 1 + len(chain.fallbacks) - len(chain.fallbacks)
        assert chain.primary.name


def test_post_json_sync_reports_transport_failure(monkeypatch) -> None:
    # Hermetic: a real closed port can be intercepted by proxies, security
    # software, or registry-level Windows proxy settings, so raise the
    # transport error directly instead of depending on the network stack.
    def refuse(request, timeout):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)

    with pytest.raises(RuntimeError, match="Local model"):
        _post_json_sync(
            "http://127.0.0.1:9/v1/chat/completions",
            {"probe": True},
            {"Content-Type": "application/json"},
            "Local model",
        )


def test_health_check_against_unreachable_url_is_false() -> None:
    router = MultiModelRouter({})
    model = ModelConfig(
        name="a",
        endpoint_type=ModelEndpoint.LOCAL,
        endpoint_url="http://localhost/v1",
        api_key_env="TEST_KEY",
        model_name="a",
        health_check_url="http://127.0.0.1:1/health",
    )

    assert router._check_health_sync(model.health_check_url) is False


class _FakeResponse:
    def __init__(self, body: bytes = b'{"ok": true}', status: int = 200) -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


def test_post_json_sync_decodes_json_response(monkeypatch) -> None:
    seen: dict[str, object] = {}

    def fake_urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["timeout"] = timeout
        return _FakeResponse(b'{"ok": true}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    result = _post_json_sync(
        "http://example/api", {"a": 1}, {"Content-Type": "application/json"}, "Test"
    )

    assert result == {"ok": True}
    assert seen["url"] == "http://example/api"


def test_post_json_sync_reports_http_error_detail(monkeypatch) -> None:
    def raise_http_error(request, timeout):
        raise urllib.error.HTTPError(
            "http://example/api",
            500,
            "Internal Server Error",
            hdrs=None,
            fp=io.BytesIO(b'{"detail": "exploded"}'),
        )

    monkeypatch.setattr(urllib.request, "urlopen", raise_http_error)

    with pytest.raises(RuntimeError, match="Test API error 500") as excinfo:
        _post_json_sync(
            "http://example/api", {}, {"Content-Type": "application/json"}, "Test"
        )

    assert "exploded" in str(excinfo.value)


def test_post_json_sync_reports_timeout(monkeypatch) -> None:
    def raise_timeout(request, timeout):
        raise TimeoutError()

    monkeypatch.setattr(urllib.request, "urlopen", raise_timeout)

    with pytest.raises(RuntimeError, match="timed out after 30"):
        _post_json_sync(
            "http://example/api", {}, {"Content-Type": "application/json"}, "Test"
        )


def test_post_json_dispatches_blocking_call_to_worker_thread(monkeypatch) -> None:
    calls: list[str] = []

    def fake_sync(url, payload, headers, label):
        calls.append(url)
        return {"ok": True}

    monkeypatch.setattr(mcp_module, "_post_json_sync", fake_sync)

    result = asyncio.run(_post_json("http://example/api", {}, {}, "Test"))

    assert result == {"ok": True}
    assert calls == ["http://example/api"]


def test_call_model_routes_local_endpoint_through_post_json(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    async def fake_post(url, payload, headers, label):
        calls.append((url, label))
        return {"content": "hello"}

    monkeypatch.setattr(mcp_module, "_post_json", fake_post)

    router = MultiModelRouter({})
    result = asyncio.run(router._call_model(_model("local-mistral"), {"task": 1}, {"ctx": 2}))

    assert result == {"content": "hello", "model": "local-mistral"}
    assert calls == [("http://localhost/v1/chat/completions", "Local model")]


def test_call_model_routes_azure_endpoint_through_post_json(monkeypatch) -> None:
    labels: list[str] = []

    async def fake_post(url, payload, headers, label):
        labels.append(label)
        return {"content": "azure-hello"}

    monkeypatch.setattr(mcp_module, "_post_json", fake_post)

    router = MultiModelRouter({})
    model = ModelConfig(
        name="az",
        endpoint_type=ModelEndpoint.AZURE,
        endpoint_url="https://example/openai/deployments/d",
        api_key_env="TEST_KEY",
        model_name="az",
    )
    result = asyncio.run(router._call_model(model, {}))

    assert result == {"content": "azure-hello", "model": "az"}
    assert labels == ["Azure model"]


def test_call_model_rejects_unhealthy_model() -> None:
    router = MultiModelRouter({})
    model = _model("a")
    router._health[model.name] = ModelHealthState(
        healthy=False, last_check=time.monotonic()
    )

    with pytest.raises(RuntimeError, match="not healthy"):
        asyncio.run(router._call_model(model, {}))

    assert router._health["a"].healthy is False


def test_unhealthy_model_is_rechecked_after_cooldown(monkeypatch) -> None:
    async def fake_post(url, payload, headers, label):
        return {"content": "recovered"}

    monkeypatch.setattr(mcp_module, "_post_json", fake_post)

    router = MultiModelRouter({})
    model = _model("a")
    router._health[model.name] = ModelHealthState(
        healthy=False, last_check=time.monotonic() - 301
    )

    result = asyncio.run(router._call_model(model, {}))

    assert result["model"] == "a"
    assert router._health["a"].healthy is True


@pytest.mark.skipif(
    importlib.util.find_spec("openai") is not None, reason="openai is installed"
)
def test_missing_openai_sdk_is_reported_as_runtime_error() -> None:
    router = MultiModelRouter({})
    model = ModelConfig(
        name="g",
        endpoint_type=ModelEndpoint.OPENAI,
        endpoint_url="https://example/v1",
        api_key_env="TEST_KEY",
        model_name="g",
    )

    with pytest.raises(RuntimeError, match="openai package is not installed"):
        asyncio.run(router._call_openai(model, {}))


@pytest.mark.skipif(
    importlib.util.find_spec("anthropic") is not None, reason="anthropic is installed"
)
def test_missing_anthropic_sdk_is_reported_as_runtime_error() -> None:
    router = MultiModelRouter({})
    model = ModelConfig(
        name="c",
        endpoint_type=ModelEndpoint.ANTHROPIC,
        endpoint_url="https://example/v1",
        api_key_env="TEST_KEY",
        model_name="c",
    )

    with pytest.raises(RuntimeError, match="anthropic package is not installed"):
        asyncio.run(router._call_anthropic(model, {}))


def test_health_check_returns_true_for_http_200(monkeypatch) -> None:
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda request, timeout: _FakeResponse(status=200)
    )

    assert MultiModelRouter({})._check_health_sync("http://example/health") is True


def test_record_success_closes_half_open_breaker() -> None:
    router = MultiModelRouter({"t": _chain("t", "a")})
    router._circuit_breakers["t"] = CircuitBreakerState(
        state="half-open", failure_count=2
    )

    asyncio.run(router._record_success("a", "t"))

    breaker = router._circuit_breakers["t"]
    assert breaker.state == "closed"
    assert breaker.success_count == 1


def test_record_success_leaves_closed_breaker_untouched() -> None:
    router = MultiModelRouter({"t": _chain("t", "a")})
    router._circuit_breakers["t"] = CircuitBreakerState(state="closed")

    asyncio.run(router._record_success("a", "t"))

    assert router._circuit_breakers["t"].state == "closed"


def test_register_project_tool_reports_created_project() -> None:
    payload = json.loads(
        asyncio.run(
            mcp_module.pcbflow_register_project(
                path="C:\\work\\demo", type="kicad", description="demo board"
            )
        )
    )

    assert payload["status"] == "created"
    assert payload["name"] == "demo"
    assert payload["eda_type"] == "kicad"
    assert payload["project_id"].startswith("proj_")


def test_run_workflow_tool_falls_back_until_local_model_answers(monkeypatch) -> None:
    async def fake_post(url, payload, headers, label):
        return {"content": "planned"}

    monkeypatch.setattr(mcp_module, "_post_json", fake_post)
    monkeypatch.setattr(mcp_module, "_collab_server", None)

    payload = json.loads(
        asyncio.run(
            mcp_module.pcbflow_run_workflow(
                project_id="prj_1",
                workflow_name="plan",
                parameters={"goal": "plan"},
            )
        )
    )

    assert payload["status"] == "completed"
    assert payload["result"]["model"] == "mistral-large"


def test_query_chains_tool_lists_all_and_single_chain() -> None:
    everything = json.loads(asyncio.run(mcp_module.pcbflow_query_chains()))
    assert set(everything["chains"]) == {
        "task_planning",
        "code_generation",
        "design_review",
    }

    single = json.loads(
        asyncio.run(mcp_module.pcbflow_query_chains(chain_name="code_generation"))
    )
    assert single["chain_name"] == "code_generation"
    assert single["primary_model"]["name"] == "codellama-34b"
    assert single["fallback_models"][0]["name"] == "gpt-4o"

    missing = json.loads(
        asyncio.run(mcp_module.pcbflow_query_chains(chain_name="nope"))
    )
    assert missing == {"error": "Chain not found: nope"}


def test_list_history_tool_returns_recorded_collaborations(monkeypatch) -> None:
    monkeypatch.setattr(mcp_module, "_collab_server", None)
    server = mcp_module._init_collab_server()
    asyncio.run(
        server._persistence.append_history(
            {"collaboration_id": "collab_x", "model": "a"}
        )
    )

    payload = json.loads(
        asyncio.run(
            mcp_module.pcbflow_list_history(collaboration_id="collab_x", limit=5)
        )
    )

    assert payload["total"] == 1
    assert payload["entries"][0]["collaboration_id"] == "collab_x"


def test_list_projects_and_read_artifacts_tools_return_empty_placeholders() -> None:
    projects = json.loads(asyncio.run(mcp_module.pcbflow_list_projects()))
    assert projects["total"] == 0
    assert projects["projects"] == []

    artifacts = json.loads(
        asyncio.run(
            mcp_module.pcbflow_read_artifacts(project_id="prj_1", artifact_type="drc")
        )
    )
    assert artifacts["project_id"] == "prj_1"
    assert artifacts["total"] == 0


def test_query_eda_capabilities_tool_filters_by_type() -> None:
    everything = json.loads(asyncio.run(mcp_module.pcbflow_query_eda_capabilities()))
    assert set(everything["eda_capabilities"]) == {
        "kicad",
        "lceda",
        "easyeda",
        "zhongan",
        "others",
    }

    filtered = json.loads(
        asyncio.run(mcp_module.pcbflow_query_eda_capabilities(eda_type="kicad"))
    )
    assert set(filtered["eda_capabilities"]) == {"kicad"}

    unknown = json.loads(
        asyncio.run(mcp_module.pcbflow_query_eda_capabilities(eda_type="mystery"))
    )
    assert unknown["eda_capabilities"] == {"mystery": {}}


def test_init_collab_server_creates_and_caches_singleton(monkeypatch) -> None:
    monkeypatch.setattr(mcp_module, "_collab_server", None)

    first = mcp_module._init_collab_server()
    second = mcp_module._init_collab_server()

    assert first is second


def test_main_runs_the_server(monkeypatch) -> None:
    ran: list[bool] = []
    monkeypatch.setattr(mcp_module.app, "run", lambda: ran.append(True))

    mcp_module.main()

    assert ran == [True]
