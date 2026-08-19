"""Export eval case and trajectory sidecar models to canonical schemas.

Committed and drift checked exactly like the plugin format schema. Run as
``python -m curie_worker.eval.schema_export`` to rewrite both artifacts.
"""

import json
from pathlib import Path
from typing import Any

from pydantic.json_schema import models_json_schema

from .models import EvalCase, EvalSuite, Grader
from .trajectory import TrajectorySidecar

_MODELS = (EvalSuite, EvalCase, Grader)

SCHEMA_ID = "https://curietech.ai/schemas/eval-cases.schema.json"
TRAJECTORY_SCHEMA_ID = "https://curietech.ai/schemas/trajectory.schema.json"


def schema_path() -> Path:
    """The committed schema file location inside apps/worker."""

    return Path(__file__).resolve().parents[3] / "schema" / "eval-cases.schema.json"


def build_schema() -> dict[str, Any]:
    """Build the combined JSON Schema document for the eval-case models."""

    _, top = models_json_schema(
        [(model, "validation") for model in _MODELS],
        ref_template="#/$defs/{model}",
    )
    doc: dict[str, Any] = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": SCHEMA_ID,
        "title": "Curie Eval Case Format",
    }
    doc.update(top)
    return doc


def render_schema() -> str:
    """Render the canonical schema string (sorted keys, trailing newline)."""

    return json.dumps(build_schema(), indent=2, sort_keys=True) + "\n"


def write_schema() -> Path:
    """Write the canonical schema to its committed location and return the path."""

    path = schema_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_schema(), encoding="utf-8")
    return path


def trajectory_schema_path() -> Path:
    """The committed trajectory sidecar schema location."""

    return Path(__file__).resolve().parents[3] / "schema" / "trajectory.schema.json"


def build_trajectory_schema() -> dict[str, Any]:
    """Build the JSON Schema document for the trajectory sidecar."""

    generated = TrajectorySidecar.model_json_schema(ref_template="#/$defs/{model}")
    generated["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    generated["$id"] = TRAJECTORY_SCHEMA_ID
    generated["title"] = "Curie Trajectory Sidecar"
    return generated


def render_trajectory_schema() -> str:
    """Render the canonical trajectory sidecar schema."""

    return json.dumps(build_trajectory_schema(), indent=2, sort_keys=True) + "\n"


def write_trajectory_schema() -> Path:
    """Write the trajectory schema and return its path."""

    path = trajectory_schema_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_trajectory_schema(), encoding="utf-8")
    return path


if __name__ == "__main__":
    for written in (write_schema(), write_trajectory_schema()):
        print(f"wrote {written}")
