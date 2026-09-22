"""Real KiCad manufacturing export.

This module drives ``kicad-cli`` to produce genuine manufacturing outputs from
an adopted KiCad board: Gerbers (including inner copper layers), an Excellon
drill file, a position/CPL file, a schematic BOM, and a machine-readable DRC
report. It deliberately does **not** modify the source board: every command
below reads the board and writes only into a caller-supplied output directory.

It is read-only with respect to the design. Writing BoardIR back into a
``.kicad_pcb`` is a separate, later increment.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pcbflow.process import ProcessPort, ProcessTimeoutError


class KicadExportError(RuntimeError):
    """A ``kicad-cli`` export command failed or produced no usable output."""

    def __init__(self, command: str, returncode: int, stderr: str) -> None:
        super().__init__(f"kicad-cli {command} failed with exit code {returncode}")
        self.command = command
        self.returncode = returncode
        self.stderr = stderr


class KicadExportUnavailableError(RuntimeError):
    """No usable ``kicad-cli`` executable is configured."""


@dataclass(frozen=True, slots=True)
class KicadExportRequest:
    """One board/schematic export job inside an isolated output directory."""

    board_file: Path
    schematic_file: Path | None
    copper_layers: tuple[str, ...] = ("F.Cu", "B.Cu")

    def __post_init__(self) -> None:
        if not self.copper_layers:
            raise ValueError("copper_layers must not be empty")
        if any(
            not isinstance(layer, str) or not layer.strip()
            for layer in self.copper_layers
        ):
            raise ValueError("copper_layers must contain nonblank layer names")


@dataclass(frozen=True, slots=True)
class KicadExportResult:
    """Paths to the files the exporter actually wrote."""

    gerber_dir: Path
    gerber_files: tuple[Path, ...]
    drill_files: tuple[Path, ...]
    position_file: Path | None
    bom_file: Path | None
    drc_report: Path

    def all_files(self) -> tuple[Path, ...]:
        files = [*self.gerber_files, *self.drill_files, self.drc_report]
        if self.position_file is not None:
            files.append(self.position_file)
        if self.bom_file is not None:
            files.append(self.bom_file)
        return tuple(files)


class KicadManufacturingExporter:
    """Drive ``kicad-cli`` to emit real manufacturing artifacts."""

    def __init__(
        self,
        runner: ProcessPort,
        executable: Path | None,
        timeout_seconds: float = 120,
    ) -> None:
        self._runner = runner
        self._executable = executable.resolve() if executable is not None else None
        self._timeout_seconds = timeout_seconds

    @property
    def executable(self) -> Path | None:
        return self._executable

    def _require_executable(self) -> Path:
        if self._executable is None or not self._executable.is_file():
            raise KicadExportUnavailableError("kicad-cli executable is unavailable")
        return self._executable

    def _run(self, argv: list[str], cwd: Path) -> None:
        command = argv[1] + " " + argv[2] if len(argv) > 2 else argv[1]
        try:
            result = self._runner.run(argv, cwd, self._timeout_seconds)
        except ProcessTimeoutError as error:
            raise KicadExportError(command, -1, "export timed out") from error
        if result.returncode != 0:
            raise KicadExportError(command, result.returncode, result.stderr)

    def export(self, request: KicadExportRequest, output_dir: Path) -> KicadExportResult:
        executable = self._require_executable()
        board = request.board_file.resolve()
        if not board.is_file():
            raise KicadExportError("pcb export", -1, f"board not found: {board}")
        output = output_dir.resolve()
        gerber_dir = output / "gerbers"
        gerber_dir.mkdir(parents=True, exist_ok=True)
        drill_dir = output / "drill"
        drill_dir.mkdir(parents=True, exist_ok=True)

        layer_list = ",".join(request.copper_layers)
        self._run(
            [
                str(executable),
                "pcb",
                "export",
                "gerbers",
                "--output",
                str(gerber_dir),
                "--layers",
                layer_list,
                str(board),
            ],
            gerber_dir,
        )

        self._run(
            [
                str(executable),
                "pcb",
                "export",
                "drill",
                "--output",
                str(drill_dir),
                "--format",
                "excellon",
                str(board),
            ],
            drill_dir,
        )

        position_file: Path | None = None
        position = output / "positions.csv"
        try:
            self._run(
                [
                    str(executable),
                    "pcb",
                    "export",
                    "pos",
                    "--output",
                    str(position),
                    "--format",
                    "csv",
                    str(board),
                ],
                output,
            )
        except KicadExportError:
            position_file = None
        else:
            position_file = position if position.is_file() else None

        bom_file: Path | None = None
        if request.schematic_file is not None:
            schematic = request.schematic_file.resolve()
            if schematic.is_file():
                candidate = output / "bom.csv"
                try:
                    self._run(
                        [
                            str(executable),
                            "sch",
                            "export",
                            "bom",
                            "--output",
                            str(candidate),
                            str(schematic),
                        ],
                        output,
                    )
                except KicadExportError:
                    bom_file = None
                else:
                    bom_file = candidate if candidate.is_file() else None

        drc_report = output / "drc.json"
        self._run(
            [
                str(executable),
                "pcb",
                "drc",
                "--format",
                "json",
                "--output",
                str(drc_report),
                str(board),
            ],
            output,
        )
        if not drc_report.is_file():
            raise KicadExportError("pcb drc", 0, "DRC report was not created")

        gerber_files = tuple(sorted(gerber_dir.iterdir()))
        drill_files = tuple(sorted(drill_dir.iterdir()))
        if not gerber_files:
            raise KicadExportError("pcb export gerbers", 0, "no Gerber files were produced")
        if not drill_files:
            raise KicadExportError("pcb export drill", 0, "no drill files were produced")

        return KicadExportResult(
            gerber_dir=gerber_dir,
            gerber_files=gerber_files,
            drill_files=drill_files,
            position_file=position_file,
            bom_file=bom_file,
            drc_report=drc_report,
        )


__all__ = [
    "KicadExportError",
    "KicadExportRequest",
    "KicadExportResult",
    "KicadExportUnavailableError",
    "KicadManufacturingExporter",
]
