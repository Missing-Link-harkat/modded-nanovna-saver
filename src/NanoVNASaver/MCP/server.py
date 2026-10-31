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
"""MCP server inside Saver.

Tools are generated from the action registry. The server runs in its own
thread with its own asyncio loop; every action is executed on the Qt main
thread through MainThreadBridge.

Transports:
  * streamable HTTP on 127.0.0.1 with a bearer token (GUI mode; the user
    sees what the model does and can press STOP)
  * stdio (headless mode, for clients that start Saver themselves)
"""

import asyncio
import inspect
import json
import logging
import secrets
import threading
import time
from pathlib import Path
from typing import Annotated, Any

from pydantic import BaseModel, Field

from .bridge import MainThreadBridge
from .control import AIControl, LogEntry
from .prompts import register_prompts
from .registry import REGISTRY, Action, Risk

try:  # mcp >= 2
    from mcp.server.mcpserver import (
        Context,
        Elicit,
        ElicitationResult,
        Image,
        Resolve,
    )
    from mcp.server.mcpserver import MCPServer as _Server
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import Context, Image  # type: ignore[no-redef]
    from mcp.server.fastmcp import FastMCP as _Server  # type: ignore[no-redef]
    from mcp.server.fastmcp.exceptions import (  # type: ignore[no-redef]
        ToolError,
    )

    Resolve = None  # type: ignore[assignment,misc]

from mcp.types import ToolAnnotations

from .api import ActionError, PngImage, SaverAPI

logger = logging.getLogger(__name__)

CONFIG_DIR = Path.home() / ".nanovna-saver"
CONNECTION_FILE = CONFIG_DIR / "mcp.json"

INSTRUCTIONS = """\
NanoVNA-Saver controls a vector network analyzer (VNA) connected to the
user's computer. Typical flow: device_connect -> sweep_configure ->
sweep_run -> data_summary / data_points / chart_image / analysis_run.
Frequencies are in Hz. S11 is reflection at port 1 (antennas, loads),
S21 is transmission from port 1 to port 2 (filters, cables, amplifiers).
Calibrate (calibration_capture short/open/load, then calibration_apply)
over the range you measure; the user must connect each standard.
Actions that change state work only when the user has allowed AI control
in Saver; saving files always asks the user. Explain results in terms of
the user's goal (e.g. antenna resonance, bandwidth at VSWR <= 2).
"""


class Confirm(BaseModel):
    ok: bool = Field(description="Kyllä, jatka")


class Refused(ToolError):
    """Shown to the model as the tool's error text."""


# Marks that the client could not be asked; Saver asks in its own window.
_ASK_IN_GUI = object()


def _client_supports_elicitation(ctx: Context) -> bool:
    try:
        caps = ctx.client_capabilities  # mcp >= 2
    except AttributeError:
        try:
            caps = ctx.session.client_params.capabilities
        except AttributeError:
            return False
    return getattr(caps, "elicitation", None) is not None


