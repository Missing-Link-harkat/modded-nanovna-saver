"""SaverMCP.can_ask and the guard around the framework internals used by
_forbid_unknown_arguments (review of Saver PR #13)."""

from types import SimpleNamespace

import pytest

pytest.importorskip("mcp")

from NanoVNASaver.MCP.server import _ASK_IN_GUI, SaverMCP  # noqa: E402


def _server(gui_confirm=None):
    return SimpleNamespace(gui_confirm=gui_confirm)


def test_framework_asked_client_answered():
    answer = SimpleNamespace(action="accept", data=SimpleNamespace(ok=True))
    assert SaverMCP.can_ask(_server(), None, answer) is True


def test_answer_says_ask_in_gui_but_headless():
    # the client does not elicit, so the resolver said "ask in the GUI",
    # and there is no GUI: nobody can be asked
    answer = SimpleNamespace(action="accept", data=_ASK_IN_GUI)
    assert SaverMCP.can_ask(_server(), None, answer) is False


def test_answer_says_ask_in_gui_with_window():
    answer = SimpleNamespace(action="accept", data=_ASK_IN_GUI)
    assert SaverMCP.can_ask(_server(lambda message: True), None, answer) is True


def test_no_answer_no_ctx_depends_on_window():
    assert SaverMCP.can_ask(_server(), None) is False
    assert SaverMCP.can_ask(_server(lambda message: True), None) is True


def test_missing_tool_manager_is_a_clear_error():
    fake = SimpleNamespace(server=object())
    with pytest.raises(RuntimeError, match="_tool_manager"):
        SaverMCP._forbid_unknown_arguments(fake, "sweep_run")


def test_missing_arg_model_is_a_clear_error():
    manager = SimpleNamespace(get_tool=lambda name: SimpleNamespace())
    fake = SimpleNamespace(server=SimpleNamespace(_tool_manager=manager))
    with pytest.raises(RuntimeError, match="arg_model"):
        SaverMCP._forbid_unknown_arguments(fake, "sweep_run")


def test_unregistered_tool_is_a_clear_error():
    manager = SimpleNamespace(get_tool=lambda name: None)
    fake = SimpleNamespace(server=SimpleNamespace(_tool_manager=manager))
    with pytest.raises(RuntimeError, match="ei löytynyt"):
        SaverMCP._forbid_unknown_arguments(fake, "sweep_run")
