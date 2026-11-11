"""UI-control permission of AIControl (device_input): bound to one device
connection, needs AI control, ends on STOP / reconnect / disconnect."""

from NanoVNASaver.MCP.control import AIControl


class Conn:
    """Stand-in for the VNA object: one object per device connection."""


def make():
    ctl = AIControl()
    state = {"conn": None}
    ctl.connection = lambda: state["conn"]
    return ctl, state


def test_default_is_off_and_needs_control_and_a_device():
    ctl, state = make()
    assert not ctl.input_allowed
    assert not ctl.allow_input(True)          # AI control is off, no device
    ctl.allow(True)
    assert not ctl.allow_input(True)          # no device connected
    state["conn"] = Conn()
    assert ctl.allow_input(True)
    assert ctl.input_allowed


def test_permission_is_separate_from_ai_control():
    ctl, state = make()
    state["conn"] = Conn()
    ctl.allow(True)
    assert ctl.allowed and not ctl.input_allowed   # AI control does not imply it


def test_stop_revokes_both():
    ctl, state = make()
    state["conn"] = Conn()
    ctl.allow(True)
    ctl.allow_input(True)
    ctl.stop()
    assert not ctl.allowed and not ctl.input_allowed
    ctl.allow(True)                                 # allowing AI control again ...
    assert not ctl.input_allowed                    # ... does not bring it back


def test_revoking_ai_control_revokes_ui_control():
    ctl, state = make()
    state["conn"] = Conn()
    ctl.allow(True)
    ctl.allow_input(True)
    ctl.allow(False)
    ctl.allow(True)
    assert not ctl.input_allowed


def test_new_connection_expires_it():
    ctl, state = make()
    state["conn"] = Conn()
    ctl.allow(True)
    ctl.allow_input(True)
    state["conn"] = Conn()                          # reconnect: a new VNA object
    assert not ctl.input_allowed
    assert ctl.allowed                              # AI control itself stays


def test_disconnect_expires_it():
    ctl, state = make()
    state["conn"] = Conn()
    ctl.allow(True)
    ctl.allow_input(True)
    state["conn"] = None
    assert not ctl.input_allowed


def test_user_can_revoke_and_grant_again():
    ctl, state = make()
    state["conn"] = Conn()
    ctl.allow(True)
    ctl.allow_input(True)
    ctl.allow_input(False)
    assert not ctl.input_allowed
    assert ctl.allow_input(True)
    assert ctl.input_allowed


def test_grant_and_revoke_are_logged_and_listeners_notified():
    ctl, state = make()
    notified = []
    ctl.listeners.append(lambda: notified.append(1))
    state["conn"] = Conn()
    ctl.allow(True)
    ctl.allow_input(True)
    ctl.stop()
    lines = [e.line() for e in ctl.entries if e.tool == "ui_control"]
    assert any("sallittu" in ln for ln in lines)
    assert any("päättyi" in ln and "AI-ohjaus otettu pois" in ln for ln in lines)
    assert notified
