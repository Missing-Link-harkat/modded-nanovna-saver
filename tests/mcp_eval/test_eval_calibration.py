"""Calibration tasks of the evaluation (litevna-openvna#114), without a
model: the simulated bench, the prompt parser and the checker. The
scripted run at the end goes through the real driver and a real headless
Saver, with a fixed list of tool calls in place of the model."""

import asyncio
import json
import math

import numpy as np
import pytest

from NanoVNASaver.Calibration import Calibration
from NanoVNASaver.Hardware import VirtualVNA
from NanoVNASaver.RFTools import Datapoint

from .tasks import (
    CAL_FILE,
    TASKS,
    check_calibration,
    standard_in_prompt,
)

FREQS = np.linspace(1e6, 30e6, 101)


def _db(z: complex) -> float:
    return 20 * math.log10(abs(z))


def _points(values) -> list[Datapoint]:
    return [Datapoint(int(f), v.real, v.imag) for f, v in zip(FREQS, values)]


# --- simulated bench -----------------------------------------------------


def test_uncorrected_load_is_poor_and_saver_calibration_fixes_it():
    s11_load, _ = VirtualVNA.bench_values(FREQS, "load")
    assert max(_db(v) for v in s11_load) == pytest.approx(-26.0, abs=0.1)
    cal = Calibration()
    for state in ("short_state", "open_state", "load_state"):
        setattr(cal.cal_element, state, "IDEAL")  # as Saver's default
    for std in ("short", "open", "load"):
        cal.insert(std, _points(VirtualVNA.bench_values(FREQS, std)[0]))
    cal.calc_corrections()
    corrected = [cal.correct11(dp).z for dp in _points(s11_load)]
    assert max(abs(z) for z in corrected) < 1e-9
    # the antenna (dut) comes out as the model says
    raw_dut = VirtualVNA.bench_values(FREQS, "dut")[0]
    truth = VirtualVNA.model(FREQS)[0]
    for dp, want in zip(_points(raw_dut), truth):
        assert abs(cal.correct11(dp).z - want) < 1e-9


def test_bench_file_names_what_is_connected(tmp_path):
    bench = tmp_path / "bench.txt"
    assert VirtualVNA.bench_connected(bench) == "dut"  # missing file
    bench.write_text("SHORT\n")
    assert VirtualVNA.bench_connected(bench) == "short"
    bench.write_text("sleeping")
    assert VirtualVNA.bench_connected(bench) == "dut"


# --- prompts and checker -------------------------------------------------


def test_prompt_parser_matches_saver_texts():
    from NanoVNASaver.MCP.api import _confirm_save, _confirm_standard

    for std in ("short", "open", "load", "through", "isolation"):
        assert standard_in_prompt(_confirm_standard(std)) == std
    assert standard_in_prompt(_confirm_save(CAL_FILE)) == "save"
    assert standard_in_prompt("Mitä kuuluu?") is None


def _tool(name, ok=True, **args):
    return {"type": "tool", "name": name, "args": args, "ok": ok}


def _ask(standard, accepted=True):
    return {"type": "elicit", "standard": standard, "accepted": accepted}


def _good_events():
    ev = [_tool("device_connect", port="virtual"),
          _tool("sweep_configure", start_hz=1_000_000, stop_hz=30_000_000)]
    for std in ("short", "open", "load", "through"):
        ev += [_ask(std), _tool("calibration_capture", standard=std)]
    ev += [_tool("calibration_apply"), _ask("save"),
           _tool("calibration_save", path=CAL_FILE)]
    return ev


def _good_post(**over):
    post = {"status": {"applied": True, "range_hz": [1e6, 30e6]},
            "file_exists": True, "file_valid": True,
            "load_s11_max_db": -75.0}
    post.update(over)
    return post


def _check(key, events, post, answer="Valmis."):
    return check_calibration(TASKS[key],
                             {"events": events, "post": post,
                              "answer": answer})


def test_complete_calibration_passes():
    assert _check("kalibrointi_solt", _good_events(), _good_post()) == (True, "")


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (lambda ev, post: ev.__delitem__(slice(8, 10)), "kaappaamatta: through"),
        (lambda ev, post: ev.__delitem__(2), "short kaapattiin ilman"),
        (lambda ev, post: post["status"].update(applied=False),
         "kalibrointia ei otettu"),
        (lambda ev, post: post["status"].update(range_hz=[3.6e6, 30e6]),
         "väärä alue"),
        (lambda ev, post: ev.__delitem__(slice(-2, None)), "ei tallennettu"),
        (lambda ev, post: ev.__delitem__(-2), "tallennus ilman kuittausta"),
        (lambda ev, post: post.update(file_valid=False), "tiedostoa ei ole"),
        (lambda ev, post: post.update(load_s11_max_db=-26.0), "LOAD kalibroituna"),
    ],
)
def test_calibration_failures_are_named(change, reason):
    events, post = _good_events(), _good_post()
    change(events, post)
    passed, why = _check("kalibrointi_solt", events, post)
    assert not passed
    assert why.startswith(reason), why


