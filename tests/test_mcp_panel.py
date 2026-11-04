"""AI panel: the UI-control permission checkbox and its always-visible banner."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from NanoVNASaver.MCP.control import AIControl  # noqa: E402
from NanoVNASaver.MCP.panel import MCPPanel  # noqa: E402


class _Worker:
    def quit(self):
        pass


class _App(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.worker = _Worker()
        self.windows = {}


@pytest.fixture
def panel():
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    app = _App()
    control = AIControl()
    state = {"conn": None}
    control.connection = lambda: state["conn"]
    p = MCPPanel(app, control, lambda: None, 8765)
    yield p, control, state
    p.deleteLater()
    app.deleteLater()


def test_checkbox_is_disabled_until_ai_control_and_a_device(panel):
    p, control, state = panel
    assert not p.check_input.isEnabled()
    assert p.label_input.isHidden()
    control.allow(True)
    p.refresh()
    assert not p.check_input.isEnabled()          # still no device
    state["conn"] = object()
    p.refresh()
    assert p.check_input.isEnabled()
    assert p.label_input.isHidden()               # permission itself is not given


def test_banner_shows_while_the_permission_is_valid(panel):
    p, control, state = panel
    state["conn"] = object()
    control.allow(True)
    p.refresh()
    p.check_input.setChecked(True)                # user ticks the box
    assert control.input_allowed
    assert not p.label_input.isHidden()
    assert "KÄYTTÖLIITTYMÄN OHJAUS PÄÄLLÄ" in p.label_input.text()


def test_stop_and_connection_change_hide_the_banner(panel):
    p, control, state = panel
    state["conn"] = object()
    control.allow(True)
    p.refresh()
    p.check_input.setChecked(True)
    p.stop()                                      # STOP button
    p.refresh()
    assert not control.input_allowed
    assert p.label_input.isHidden() and not p.check_input.isChecked()
    control.allow(True)
    p.refresh()
    p.check_input.setChecked(True)
    state["conn"] = object()                      # reconnect
    p.refresh()
    assert p.label_input.isHidden() and not p.check_input.isChecked()
