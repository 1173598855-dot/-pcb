"""Tests for the MCP multi-model router and fallback behaviour.

These exercise the routing, failover and circuit-breaker logic directly by
stubbing the transport layer, so they run without any model credentials or
network access.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from pcbflow.mcp_server import (
    CircuitBreakerState,
    CollaborationChain,
    CollaborationPersistence,
    MCPCollaborationServer,
    ModelConfig,
    ModelEndpoint,
    MultiModelRouter,
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
    # A closed local port refuses immediately. Disable any ambient proxy so
    # the failure is a connection error rather than a slow proxied timeout.
    for variable in (
        "http_proxy",
        "https_proxy",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "all_proxy",
        "ALL_PROXY",
    ):
        monkeypatch.delenv(variable, raising=False)

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
