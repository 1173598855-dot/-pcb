from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
import uvicorn
from fastapi.encoders import jsonable_encoder

from pcbflow.api import create_app
from pcbflow.config import Settings
from pcbflow.container import Container, build_container

app = typer.Typer(no_args_is_help=True)
project_app = typer.Typer(no_args_is_help=True)
task_app = typer.Typer(no_args_is_help=True)
app.add_typer(project_app, name="project")
app.add_typer(task_app, name="task")


def _build() -> Container:
    return build_container(Settings.from_env())


def _emit(value: object, json_output: bool, human: str) -> None:
    if json_output:
        typer.echo(
            json.dumps(
                jsonable_encoder(value),
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
    else:
        typer.echo(human)


@app.command()
def doctor(
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    container = _build()
    try:
        capability = container.kicad.probe()
        _emit(
            {"kicad_cli": capability},
            json_output,
            "KiCad CLI available" if capability.available else "KiCad CLI unavailable",
        )
    finally:
        container.engine.dispose()


@project_app.command("add")
def project_add(
    path: Path,
    name: Annotated[str, typer.Option("--name")],
    idempotency_key: Annotated[str, typer.Option("--idempotency-key")],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    container = _build()
    try:
        project = container.projects.create(name, path, idempotency_key)
        _emit(project, json_output, project.id)
    finally:
        container.engine.dispose()


@project_app.command("list")
def project_list(
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    container = _build()
    try:
        projects = container.projects.list()
        _emit(projects, json_output, f"{len(projects)} project(s)")
    finally:
        container.engine.dispose()


@app.command("validate")
def validate_project(
    project_id: str,
    idempotency_key: Annotated[str, typer.Option("--idempotency-key")],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    container = _build()
    try:
        task = container.validation.enqueue(project_id, idempotency_key)
        _emit(task, json_output, task.id)
    finally:
        container.engine.dispose()


@app.command("worker")
def run_worker(
    once: Annotated[bool, typer.Option("--once")] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    if not once:
        raise typer.BadParameter("only --once is supported")
    container = _build()
    try:
        handled = container.worker.run_once()
        _emit({"handled": handled}, json_output, "handled" if handled else "idle")
    finally:
        container.engine.dispose()


@task_app.command("show")
def task_show(
    task_id: str,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    container = _build()
    try:
        task = container.tasks.get(task_id)
        _emit(task, json_output, f"{task.id}: {task.status.value}")
    finally:
        container.engine.dispose()


@app.command("findings")
def list_findings(
    project_id: str,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    container = _build()
    try:
        container.projects.get(project_id)
        findings = container.findings.list_for_project(project_id)
        _emit(findings, json_output, f"{len(findings)} finding(s)")
    finally:
        container.engine.dispose()


@app.command("serve")
def serve(
    host: Annotated[str, typer.Option("--host")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", min=1, max=65535)] = 8765,
) -> None:
    container = _build()
    try:
        uvicorn.run(create_app(container), host=host, port=port)
    finally:
        container.engine.dispose()