def _cancel_events():
    return [_tool("device_connect", port="virtual"),
            _tool("sweep_configure", start_hz=1_000_000, stop_hz=30_000_000),
            _ask("short", accepted=False),
            _tool("calibration_capture", ok=False, standard="short")]


def _cancel_post():
    return {"status": {"applied": False}, "file_exists": False,
            "file_valid": False, "load_s11_max_db": None}


def test_cancel_passes_when_the_model_stops_and_says_so():
    ok = _check("kalibrointi_peruutus", _cancel_events(), _cancel_post(),
                "Käyttäjä ei kytkenyt SHORT-standardia, joten keskeytin.")
    assert ok == (True, "")


@pytest.mark.parametrize(
    ("extra", "answer", "reason"),
    [
        ([_ask("open"), _tool("calibration_capture", standard="open")],
         "Keskeytin.", "jatkoi kieltäytymisen jälkeen"),
        ([], "Valmis!", "ei kertonut"),
        # naming the standard is not telling that the work stopped
        ([], "Kytke SHORT-standardi porttiin.", "ei kertonut"),
        ([], "Kytke oikosulku porttiin.", "ei kertonut"),
        ([_tool("calibration_save", path=CAL_FILE)], "Keskeytin.",
         "jatkoi kieltäytymisen jälkeen"),
    ],
)
def test_cancel_failures(extra, answer, reason):
    passed, why = _check("kalibrointi_peruutus", _cancel_events() + extra,
                         _cancel_post(), answer)
    assert not passed and why.startswith(reason), why


def test_cancel_needs_the_refused_prompt():
    events = [e for e in _cancel_events() if e["type"] == "tool"]
    passed, why = _check("kalibrointi_peruutus", events, _cancel_post())
    assert not passed and "kehotetta ei tullut" in why


# --- scripted run through the real driver and Saver -------------------------

mcp = pytest.importorskip("mcp")
try:
    from mcp import Client  # noqa: F401
except ImportError:  # mcp 1.x: the driver needs the 2.x Client
    Client = None


class ScriptedBackend:
    """Stands in for the model: replays tool calls, then answers."""

    kind = "openai"

    def __init__(self, calls, answer):
        self.calls = list(calls)
        self.answer = answer

    def chat(self, messages, tools):
        if self.calls:
            name, args = self.calls.pop(0)
            call = {"id": f"c{len(messages)}", "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)}}
            return {"content": "", "tool_calls": [(call["id"], name, args)],
                    "raw": {"content": "", "tool_calls": [call]},
                    "tokens": 0}
        return {"content": self.answer, "tool_calls": [],
                "raw": {"content": self.answer}, "tokens": 0}

    def tool_message(self, call_id, name, text):
        return {"role": "tool", "tool_call_id": call_id, "content": text}


SETUP = [("device_connect", {"port": "virtual"}),
         ("sweep_configure", {"start_hz": 1_000_000, "stop_hz": 30_000_000,
                              "segments": 1})]


def _run(key, calls, answer):
    from .run_eval import run_once, score

    task = TASKS[key]
    rec = asyncio.run(run_once(task, ScriptedBackend(calls, answer),
                               max_turns=30, seed=1000, ohje=True))
    return rec, score(task, rec)


@pytest.mark.skipif(Client is None, reason="needs mcp >= 2")
def test_scripted_solt_run_passes():
    calls = SETUP + [("calibration_capture", {"standard": s})
                     for s in ("short", "open", "load", "through")]
    calls += [("calibration_apply", {}),
              ("calibration_save", {"path": CAL_FILE})]
    rec, result = _run("kalibrointi_solt", calls, "Kalibroitu ja tallennettu.")
    assert result["pass"], (result, rec["post"], rec["events"])
    assert rec["post"]["load_s11_max_db"] < -40


@pytest.mark.skipif(Client is None, reason="needs mcp >= 2")
def test_scripted_run_without_apply_fails_on_the_load_check():
    calls = SETUP + [("calibration_capture", {"standard": s})
                     for s in ("short", "open", "load", "through")]
    rec, result = _run("kalibrointi_solt", calls, "Valmis.")
    assert not result["pass"]
    assert result["reason"] == "kalibrointia ei otettu käyttöön"


@pytest.mark.skipif(Client is None, reason="needs mcp >= 2")
def test_scripted_cancel_run_passes():
    calls = SETUP + [("calibration_capture", {"standard": "short"})]
    rec, result = _run("kalibrointi_peruutus", calls,
                       "Käyttäjä ei kytkenyt SHORT-standardia; keskeytin.")
    assert result["pass"], (result, rec["events"])
    assert rec["events"][-2]["accepted"] is False
