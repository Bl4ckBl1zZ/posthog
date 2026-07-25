"""Run Replay Vision scanners against timestamped recording frames using Anthropic Messages."""

from __future__ import annotations

import json
import math
import time
import base64
import asyncio
import tempfile
import subprocess
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from django.conf import settings

import anthropic
import structlog
from anthropic.types import MessageParam, ToolParam
from anthropic.types.tool_choice_param import ToolChoiceParam
from asgiref.sync import sync_to_async
from pydantic import BaseModel, ValidationError

from posthog.storage import object_storage

from products.exports.backend.models.exported_asset import ExportedAsset
from products.replay_vision.backend.temporal.constants import replay_vision_distinct_id
from products.replay_vision.backend.temporal.errors import FailureKind, ScannerFailureError
from products.replay_vision.backend.temporal.events_tool import (
    GET_EVENTS_TOOL_NAME,
    build_events_index,
    dispatch_events_tool,
)
from products.replay_vision.backend.temporal.metrics import REPLAY_VISION_PROVIDER_CALL
from products.replay_vision.backend.temporal.scanners.base import (
    BaseScanner,
    BaseScannerOutput,
    MissionStep,
    SignalFinding,
)
from products.replay_vision.backend.temporal.types import ScannerLlmInputs, ScannerSnapshot

logger = structlog.get_logger(__name__)

_MAX_LLM_ATTEMPTS = 2
_MAX_TOOL_ROUNDS = 8
_MAX_RAW_FRAME_BYTES = 16 * 1024 * 1024
_RESULT_TOOL_PREFIX = "submit_replay_vision_"
_ASSET_READ_ATTEMPTS = 5


@dataclass(frozen=True)
class VideoFrame:
    timestamp_seconds: int
    content: bytes


async def run_anthropic_mission(
    *,
    scanner: BaseScanner,
    snapshot: ScannerSnapshot,
    preamble_text: str,
    team_id: int,
    llm_inputs: ScannerLlmInputs,
    asset_id: int,
) -> tuple[BaseScannerOutput, list[SignalFinding]]:
    api_key = str(getattr(settings, "ANTHROPIC_API_KEY", ""))
    if not api_key:
        raise ScannerFailureError(
            "ANTHROPIC_API_KEY is required for Replay Vision's Anthropic provider",
            kind=FailureKind.INTERNAL_ERROR,
        )

    video_bytes, mime_type = await _load_video_asset(asset_id)
    frames = await asyncio.to_thread(
        _extract_video_frames,
        video_bytes,
        mime_type,
        llm_inputs.metadata.duration_seconds,
        max(1, min(int(getattr(settings, "REPLAY_VISION_ANTHROPIC_MAX_FRAMES", 20)), 40)),
        max(320, min(int(getattr(settings, "REPLAY_VISION_ANTHROPIC_FRAME_WIDTH", 1024)), 1920)),
    )
    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": _frame_content(frames),
        }
    ]
    events_index = build_events_index(llm_inputs)

    def dispatch(tool_input: dict[str, Any]) -> dict[str, Any]:
        call = SimpleNamespace(name=GET_EVENTS_TOOL_NAME, args=tool_input)
        return dispatch_events_tool(call, events_index)

    model = str(getattr(settings, "REPLAY_VISION_ANTHROPIC_MODEL", "claude-3-5-sonnet-20241022"))
    client = anthropic.AsyncAnthropic(api_key=api_key, timeout=120.0)
    metric_labels = {
        "provider": "anthropic",
        "model": model,
        "scanner_type": snapshot.scanner_type.value,
    }

    step_outputs: dict[str, BaseModel] = {}
    for step in scanner.mission_steps():
        checkpoint = len(messages)
        messages.append(
            {
                "role": "user",
                "content": (
                    f"{step.instruction}\n\n"
                    f"When ready, call `{_result_tool_name(step)}` with the complete structured answer."
                ),
            }
        )
        parsed = await _run_anthropic_step(
            client=client,
            model=model,
            system=preamble_text,
            messages=messages,
            step=step,
            dispatch=dispatch,
            team_id=team_id,
            metric_labels=metric_labels,
        )
        if parsed is None:
            del messages[checkpoint:]
            if step.required:
                raise ScannerFailureError(
                    f"Required step '{step.name}' rejected after {_MAX_LLM_ATTEMPTS} attempts",
                    kind=FailureKind.VALIDATION_FAILED,
                )
            continue
        step_outputs[step.name] = parsed

    return scanner.assemble(step_outputs)


