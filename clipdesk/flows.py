"""Persistent, user-scoped recipes built from ClipDesk's existing actions."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, Field, TypeAdapter


class FlowNotesStep(BaseModel):
    type: Literal["notes"] = "notes"
    enrichment: int = Field(default=0, ge=0, le=6)
    include_mermaid: bool = True
    include_timestamps: bool = True


class FlowCleanupStep(BaseModel):
    type: Literal["cleanup"] = "cleanup"
    remove_silence: bool = True
    remove_filler: bool = True
    remove_off_topic: bool = True
    remove_admin: bool = True
    remove_qa: bool = False
    remove_intro: bool = False
    remove_outro: bool = False
    output_name: str = Field(default="cleaned.mp4", min_length=1, max_length=180)


class FlowClipStep(BaseModel):
    type: Literal["clip"] = "clip"
    input_from: str = Field(default="source", min_length=1, max_length=180)
    start: float = Field(default=0.0, ge=0.0)
    end: float = Field(default=60.0, gt=0.0)
    title: str = Field(default="Flow clip", max_length=120)
    reframe: bool = False
    output_name: str = Field(default="clip.mp4", min_length=1, max_length=180)


class FlowHighlightStep(BaseModel):
    type: Literal["highlight"] = "highlight"
    mode: Literal["best", "topic"] = "best"
    query: str = Field(default="", max_length=300)
    target_seconds: float = Field(default=90.0, ge=10.0, le=7200.0)
    reframe: bool = True
    output_name: str = Field(default="highlight.mp4", min_length=1, max_length=180)


class FlowPromptStep(BaseModel):
    type: Literal["prompt"] = "prompt"
    input_from: str = Field(default="source", min_length=1, max_length=180)
    prompt: str = Field(min_length=2, max_length=2000)
    output_name: str = Field(default="prompt-edit.mp4", min_length=1, max_length=180)


class FlowBookendStep(BaseModel):
    type: Literal["intro", "outro"]
    source: Literal["generate", "local"] = "generate"
    input_from: str = Field(default="source", min_length=1, max_length=180)
    local_path: str = Field(default="", max_length=1000)
    style_id: str = "prestige"
    duration_seconds: float = Field(default=10.0, ge=5.0, le=60.0)
    shot_count: int = Field(default=5, ge=2, le=12)
    title: str = Field(default="", max_length=120)
    subtitle: str = Field(default="", max_length=200)
    audio_id: str = Field(default="elevate", max_length=220)
    include_final_message: bool = False
    final_message: str = Field(default="", max_length=120)
    output_name: str = Field(default="bookend.mp4", min_length=1, max_length=180)


class FlowAssembleStep(BaseModel):
    type: Literal["assemble"] = "assemble"
    input_from: str = Field(default="source", min_length=1, max_length=180)
    intro_transition: Literal["cut", "fade", "dissolve", "wipe-left", "slide-left"] = "fade"
    outro_transition: Literal["cut", "fade", "dissolve", "wipe-left", "slide-left"] = "fade"
    output_name: str = Field(default="final.mp4", min_length=1, max_length=180)


FlowStep = Annotated[
    FlowNotesStep
    | FlowCleanupStep
    | FlowClipStep
    | FlowHighlightStep
    | FlowPromptStep
    | FlowBookendStep
    | FlowAssembleStep,
    Field(discriminator="type"),
]


class FlowDefinition(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,79}$")
    name: str = Field(min_length=2, max_length=100)
    description: str = Field(default="", max_length=300)
    steps: list[FlowStep] = Field(min_length=1, max_length=20)

_FLOW_LIST = TypeAdapter(list[FlowDefinition])


def flows_path(state_dir: Path) -> Path:
    return state_dir / "flows.json"


def load_flows(state_dir: Path) -> list[FlowDefinition]:
    path = flows_path(state_dir)
    if not path.is_file():
        return []
    try:
        return _FLOW_LIST.validate_python(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, json.JSONDecodeError):
        return []


def _write_flows(state_dir: Path, flows: list[FlowDefinition]) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    destination = flows_path(state_dir)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".flows-", suffix=".json.tmp", dir=state_dir
    )
    temporary = Path(temporary_name)
    try:
        with open(descriptor, "w", encoding="utf-8", closefd=True) as handle:
            json.dump(
                [item.model_dump(mode="json") for item in flows],
                handle,
                indent=2,
            )
            handle.flush()
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def save_flow(state_dir: Path, flow: FlowDefinition) -> FlowDefinition:
    flows = [item for item in load_flows(state_dir) if item.id != flow.id]
    flows.append(flow)
    _write_flows(state_dir, flows)
    return flow


def delete_flow(state_dir: Path, flow_id: str) -> bool:
    existing = load_flows(state_dir)
    remaining = [item for item in existing if item.id != flow_id]
    if len(remaining) == len(existing):
        return False
    _write_flows(state_dir, remaining)
    return True