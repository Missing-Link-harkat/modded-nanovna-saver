"""Allowlist of the device_shell tools (MCP/shell_allowlist.py)."""

import pytest

from NanoVNASaver.MCP.registry import Risk
from NanoVNASaver.MCP.shell_allowlist import (
    ShellRefused,
    allowed_commands,
    check,
    check_input,
    classify,
    confirm_text,
)

READ = ["version", "info", "sweep-stats", "sd stats", "cal why",
        "msc log", "usb status", "sweep", "ref show", "power", "s21", "  version  "]
ACT = ["sweep 50000000 1500000000 201", "sweep 1000000 2000000",
       "sweep-avg 16", "ref on", "power lo 2", "cal off", "recall 1",
       "s21 on", "s21 off", "s21 norm", "s21 norm clear"]
PERSIST = ["save 2", "meas save", "meas note Antenna 3 test"]
NEVER = ["reset", "config-reset", "usb mode storage force", "usb reset",
         "sd read 0", "sd cs PB 9", "pin mode PA 6 1", "rf set 1 1 1 1 1",
         "i2c probe", "clk-set 0", "clk-drive 1", "test-mode on",
         "fs", "bootloader", "dfu", "flash", "capture", "cal open",
         "save 5", "sweep-avg 17", "save",
         # `cal on` sets the flag without checking for a valid calibration
         # (litevna-openvna#144): deliberately not listed
         "cal on"]
INPUT = ["input key 4f 00 down", "input key 28 0 up", "input ptr 100 -20 move",
         "input ptr 240 160 down", "input wheel -3", "  input wheel 5  "]


@pytest.mark.parametrize("risk,commands",
                         [(Risk.READ, READ), (Risk.ACT, ACT),
                          (Risk.PERSIST, PERSIST)])
def test_listed_commands_have_their_class(risk, commands):
    for cmd in commands:
        assert check(cmd, risk) == " ".join(cmd.split())


@pytest.mark.parametrize("cmd", NEVER)
def test_unlisted_commands_refused(cmd):
    with pytest.raises(ShellRefused):
        classify(cmd)


@pytest.mark.parametrize("cmd", [
    "version; reset", "version && reset", "version | cat", "info\nreset",
    "info\rreset", "version `reset`", "ver$(sion)", "info > x",
    "", "   ", "version " + "a" * 200, "versiön",
])
def test_injection_and_junk_refused(cmd):
    with pytest.raises(ShellRefused):
        classify(cmd)


def test_wrong_class_names_the_right_tool():
    with pytest.raises(ShellRefused, match="device_shell_act"):
        check("sweep-avg 4", Risk.READ)
    with pytest.raises(ShellRefused, match="device_shell_persist"):
        check("save 1", Risk.ACT)
    with pytest.raises(ShellRefused, match="device_shell"):
        check("version", Risk.PERSIST)


def test_every_rule_has_a_reason_and_class_lists_exist():
    from NanoVNASaver.MCP.shell_allowlist import RULES

    assert all(r.reason for r in RULES)
    for risk in (Risk.READ, Risk.ACT, Risk.PERSIST):
        assert allowed_commands(risk)


@pytest.mark.parametrize("cmd", INPUT)
def test_input_events_are_accepted_only_by_check_input(cmd):
    assert check_input(cmd) == " ".join(cmd.split())
    # the shell tools refuse them and name device_input (it has its own permission)
    for risk in (Risk.READ, Risk.ACT, Risk.PERSIST):
        with pytest.raises(ShellRefused, match="device_input"):
            check(cmd, risk)


@pytest.mark.parametrize("cmd", [
    "version", "sweep 1 2", "input", "input key", "input key zz 00 down",
    "input key 4f 00 sideways", "input ptr 1 2", "input wheel x", "input stats",
    "input key 4f 00 down; reset", "input key 4f 00 down\nreset",
    "input key 4f 00 down | cat", "reset", "save 1",
])
def test_check_input_refuses_everything_else(cmd):
    with pytest.raises(ShellRefused):
        check_input(cmd)


def test_input_stats_stays_a_read_command():
    assert check("input stats", Risk.READ) == "input stats"


def test_confirm_text_names_the_rule_reason():
    text = confirm_text("save 2")
    assert "save 2" in text
    assert "overwrites" in text  # the rule's own reason, not a generic sentence
    assert "SD" in confirm_text("meas save")
    # a command that is not even on the list still gets a readable text
    assert "reset" in confirm_text("reset")

