#  NanoVNASaver
#
#  A python program to view and export Touchstone data from a NanoVNA
#  Copyright (C) 2019, 2020  Rune B. Broberg
#  Copyright (C) 2020,2021 NanoVNA-Saver Authors
#
#  This program is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""Run the MCP evaluation: a real model drives headless Saver.

Each run starts a fresh `python -m NanoVNASaver --headless --mcp stdio
--virtual --virtual-seed N` and gives the model one task, all of
Saver's MCP tools and Saver's server instructions (as real clients do).
Run i uses seed --seed + i for every model and task, so models are
compared on the same simulated devices.
Every run is appended to a JSONL file; `--summary` turns a file into a
table. Three outcomes are kept apart: pass, fail (the model answered
wrong or not at all) and error (the run itself broke: model server down,
Saver crashed, timeout), so a broken run is never counted as the
model's failure or success. A connection error to the model server is
retried once (the row says so); a timeout is not, since it may be the
model being slow. After --max-errors runs in a row end in error the
remaining runs are not started (the model server is down; measured
2.11.2026: five hours of timeouts, 99 runs, before the run ended). Rerun
the same seeds later: `--summary` keeps one row per model, prompt, task
and seed, the latest that is not an error.

Backends: "ollama" (native /api/chat, sets the context length so all
tool schemas fit) and "openai" (any OpenAI compatible
/chat/completions; key from the env variable named by --api-key-env).

    python tests/mcp_eval/run_eval.py --model qwen3-coder:30b \
        --base-url http://localhost:11434 --n 10 --out eval.jsonl
    python tests/mcp_eval/run_eval.py --summary eval.jsonl
