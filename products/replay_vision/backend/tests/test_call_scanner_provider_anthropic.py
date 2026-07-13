from pathlib import Path
from typing import Any, cast

import pytest

from pydantic import BaseModel

from products.replay_vision.backend.temporal.activities.call_scanner_provider_anthropic import (
    VideoFrame,
    _extract_video_frames,
    _frame_content,
    _run_anthropic_step,
)
from products.replay_vision.backend.temporal.scanners.base import MissionStep


class _Core(BaseModel):
    verdict: str


class _ToolUse:
    type = "tool_use"

    def __init__(self, block_id: str, name: str, tool_input: dict[str, Any]) -> None:
        self.id = block_id
        self.name = name
        self.input = tool_input

    def model_dump(self, **kwargs: Any) -> dict[str, Any]:
        return {"type": self.type, "id": self.id, "name": self.name, "input": self.input}


class _Response:
    def __init__(self, *content: _ToolUse) -> None:
        self.content = list(content)


class _Messages:
    def __init__(self, responses: list[_Response]) -> None:
        self.responses = iter(responses)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> _Response:
        self.calls.append(kwargs)
        return next(self.responses)


class _Client:
    def __init__(self, responses: list[_Response]) -> None:
        self.messages = _Messages(responses)


async def _run_step(client: _Client, messages: list[dict[str, Any]], dispatch: Any):
    return await _run_anthropic_step(
        client=cast(Any, client),
        model="claude-test",
        system="system",
        messages=messages,
        step=MissionStep(name="core", instruction="classify", response_model=_Core),
        dispatch=dispatch,
        team_id=1,
        metric_labels={"provider": "anthropic", "model": "claude-test", "scanner_type": "monitor"},
    )


@pytest.mark.asyncio
async def test_runs_event_lookup_before_accepting_structured_result() -> None:
    client = _Client(
        [
            _Response(_ToolUse("lookup-1", "get_events_around", {"rec_t": 12})),
            _Response(_ToolUse("result-1", "submit_replay_vision_core", {"verdict": "yes"})),
        ]
    )
    messages: list[dict[str, Any]] = [{"role": "user", "content": "frames"}]
    lookups: list[dict[str, Any]] = []

    def dispatch(value: dict[str, Any]) -> dict[str, Any]:
        lookups.append(value)
        return {"events": []}

    result = await _run_step(client, messages, dispatch)

    assert result == _Core(verdict="yes")
    assert lookups == [{"rec_t": 12}]
    assert messages[2]["content"][0]["tool_use_id"] == "lookup-1"
    assert messages[-1]["content"][0]["tool_use_id"] == "result-1"


@pytest.mark.asyncio
async def test_rejects_invalid_structured_result_twice() -> None:
    client = _Client(
        [
            _Response(_ToolUse("result-1", "submit_replay_vision_core", {})),
            _Response(_ToolUse("result-2", "submit_replay_vision_core", {"wrong": "shape"})),
        ]
    )
    messages: list[dict[str, Any]] = [{"role": "user", "content": "frames"}]

    result = await _run_step(client, messages, lambda value: {})

    assert result is None
    assert messages[-1]["content"][0]["is_error"] is True


def test_frame_content_includes_timestamp_and_jpeg_data() -> None:
    content = _frame_content([VideoFrame(timestamp_seconds=7, content=b"jpeg")])

    assert content[1]["text"] == "Frame at approximately REC_T=7 seconds:"
    assert content[2]["source"] == {
        "type": "base64",
        "media_type": "image/jpeg",
        "data": "anBlZw==",
    }


def test_extract_video_frames_runs_one_ffmpeg_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: Any) -> None:
        commands.append(command)
        output_pattern = Path(command[-1])
        for index in range(1, 4):
            output_pattern.with_name(f"frame-{index:03d}.jpg").write_bytes(f"frame-{index}".encode())

    monkeypatch.setattr("subprocess.run", fake_run)

    frames = _extract_video_frames(b"video", "video/mp4", 5.2, max_frames=3, frame_width=1024)

    assert [frame.timestamp_seconds for frame in frames] == [0, 2, 3]
    assert [frame.content for frame in frames] == [b"frame-1", b"frame-2", b"frame-3"]
    assert len(commands) == 1
    assert commands[0][commands[0].index("-frames:v") + 1] == "3"
