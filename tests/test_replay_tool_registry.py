"""The replay tool registry: recorded results, real schemas, loud divergence."""

from __future__ import annotations

import pytest

from raven.agent.tools.base import Tool
from raven.agent.tools.registry import ToolRegistry
from raven.replay import cassette as cas
from raven.replay.tool_registry import ReplayToolRegistry, tool_registry_for_env

from tests.test_replay_cassette import _write


@pytest.fixture(autouse=True)
def _fresh_cassette_cache():
    cas._reset_for_tests()
    yield
    cas._reset_for_tests()


class _Spy(Tool):
    """A tool that records whether it was really run."""

    def __init__(self, name: str = "list_dir") -> None:
        self._name = name
        self.ran = False

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "list a directory"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}

    async def execute(self, **kwargs) -> str:
        self.ran = True
        return "REAL SIDE EFFECT"


def _registry(tmp_path, tools=None, tool_rows=None):
    reg = ReplayToolRegistry(cas.load_cassette(_write(tmp_path, tools=tool_rows)))
    for tool in tools or [_Spy()]:
        reg.register(tool)
    return reg


class TestExecution:
    async def test_returns_the_recorded_output_without_running_the_tool(self, tmp_path):
        spy = _Spy()
        reg = _registry(tmp_path, tools=[spy])
        assert await reg.execute("list_dir", {"path": "/w"}) == "setup.py"
        assert spy.ran is False

    async def test_output_is_a_string_the_loop_can_put_in_a_message(self, tmp_path):
        result = await _registry(tmp_path).execute("list_dir", {"path": "/w"})
        assert isinstance(result, str)
        assert getattr(result, "display_text", "missing") is None

    async def test_mangled_names_are_repaired_before_comparing(self, tmp_path):
        """Name repair is real behaviour on the live path, so replay must apply
        it too -- otherwise a case-mangled call would read as a divergence."""
        assert await _registry(tmp_path).execute("List_Dir", {"path": "/w"}) == "setup.py"

    async def test_a_different_tool_is_reported_as_divergence(self, tmp_path):
        reg = _registry(tmp_path, tools=[_Spy(), _Spy("read_file")])
        with pytest.raises(cas.ReplayToolMismatchError) as exc:
            await reg.execute("read_file", {"path": "/w"})
        msg = str(exc.value)
        assert "demo__demo-1" in msg and "list_dir" in msg and "read_file" in msg

    async def test_running_past_the_recording_raises(self, tmp_path):
        reg = _registry(tmp_path)
        await reg.execute("list_dir", {"path": "/w"})
        with pytest.raises(cas.ReplayExhaustedError):
            await reg.execute("list_dir", {"path": "/w"})


class TestSchemasStayReal:
    def test_definitions_come_from_the_registered_tools(self, tmp_path):
        """Tool schemas are a diff target: editing a description must change the
        replayed request, so they cannot be served from the cassette."""
        reg = _registry(tmp_path)
        names = [d["function"]["name"] for d in reg.get_definitions()]
        assert names == ["list_dir"]
        assert "list a directory" in reg.get_definitions()[0]["function"]["description"]

    def test_registry_interface_is_intact(self, tmp_path):
        reg = _registry(tmp_path)
        assert reg.has("list_dir") and "list_dir" in reg and len(reg) == 1
        assert reg.tool_names == ["list_dir"]
        assert reg.canonical_name("LIST_DIR") == "list_dir"
        assert reg.get("list_dir") is not None


class TestWorkspaceDirtyFlag:
    async def test_successful_write_marks_the_workspace_dirty(self, tmp_path):
        """The completion gates probe git for changes. Replay writes nothing, so
        without this flag the gate would always see a clean tree and refuse to
        let the recorded trajectory finish."""
        rows = [{"idx": 0, "tool_call_id": "c1", "name": "write_file", "content": "Wrote 12 lines"}]
        reg = _registry(tmp_path, tools=[_Spy("write_file")], tool_rows=rows)
        assert reg.workspace_dirty is False
        await reg.execute("write_file", {"path": "/w"})
        assert reg.workspace_dirty is True

    async def test_failed_write_leaves_it_clean(self, tmp_path):
        rows = [{"idx": 0, "tool_call_id": "c1", "name": "write_file", "content": "Error: no such file"}]
        reg = _registry(tmp_path, tools=[_Spy("write_file")], tool_rows=rows)
        await reg.execute("write_file", {"path": "/w"})
        assert reg.workspace_dirty is False

    async def test_read_only_tools_leave_it_clean(self, tmp_path):
        reg = _registry(tmp_path)
        await reg.execute("list_dir", {"path": "/w"})
        assert reg.workspace_dirty is False


class TestEnvGate:
    def test_without_a_cassette_the_real_registry_is_used(self, monkeypatch):
        monkeypatch.delenv(cas.ENV_CASSETTE, raising=False)
        reg = tool_registry_for_env()
        assert type(reg) is ToolRegistry

    def test_with_a_cassette_the_replay_registry_is_used(self, tmp_path, monkeypatch):
        monkeypatch.setenv(cas.ENV_CASSETTE, str(_write(tmp_path)))
        assert isinstance(tool_registry_for_env(), ReplayToolRegistry)