"""

import argparse
import asyncio
import json
import math
import os
import statistics
import sys
import tempfile
import threading
import time
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(HERE))

from mcp import Client, StdioServerParameters  # noqa: E402
from mcp import types as mcp_types  # noqa: E402
from tasks import (  # noqa: E402
    TASKS,
    Task,
    check_calibration,
    parse_answer,
    standard_in_prompt,
    tasks_for,
)

from NanoVNASaver.Calibration import Calibration  # noqa: E402

SYSTEM = (
    "Ohjaat NanoVNA-Saver-ohjelmaa ja siihen kytkettyä vektoriverkko-"
    "analysaattoria työkaluilla.{ohje} "
    "Jos laitetta ei ole yhdistetty, etsi portti ja yhdistä se ensin. "
    "Vastaa suomeksi."
)
# --ohje on/off: the A/B variable for "does telling the model not to
# guess make it measure"
OHJE = " Mittaa aina itse työkaluilla; älä arvaa."
# Tools whose result is a data summary of a sweep they ran:
# device_connect sweeps the device's current range right after connecting.
SWEEPING = ("sweep_run", "device_connect")
TRANSIENT = ("URLError", "ConnectionResetError", "ConnectionRefusedError",
             "RemoteDisconnected", "IncompleteRead")
MAX_RESULT_CHARS = 6000


def http_json(url: str, body: dict, headers: dict, timeout: float) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json", **headers},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


class Backend:
    def __init__(self, args):
        self.kind = args.backend
        self.model = args.model
        self.base = args.base_url.rstrip("/")
        self.timeout = args.call_timeout
        self.num_ctx = args.num_ctx
        self.headers = {}
        if args.api_key_env:
            key = os.environ.get(args.api_key_env)
            if not key:
                raise SystemExit(f"{args.api_key_env} is not set")
            self.headers["Authorization"] = f"Bearer {key}"

    def chat(self, messages: list[dict], tools: list[dict]) -> dict:
        """One model turn as {"content": str, "tool_calls": [(id, name,
        args)], "tokens": int | None}."""
        if self.kind == "ollama":
            out = http_json(
                f"{self.base}/api/chat",
                {"model": self.model, "messages": messages, "tools": tools,
                 "stream": False, "options": {"num_ctx": self.num_ctx}},
                self.headers, self.timeout,
            )
            msg = out["message"]
            calls = [
                (f"c{i}", c["function"]["name"], c["function"]["arguments"])
                for i, c in enumerate(msg.get("tool_calls") or [])
            ]
            tokens = (out.get("prompt_eval_count") or 0) + (
                out.get("eval_count") or 0)
            return {"content": msg.get("content") or "", "tool_calls": calls,
                    "raw": msg, "tokens": tokens}
        out = http_json(
            f"{self.base}/chat/completions",
            {"model": self.model, "messages": messages, "tools": tools},
            self.headers, self.timeout,
        )
        msg = out["choices"][0]["message"]
        calls = []
        for c in msg.get("tool_calls") or []:
            raw = c["function"].get("arguments") or "{}"
            try:
                parsed = json.loads(raw) if isinstance(raw, str) else raw
            except json.JSONDecodeError:
                parsed = {"_invalid_json": raw}
            calls.append((c["id"], c["function"]["name"], parsed))
        usage = out.get("usage") or {}
        return {"content": msg.get("content") or "", "tool_calls": calls,
                "raw": msg, "tokens": usage.get("total_tokens")}

    def tool_message(self, call_id: str, name: str, text: str) -> dict:
        if self.kind == "ollama":
            return {"role": "tool", "tool_name": name, "content": text}
        return {"role": "tool", "tool_call_id": call_id, "content": text}


def tool_specs(tools) -> list[dict]:
    return [
        {"type": "function", "function": {
            "name": t.name,
            "description": t.description or "",
            "parameters": (getattr(t, "input_schema", None)
                           or getattr(t, "inputSchema", None)
                           or {"type": "object", "properties": {}}),
        }}
        for t in tools
    ]


def result_text(result) -> str:
    parts = []
    for c in result.content:
        parts.append(getattr(c, "text", None) or f"[{c.type}]")
    text = "\n".join(parts)
    if getattr(result, "is_error", None) or getattr(result, "isError", False):
        text = "VIRHE: " + text
    return text[:MAX_RESULT_CHARS]


async def read_state(client, check: str) -> float | None:
    if check == "marker0_hz":
        res = await client.call_tool("marker_list", {})
        # list results come as one content item (JSON object) per element
        for item in res.content:
            marker = json.loads(item.text)
            if marker.get("index") == 0 and marker.get("freq_hz"):
                return float(marker["freq_hz"])
    return None


def sweep_range(text: str) -> tuple[float, float] | None:
    """start/stop of the sweep summarised in a SWEEPING tool's result,
    else None (e.g. device_connect when no sweep ran)."""
    try:
        out = json.loads(text)
        return float(out["start_hz"]), float(out["stop_hz"])
    except (ValueError, KeyError, TypeError):
        return None


class SimulatedUser:
    """Plays the person at the bench in calibration tasks (#114): answers
    Saver's elicitations, "connects" each standard it is asked for by
    writing the --virtual-bench file (outside MCP, like a hand), and
    refuses `decline`. Every prompt goes to `events` in order."""

    def __init__(self, bench: Path, events: list, decline: str | None):
        self.bench = bench
        self.events = events
        self.decline = decline
        bench.write_text("dut", encoding="utf-8")  # the antenna

    async def __call__(self, _ctx, params):
        message = getattr(params, "message", "") or ""
        standard = standard_in_prompt(message)
        accept = standard is not None and standard != self.decline
        if accept and standard != "save":
            self.bench.write_text(standard, encoding="utf-8")
        self.events.append({"type": "elicit", "standard": standard,
                            "accepted": accept, "message": message[:200]})
        if accept:
            return mcp_types.ElicitResult(action="accept",
                                          content={"ok": True})
        return mcp_types.ElicitResult(action="decline")


async def calibration_post(client, task: Task, bench: Path,
                           workspace: Path) -> dict:
    """What the checker needs, read after the model stopped: status, the
    saved file, and |S11| of a LOAD measured with the calibration on."""
    post: dict = {}
    res = await client.call_tool("calibration_status", {})
    post["status"] = json.loads(res.content[0].text)
    path = workspace / (task.cal_file or "")
    post["file_exists"] = path.is_file()
    post["file_valid"] = False
    if post["file_exists"]:
        try:
            cal = Calibration()
            cal.load(str(path))
            post["file_valid"] = cal.isValid1Port()
        except Exception:  # pylint: disable=broad-except
            pass
    post["load_s11_max_db"] = None
    if post["status"].get("applied"):
        bench.write_text("load", encoding="utf-8")
        res = await client.call_tool("sweep_run", {})
        if not result_text(res).startswith("VIRHE"):
            text = await client.read_resource("saver://sweep/latest.s1p")
            body = "".join(getattr(c, "text", "") for c in text.contents)
            mags = []
            for line in body.splitlines():
                cols = line.split()
                if len(cols) >= 3 and not line.startswith(("!", "#")):
                    mags.append(abs(complex(float(cols[1]), float(cols[2]))))
            if mags:
                post["load_s11_max_db"] = round(
                    20 * math.log10(max(max(mags), 1e-12)), 1)
    return post


async def run_once(task: Task, backend: Backend, max_turns: int,
                   seed: int, ohje: bool) -> dict:
    with tempfile.TemporaryDirectory(prefix="mcp_eval_") as tmp:
        return await _run_once(task, backend, max_turns, seed, ohje,
                               Path(tmp))


async def _run_once(task: Task, backend: Backend, max_turns: int,
                    seed: int, ohje: bool, tmp: Path) -> dict:
    calibrating = task.kind != "answer"
    bench = tmp / "bench.txt"
    workspace = tmp / "ws"
    argv = ["-m", "NanoVNASaver", "--headless", "--mcp", "stdio",
            "--virtual", "--virtual-seed", str(seed),
            "--mcp-dir", str(workspace)]  # never the user's own folder
    if calibrating:
        argv += ["--virtual-bench", str(bench)]
    params = StdioServerParameters(
        command=sys.executable,
        args=argv,
        env={**os.environ,
             # noserial: never probe a real VNA that is plugged in
             "PYTHONPATH": os.pathsep.join(
                 p for p in (str(HERE / "noserial"), str(ROOT / "src"),
                             os.environ.get("PYTHONPATH", "")) if p),
             "QT_QPA_PLATFORM": "offscreen"},
        cwd=str(ROOT),
    )
    rec: dict = {"tools": [], "turns": 0, "tokens": 0, "answer": "",
                 "sweeps": []}
    events: list = []
    user = None
    if calibrating:
        rec["events"] = events
        user = SimulatedUser(bench, events, task.decline)
    async with Client(params, elicitation_callback=user) as client:
        specs = tool_specs((await client.list_tools()).tools)
        system = SYSTEM.format(ohje=OHJE if ohje else "")
        if client.instructions:
            system += "\n\n" + client.instructions
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": task.prompt}]
        for _ in range(max_turns):
            rec["turns"] += 1
            # the HTTP call blocks; keep the MCP session's loop alive
            reply = await asyncio.to_thread(backend.chat, messages, specs)
            rec["tokens"] += reply["tokens"] or 0
            messages.append({"role": "assistant", **{
                k: v for k, v in reply["raw"].items()
                if k in ("content", "tool_calls")}})
            if not reply["tool_calls"]:
                rec["answer"] = reply["content"]
                break
            for call_id, name, args in reply["tool_calls"]:
                rec["tools"].append(name)
                try:
                    res = await client.call_tool(name, args or {})
                    text = result_text(res)
                except Exception as exc:  # pylint: disable=broad-except
                    text = f"VIRHE: {exc}"
                if name in SWEEPING and not text.startswith("VIRHE"):
                    span = sweep_range(text)
                    if span:
                        rec["sweeps"].append(span)
                if calibrating:
                    events.append({"type": "tool", "name": name,
                                   "args": args or {},
                                   "ok": not text.startswith("VIRHE")})
                messages.append(backend.tool_message(call_id, name, text))
        else:
            rec["stopped"] = "max_turns"
        if task.state_check:
            rec["state"] = await read_state(client, task.state_check)
        if calibrating:
            rec["post"] = await calibration_post(client, task, bench,
                                                 workspace)
    return rec


def describe(exc: BaseException) -> str:
    """The real causes inside (nested) exception groups, not just
    "unhandled errors in a TaskGroup".

    Duck-typed on .exceptions: Python 3.10 has no built-in
    BaseExceptionGroup, and anyio raises the exceptiongroup backport
    there (requires-python is >=3.10)."""
    inner = getattr(exc, "exceptions", None)
    if isinstance(inner, (list, tuple)) and inner:
        return "; ".join(describe(e) for e in inner)
    return f"{type(exc).__name__}: {exc}"


def score(task: Task, rec: dict) -> dict:
    """pass needs both the right value and a sweep that covered
    task.at_hz; "reason" tells a guess from a wrong measurement.
    Calibration tasks are scored from Saver's events (check_calibration)."""
    if task.kind != "answer":
        passed, reason = check_calibration(task, rec)
        return {"value": None, "checked": None, "measured": True,
                "pass": passed, "reason": reason}
    value = parse_answer(rec["answer"], task.unit)
    checked = rec.get("state") if task.state_check else value
    measured = task.at_hz is None or any(
        lo <= task.at_hz <= hi for lo, hi in rec.get("sweeps", []))
    right = task.ok(checked)
    if checked is None:
        reason = "ei vastausta"
    elif not measured:
        reason = "ei mitattu" + (" (arvaus osui)" if right else "")
    elif not right:
        reason = "väärä arvo"
    else:
        reason = ""
    return {"value": value, "checked": checked, "measured": measured,
            "pass": right and measured, "reason": reason}


def run(args) -> None:
    backend = Backend(args)
    keys = args.tasks or list(TASKS)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    lock = threading.Lock()
    in_a_row = [0]          # runs in a row that ended in error
    stopped = threading.Event()
    skipped = [0]

    def one(i: int, key: str) -> None:
        if stopped.is_set():
            with lock:
                skipped[0] += 1
            return
        seed = args.seed + i
        task = tasks_for(seed)[key]
        row = {"model": args.model, "backend": args.backend,
               "task": key, "i": i, "seed": seed, "ohje": args.ohje,
               "truth": task.truth, "tol": task.tolerance(),
               "at_hz": task.at_hz, "jobs": args.jobs,
               "started": time.time()}
        t0 = time.monotonic()
        for attempt in (1, 2):
            try:
                rec = asyncio.run(asyncio.wait_for(
                    run_once(task, backend, args.max_turns, seed,
                             args.ohje == "on"),
                    args.run_timeout))
                row.update(rec)
                row.update(score(task, rec))
                row["outcome"] = "pass" if row["pass"] else "fail"
                row.pop("error", None)
                break
            except Exception as exc:  # pylint: disable=broad-except
                row["outcome"] = "error"
                row["error"] = describe(exc)[:1000]
                if attempt == 1 and row["error"].startswith(TRANSIENT):
                    row["retried"] = row["error"]
                    continue
                break
        row["duration_s"] = round(time.monotonic() - t0, 1)
        with lock, out.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            in_a_row[0] = in_a_row[0] + 1 if row["outcome"] == "error" else 0
            if in_a_row[0] >= args.max_errors and not stopped.is_set():
                stopped.set()
                print(f"{in_a_row[0]} virhe-ajoa peräkkäin: loput ajot "
                      "jätetään aloittamatta", flush=True)
        print(f"{args.model} {key} #{i}: {row['outcome']} "
              f"({row['duration_s']} s)", flush=True)

    # Each run is its own Saver process; with --jobs > 1 the next ones
    # start up while the model answers the previous (durations then
    # include waiting for the model server).
    work = [(i, key) for i in range(args.n) for key in keys]
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        list(pool.map(lambda w: one(*w), work))
    if stopped.is_set():
        print(f"keskeytetty: {skipped[0]} ajoa aloittamatta", flush=True)
        raise SystemExit(2)


def summary(path: str) -> str:
    rows = [json.loads(line) for line in Path(path).read_text(
        encoding="utf-8").splitlines() if line.strip()]
    # A rerun of the same seed replaces the earlier row: the latest row
    # that is not an error, else the latest error.
    latest = {}
    for r in rows:
        key = (r["model"], r.get("ohje", "-"), r["task"], r.get("seed"))
        old = latest.get(key)
        if old is None or r["outcome"] != "error" or \
                old["outcome"] == "error":
            latest[key] = r
    groups = defaultdict(list)
    for r in latest.values():
        groups[(r["model"], r.get("ohje", "-"), r["task"])].append(r)
    lines = [
        "| malli | ohje | tehtävä | onnistui | arvaus osui | virhe-ajot | "
        "mediaani s | työkalukutsuja (ka) |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for (model, ohje, task), rs in sorted(groups.items()):
        ok = [r for r in rs if r["outcome"] != "error"]
        passed = sum(r["outcome"] == "pass" for r in ok)
        # rows from before the measurement rule have no "reason"
        guessed = sum(r.get("reason") == "ei mitattu (arvaus osui)"
                      for r in ok)
        errors = len(rs) - len(ok)
        rate = f"{passed}/{len(ok)}" if ok else "–"
        dur = statistics.median(r["duration_s"] for r in rs)
        calls = (statistics.mean(len(r["tools"]) for r in ok)
                 if ok else float("nan"))
        lines.append(f"| {model} | {ohje} | {task} | {rate} | {guessed} | "
                     f"{errors} | "
                     f"{dur:.0f} | {calls:.1f} |")
    return "\n".join(lines)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--model")
    p.add_argument("--backend", choices=("ollama", "openai"),
                   default="ollama")
    p.add_argument("--base-url", default="http://localhost:11434",
                   help="ollama: server root; openai: …/v1")
    p.add_argument("--api-key-env", help="env variable holding the key")
    p.add_argument("--tasks", nargs="*", choices=list(TASKS))
    p.add_argument("--n", type=int, default=10)
    p.add_argument("--jobs", type=int, default=1,
                   help="runs in parallel (each its own Saver)")
    p.add_argument("--max-turns", type=int, default=20)
    p.add_argument("--num-ctx", type=int, default=32768)
    p.add_argument("--call-timeout", type=float, default=300)
    p.add_argument("--run-timeout", type=float, default=900)
    p.add_argument("--seed", type=int, default=1000,
                   help="run i simulates the device drawn from seed + i")
    p.add_argument("--ohje", choices=("on", "off"), default="on",
                   help='"measure, do not guess" in the system prompt')
    p.add_argument("--max-errors", type=int, default=6,
                   help="stop starting runs after this many errors in a row")
    p.add_argument("--out", default="mcp_eval_results.jsonl")
    p.add_argument("--summary", metavar="JSONL")
    args = p.parse_args()
    if args.summary:
        print(summary(args.summary))
        return
    if not args.model:
        p.error("--model is required")
    run(args)


if __name__ == "__main__":
    main()