class SaverMCP:
    """Owns the MCP server, its thread and the control state."""

    def __init__(
        self,
        api: SaverAPI,
        control: AIControl,
        bridge: MainThreadBridge,
        gui_confirm: Any = None,
    ) -> None:
        self.api = api
        self.control = control
        self.bridge = bridge
        # callable(message) -> bool, run on the main thread (GUI dialog)
        self.gui_confirm = gui_confirm
        self._act_lock: asyncio.Lock | None = None
        self.server = _Server(
            name="nanovna-saver", instructions=INSTRUCTIONS
        )
        self._thread: threading.Thread | None = None
        self._uvicorn: Any = None
        self.url = ""
        self.token = ""
        for act in REGISTRY.values():
            self._add_tool(act)
        self._add_resources()
        register_prompts(self.server)

    # --- tools -----------------------------------------------------------

    def _add_tool(self, act: Action) -> None:
        sig = inspect.signature(act.func)
        params = [p for n, p in sig.parameters.items() if n != "self"]
        ctx_param = inspect.Parameter(
            "ctx", inspect.Parameter.KEYWORD_ONLY, annotation=Context
        )

        extra = [ctx_param]
        annotations: dict[str, Any] = {"ctx": Context}
        if act.confirm is not None and Resolve is not None:
            # mcp >= 2: the framework asks the user before the tool body
            # runs, over whichever protocol version was negotiated.
            confirm_type = Annotated[
                ElicitationResult[Confirm], Resolve(self._resolver(act))
            ]
            extra.append(
                inspect.Parameter(
                    "confirmation",
                    inspect.Parameter.KEYWORD_ONLY,
                    annotation=confirm_type,
                )
            )
            annotations["confirmation"] = confirm_type

        async def tool(
            ctx: Context, confirmation: Any = None, **kwargs: Any
        ) -> Any:
            return await self.run_action(act, kwargs, ctx, confirmation)

        tool.__signature__ = sig.replace(  # type: ignore[attr-defined]
            parameters=[*params, *extra], return_annotation=Any
        )
        tool.__annotations__ = {
            **{p.name: p.annotation for p in params},
            **annotations,
            "return": Any,
        }
        tool.__name__ = act.name
        tool.__doc__ = act.description
        risk_text = {
            Risk.READ: "Read only.",
            Risk.ACT: "Needs AI control allowed in Saver.",
            Risk.PERSIST: "Writes a file; asks the user every time.",
        }[act.risk]
        self.server.add_tool(
            tool,
            name=act.name,
            description=f"{act.description}\n\n[{risk_text}]",
            annotations=ToolAnnotations(
                readOnlyHint=act.risk is Risk.READ,
                destructiveHint=act.risk is Risk.PERSIST,
                idempotentHint=act.risk is Risk.READ,
                openWorldHint=False,
            ),
            structured_output=False,
        )

    def _resolver(self, act: Action) -> Any:
        """Resolver asking the client to confirm `act` (mcp >= 2)."""
        params = [
            p
            for n, p in inspect.signature(act.func).parameters.items()
            if n != "self"
        ]

        def resolve(ctx: Context, **kwargs: Any) -> Any:
            if _client_supports_elicitation(ctx):
                return Elicit(act.confirm(**kwargs), Confirm)
            return _ASK_IN_GUI

        resolve.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
            [
                inspect.Parameter(
                    "ctx", inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    annotation=Context,
                ),
                *[p.replace(kind=inspect.Parameter.KEYWORD_ONLY)
                  for p in params],
            ]
        )
        resolve.__annotations__ = {
            "ctx": Context,
            **{p.name: p.annotation for p in params},
        }
        return resolve

    async def confirm(
        self, ctx: Context | None, message: str, answer: Any = None
    ) -> bool:
        """Ask the human: client elicitation, else Saver's own dialog,
        else no. `answer` is the outcome the framework already elicited."""
        if answer is not None:
            if getattr(answer, "action", None) != "accept":
                return False
            data = getattr(answer, "data", None)
            if data is not _ASK_IN_GUI:
                return bool(getattr(data, "ok", False))
        elif ctx is not None and _client_supports_elicitation(ctx):
            result = await ctx.elicit(message, Confirm)
            return result.action == "accept" and bool(
                getattr(result.data, "ok", False)
            )
        if self.gui_confirm is not None:
            gui_confirm = self.gui_confirm
            return bool(
                await self.bridge.call(lambda: gui_confirm(message), 3600)
            )
        return False

    async def run_action(
        self,
        act: Action,
        kwargs: dict[str, Any],
        ctx: Context | None,
        confirmation: Any = None,
    ) -> Any:
        started = time.monotonic()

        def log(outcome: str, detail: str = "") -> None:
            self.control.record(
                LogEntry(
                    time.time(),
                    act.name,
                    act.risk.value,
                    kwargs,
                    outcome,
                    detail[:300],
                    int((time.monotonic() - started) * 1000),
                )
            )

        if act.risk is not Risk.READ and not self.control.allowed:
            log("denied", "AI control not allowed")
            raise Refused(
                "AI-ohjaus ei ole sallittu. Pyydä käyttäjää sallimaan "
                "AI-ohjaus Saverin AI (MCP) -paneelista."
            )
        if self._act_lock is None:
            self._act_lock = asyncio.Lock()
        lock = self._act_lock if act.risk is not Risk.READ else None
        if lock is not None and lock.locked():
            log("denied", "another action running")
            raise Refused("Toinen toiminto on käynnissä; odota sen valmistumista.")
        try:
            if lock is not None:
                await lock.acquire()
            self.control.begin(act.name)
            if act.confirm is not None:
                if not await self.confirm(
                    ctx, act.confirm(**kwargs), confirmation
                ):
                    log("denied", "user declined")
                    raise Refused("Käyttäjä ei hyväksynyt toimintoa.")
            func = act.func
            api = self.api
            try:
                result = await self.bridge.call(
                    lambda: func(api, **kwargs), act.timeout
                )
            except TimeoutError:
                self.bridge.submit(self.api.app.worker.quit)
                log("timeout")
                raise Refused(
                    f"{act.name} ei valmistunut {act.timeout:.0f} s:ssa; "
                    "pyyhkäisy pysäytettiin."
                ) from None
            if not self.control.allowed and act.risk is not Risk.READ:
                log("stopped", "user pressed STOP")
                raise Refused("Käyttäjä painoi STOP. AI-ohjaus on pois.")
        except (ActionError, ValueError) as exc:
            log("error", str(exc))
            raise ToolError(str(exc)) from exc
        finally:
            self.control.end()
            if lock is not None and lock.locked():
                lock.release()
        log("ok")
        if isinstance(result, PngImage):
            return Image(data=result.data, format="png")
        return result

    # --- resources -------------------------------------------------------

    def _add_resources(self) -> None:
        srv = self.server

        @srv.resource(
            "saver://sweep/latest.s1p",
            name="latest_s1p",
            description="Current sweep, S11, Touchstone 1-port",
            mime_type="text/plain",
        )
        async def latest_s1p() -> str:
            return await self.bridge.call(lambda: self.api.touchstone_text(1))

        @srv.resource(
            "saver://sweep/latest.s2p",
            name="latest_s2p",
            description="Current sweep, S11 and S21, Touchstone 2-port",
            mime_type="text/plain",
        )
        async def latest_s2p() -> str:
            return await self.bridge.call(lambda: self.api.touchstone_text(2))

        for uri, name, getter in (
            ("saver://device", "device", "device_info"),
            ("saver://sweep/settings", "sweep_settings", "sweep_get"),
            ("saver://sweep/summary", "sweep_summary", "data_summary"),
            ("saver://calibration", "calibration", "calibration_status"),
            ("saver://markers", "markers", "marker_list"),
        ):
            self._json_resource(uri, name, getter)

        @srv.resource(
            "saver://ai-log",
            name="ai_log",
            description="What the AI has done in this Saver session",
            mime_type="text/plain",
        )
        def ai_log() -> str:
            return "\n".join(e.line() for e in self.control.entries[-200:])

    def _json_resource(self, uri: str, name: str, getter: str) -> None:
        method = getattr(self.api, getter)

        async def read() -> str:
            return json.dumps(await self.bridge.call(method), default=str)

        read.__name__ = name
        self.server.resource(
            uri,
            name=name,
            description=(method.__doc__ or "").strip().splitlines()[0],
            mime_type="application/json",
        )(read)

    # --- transports ------------------------------------------------------

    def start_http(self, port: int = 8765, token: str | None = None) -> str:
        """Start streamable HTTP on 127.0.0.1 in a thread; returns URL."""
        import uvicorn  # pylint: disable=import-outside-toplevel

        self.token = token or secrets.token_urlsafe(24)
        try:
            app = self.server.streamable_http_app(host="127.0.0.1")
        except TypeError:  # mcp 1.x
            app = self.server.streamable_http_app()
        guarded = _TokenGuard(app, self.token)
        config = uvicorn.Config(
            guarded, host="127.0.0.1", port=port, log_level="warning"
        )
        self._uvicorn = uvicorn.Server(config)
        self._thread = threading.Thread(
            target=self._uvicorn.run, name="mcp-http", daemon=True
        )
        self._thread.start()
        self.url = f"http://127.0.0.1:{port}/mcp"
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            CONNECTION_FILE.write_text(
                json.dumps({"url": self.url, "token": self.token}),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("Cannot write %s: %s", CONNECTION_FILE, exc)
        logger.info("MCP server listening on %s", self.url)
        return self.url

    def stop_http(self) -> None:
        if self._uvicorn is not None:
            self._uvicorn.should_exit = True
            self._uvicorn = None
        self.url = ""

    def start_stdio(self, on_exit: Any = None) -> None:
        """Serve MCP on stdin/stdout in a thread (headless mode)."""

        def run() -> None:
            try:
                asyncio.run(self.server.run_stdio_async())
            finally:
                if on_exit is not None:
                    self.bridge.submit(on_exit)

        self._thread = threading.Thread(target=run, name="mcp-stdio",
                                        daemon=True)
        self._thread.start()


class _TokenGuard:
    """ASGI wrapper: require `Authorization: Bearer <token>` and refuse
    browser requests from other origins."""

    def __init__(self, app: Any, token: str) -> None:
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            headers = dict(scope.get("headers") or [])
            origin = headers.get(b"origin", b"")
            if origin and not origin.startswith(
                (b"http://127.0.0.1", b"http://localhost")
            ):
                await _reply(send, 403, b"origin not allowed")
                return
            if not secrets.compare_digest(
                headers.get(b"authorization", b""), self.expected
            ):
                await _reply(send, 401, b"missing or wrong token")
                return
        await self.app(scope, receive, send)


async def _reply(send: Any, status: int, body: bytes) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"text/plain")],
        }
    )
    await send({"type": "http.response.body", "body": body})
