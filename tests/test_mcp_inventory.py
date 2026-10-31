"""Every GUI control is either an MCP action or excluded with a reason."""

import re
from pathlib import Path

from NanoVNASaver.MCP import api  # noqa: F401  (fills the registry)
from NanoVNASaver.MCP.inventory import INVENTORY
from NanoVNASaver.MCP.registry import REGISTRY, Risk

SRC = Path(__file__).parent.parent / "src" / "NanoVNASaver"
SIGNAL = re.compile(r"\.(clicked|toggled|triggered)\.connect\(")


def gui_controls() -> set[str]:
    found = set()
    files = [SRC / "NanoVNASaver.py"]
    files += sorted((SRC / "Windows").glob("*.py"))
    files += sorted((SRC / "Controls").glob("*.py"))
    for path in files:
        rel = path.relative_to(SRC).as_posix()
        for line in path.read_text(encoding="utf-8").splitlines():
            if SIGNAL.search(line):
                found.add(f"{rel}: {line.strip()}")
    return found


def test_every_gui_control_is_in_inventory():
    missing = sorted(gui_controls() - INVENTORY.keys())
    assert not missing, (
        "GUI controls without MCP decision; add them to "
        "MCP/inventory.py:\n" + "\n".join(missing)
    )


def test_inventory_has_no_stale_entries():
    stale = sorted(INVENTORY.keys() - gui_controls())
    assert not stale, "Remove from MCP/inventory.py:\n" + "\n".join(stale)


def test_inventory_points_to_real_actions():
    for key, value in INVENTORY.items():
        if value.startswith("-"):
            assert len(value) > 3, f"{key}: give a reason"
            continue
        for name in value.split(","):
            assert name in REGISTRY, f"{key}: unknown action {name}"


def test_every_action_is_described():
    assert len(REGISTRY) >= 30
    for act in REGISTRY.values():
        assert len(act.description) > 20, act.name
        assert act.gui, f"{act.name}: name the GUI equivalent"
        if act.risk is Risk.PERSIST:
            assert act.confirm is not None, act.name


def test_no_firmware_or_flash_actions():
    for name in REGISTRY:
        assert not re.search(r"flash|firmware|dfu|bootloader", name), name