async def _run_anthropic_step(
    *,
    client: anthropic.AsyncAnthropic,
    model: str,
    system: str,
    messages: list[dict[str, Any]],
    step: MissionStep,
    dispatch: Any,
    team_id: int,
    metric_labels: dict[str, str],
) -> BaseModel | None:
    result_tool_name = _result_tool_name(step)
    tools = [_events_tool(), _result_tool(step)]
    validation_attempts = 0

    for round_number in range(_MAX_TOOL_ROUNDS):
        started = time.monotonic()
        try:
            response = await client.messages.create(
                model=model,
                max_tokens=4096,
                system=system,
                messages=cast(list[MessageParam], messages),
                tools=cast(list[ToolParam], tools),
                tool_choice=cast(
                    ToolChoiceParam,
                    (
                        {"type": "tool", "name": result_tool_name}
                        if round_number == _MAX_TOOL_ROUNDS - 1
                        else {"type": "any"}
                    ),
                ),
                metadata={"user_id": replay_vision_distinct_id(team_id)},
            )
        except anthropic.APIConnectionError as exc:
            REPLAY_VISION_PROVIDER_CALL.labels(**metric_labels, outcome="provider_error").observe(
                time.monotonic() - started
            )
            raise ScannerFailureError(str(exc), kind=FailureKind.PROVIDER_TRANSIENT) from exc
        except anthropic.APIStatusError as exc:
            REPLAY_VISION_PROVIDER_CALL.labels(**metric_labels, outcome="provider_error").observe(
                time.monotonic() - started
            )
            status_code = exc.status_code
            transient = status_code in {408, 409, 429} or status_code >= 500
            kind = FailureKind.PROVIDER_TRANSIENT if transient else FailureKind.PROVIDER_REJECTED
            raise ScannerFailureError(str(exc), kind=kind) from exc

        assistant_content = [_content_block_param(block) for block in response.content]
        messages.append({"role": "assistant", "content": assistant_content})

        tool_results: list[dict[str, Any]] = []
        parsed_result: BaseModel | None = None
        validation_error: str | None = None
        for block in response.content:
            if block.type != "tool_use":
                continue
            name = block.name
            tool_input = dict(block.input)
            if name == GET_EVENTS_TOOL_NAME:
                result = dispatch(tool_input)
                tool_results.append(_tool_result(block.id, result))
            elif name == result_tool_name:
                parsed_result, validation_error = _parse_and_validate(step, tool_input)
                if validation_error is None:
                    tool_results.append(_tool_result(block.id, {"accepted": True}))
                else:
                    validation_attempts += 1
                    tool_results.append(_tool_result(block.id, {"error": validation_error}, is_error=True))
            else:
                tool_results.append(_tool_result(block.id, {"error": f"unknown tool: {name}"}, is_error=True))

        if tool_results:
            messages.append({"role": "user", "content": tool_results})

        outcome = "ok" if parsed_result is not None else "validation_failed" if validation_error else "ok"
        REPLAY_VISION_PROVIDER_CALL.labels(**metric_labels, outcome=outcome).observe(time.monotonic() - started)

        if parsed_result is not None:
            return parsed_result
        if validation_error is not None:
            logger.warning(
                "replay_vision.call_scanner_provider_anthropic.invalid_response",
                step=step.name,
                attempt=validation_attempts,
                error=validation_error,
            )
            if validation_attempts >= _MAX_LLM_ATTEMPTS:
                return None
        elif not tool_results:
            messages.append(
                {
                    "role": "user",
                    "content": f"Call `{result_tool_name}` now with the complete structured answer.",
                }
            )

    logger.warning("replay_vision.call_scanner_provider_anthropic.tool_rounds_exhausted", step=step.name)
    return None


async def _load_video_asset(asset_id: int) -> tuple[bytes, str]:
    asset = await ExportedAsset.objects.aget(id=asset_id)
    if asset.content:
        content = bytes(asset.content)
    elif asset.content_location:
        content = b""
        for attempt in range(_ASSET_READ_ATTEMPTS):
            content = (
                await sync_to_async(object_storage.read_bytes, thread_sensitive=False)(
                    asset.content_location, missing_ok=True
                )
                or b""
            )
            if content:
                break
            if attempt < _ASSET_READ_ATTEMPTS - 1:
                delay_seconds = 2**attempt
                logger.warning(
                    "replay_vision.asset_not_visible_yet",
                    asset_id=asset_id,
                    content_location=asset.content_location,
                    attempt=attempt + 1,
                    retry_in_seconds=delay_seconds,
                )
                await asyncio.sleep(delay_seconds)
    else:
        raise ScannerFailureError(
            f"ExportedAsset {asset_id} has neither content nor content_location",
            kind=FailureKind.INTERNAL_ERROR,
        )
    if not content:
        raise ScannerFailureError(f"ExportedAsset {asset_id} is empty", kind=FailureKind.INTERNAL_ERROR)
    return content, asset.export_format


def _extract_video_frames(
    video_bytes: bytes,
    mime_type: str,
    duration_seconds: float,
    max_frames: int,
    frame_width: int,
) -> list[VideoFrame]:
    duration = max(float(duration_seconds), 0.1)
    sample_count = max(1, min(max_frames, math.ceil(duration)))
    interval = duration / sample_count
    frame_rate = 1 / interval
    suffix = ".webm" if mime_type == "video/webm" else ".mp4"

    with tempfile.TemporaryDirectory(prefix="replay-vision-") as directory:
        input_path = Path(directory) / f"recording{suffix}"
        output_pattern = Path(directory) / "frame-%03d.jpg"
        input_path.write_bytes(video_bytes)
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(input_path),
            "-vf",
            f"fps={frame_rate:.8f},scale='min({frame_width},iw)':-2",
            "-frames:v",
            str(sample_count),
            "-q:v",
            "5",
            str(output_pattern),
        ]
        try:
            subprocess.run(command, check=True, capture_output=True, timeout=300)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            stderr = getattr(exc, "stderr", b"") or b""
            detail = stderr.decode("utf-8", errors="replace")[-500:]
            raise ScannerFailureError(
                f"Could not extract Replay Vision frames with ffmpeg: {detail or type(exc).__name__}",
                kind=FailureKind.INTERNAL_ERROR,
            ) from exc

        paths = sorted(Path(directory).glob("frame-*.jpg"))
        if not paths:
            raise ScannerFailureError(
                "ffmpeg produced no Replay Vision frames",
                kind=FailureKind.INTERNAL_ERROR,
            )
        frames = [
            VideoFrame(timestamp_seconds=round(index * interval), content=path.read_bytes())
            for index, path in enumerate(paths)
        ]
    return _fit_frame_budget(frames)


def _fit_frame_budget(frames: list[VideoFrame]) -> list[VideoFrame]:
    while len(frames) > 2 and sum(len(frame.content) for frame in frames) > _MAX_RAW_FRAME_BYTES:
        frames = frames[::2] + ([frames[-1]] if frames[-1] not in frames[::2] else [])
    return frames


def _frame_content(frames: list[VideoFrame]) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": (
                "These are ordered frames sampled across the complete session recording. "
                "Each frame is labeled with its approximate recording-relative timestamp."
            ),
        }
    ]
    for frame in frames:
        content.extend(
            [
                {"type": "text", "text": f"Frame at approximately REC_T={frame.timestamp_seconds} seconds:"},
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": base64.b64encode(frame.content).decode("ascii"),
                    },
                },
            ]
        )
    return content


def _events_tool() -> dict[str, Any]:
    return {
        "name": GET_EVENTS_TOOL_NAME,
        "description": (
            "Look up analytics events around a recording moment. rec_t is the whole number of seconds since the "
            "recording started. Use this to verify rage clicks, dead clicks, exceptions, and other event context."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "rec_t": {"type": "integer", "description": "Recording-relative time in seconds."},
                "window_s": {"type": "integer", "description": "Half-window in seconds; defaults to 10."},
            },
            "required": ["rec_t"],
        },
    }


def _result_tool(step: MissionStep) -> dict[str, Any]:
    return {
        "name": _result_tool_name(step),
        "description": "Submit the complete structured result for this Replay Vision analysis step.",
        "input_schema": step.response_model.model_json_schema(),
    }


def _result_tool_name(step: MissionStep) -> str:
    safe_name = "".join(character if character.isalnum() or character == "_" else "_" for character in step.name)
    return f"{_RESULT_TOOL_PREFIX}{safe_name}"[:64]


def _parse_and_validate(step: MissionStep, value: dict[str, Any]) -> tuple[BaseModel | None, str | None]:
    try:
        parsed = step.response_model.model_validate(value)
    except ValidationError as exc:
        return None, f"Schema validation failed: {exc}"
    if step.validate is not None:
        semantic_error = step.validate(parsed)
        if semantic_error is not None:
            return None, f"Semantic validation failed: {semantic_error}"
    return parsed, None


def _content_block_param(block: Any) -> dict[str, Any]:
    if hasattr(block, "model_dump"):
        return block.model_dump(mode="json", exclude_none=True)
    return dict(block)


def _tool_result(tool_use_id: str, value: dict[str, Any], *, is_error: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": json.dumps(value, separators=(",", ":"), default=str),
    }
    if is_error:
        result["is_error"] = True
    return result


__all__ = ["run_anthropic_mission"]
