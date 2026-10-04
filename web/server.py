"""FastAPI app: REST control plane, the /ws stream, and static file serving.

Built by ``create_app(manager, broadcaster)`` so the entry point (laptop_chat)
owns construction of the SystemManager and Broadcaster. The lifespan hook
binds the broadcaster to the running loop and kicks off ``manager.startup()``
as a background task (so the server starts accepting connections immediately,
showing STARTING, rather than blocking until the robot is ready).
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Body
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager
import asyncio

from system import (
    SystemManager,
    ConversationConflict,
    NotReady,
    InvalidSystemState,
)
from show_player import ShowError
import show_editor
from show_editor import ShowEditError


log = logging.getLogger("reachy.web")

STATIC_DIR = Path(__file__).parent / "static"


def create_app(manager: SystemManager, broadcaster) -> FastAPI:

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        broadcaster.set_loop(asyncio.get_running_loop())
        # Run startup concurrently so the server is already listening (and the
        # browser can render STARTING) while VAD loads and SSH connects.
        startup_task = asyncio.create_task(manager.startup())
        # Phase B: robot daemon heartbeat (probes only while the robot is up).
        manager.start_heartbeat()
        try:
            yield
        finally:
            startup_task.cancel()
            try:
                await startup_task
            except (asyncio.CancelledError, Exception):
                pass
            await manager.shutdown()

    app = FastAPI(title="Reachy Mini Handler Dashboard", lifespan=lifespan)

    @app.get("/api/status")
    async def api_status():
        return manager.status()

    @app.post("/api/conversation/start")
    async def api_start():
        try:
            result = await manager.start_conversation()
            return result
        except ConversationConflict:
            return JSONResponse(status_code=409, content={"error": "already_running"})
        except NotReady as e:
            return JSONResponse(status_code=503,
                                content={"error": "not_ready", "state": e.state})

    @app.post("/api/conversation/end")
    async def api_end():
        try:
            return await manager.end_conversation("user")
        except ConversationConflict:
            return JSONResponse(status_code=409, content={"error": "not_running"})

    @app.post("/api/system/stop")
    async def api_system_stop():
        try:
            return await manager.stop_system("user_stop")
        except InvalidSystemState as e:
            return JSONResponse(status_code=409,
                                content={"error": "invalid_state", "state": e.state})

    @app.post("/api/system/start")
    async def api_system_start():
        # Blocking: returns once IDLE_BREATHING is reached (~7-10 s).
        try:
            return await manager.start_system("user_start")
        except InvalidSystemState as e:
            return JSONResponse(status_code=409,
                                content={"error": "invalid_state", "state": e.state})

    # ----- show mode (operator board) -----

    # ----- identity and the persona switch -----

    @app.get("/api/identity")
    async def api_identity():
        """Who this robot is, which backend, and which key — by label only."""
        return manager.identity_payload()

    @app.get("/api/persona")
    async def api_persona():
        return manager.persona_payload()

    @app.put("/api/persona")
    async def api_persona_update(body: dict = Body(...)):
        """The switch and the panel behind it.

        Every field is optional and only what is sent moves, so the switch can
        be flicked without resending the text and the text can be edited
        without touching the switch.
        """
        allowed = ("enabled", "overlay", "preset_id", "voice")
        changes = {k: body[k] for k in allowed if k in body}
        if not changes:
            return JSONResponse(
                {"error": "nothing to change",
                 "detail": f"send one or more of: {', '.join(allowed)}"},
                status_code=400)
        try:
            return manager.set_persona(**changes)
        except ValueError as e:
            # A rejected persona is a message for the person who typed it, not
            # a server error — 400 with the sentence they need to read.
            return JSONResponse({"error": "rejected", "detail": str(e)},
                                status_code=400)

    @app.post("/api/persona/reset")
    async def api_persona_reset():
        return manager.reset_persona()

    @app.get("/api/show/cues")
    async def api_show_cues():
        """The cue catalog for the board: sections, hotkeys, text, durations."""
        return {**manager.show.catalog(),
                "playing": manager.show.playing,
                "state": manager.state.name}

    @app.post("/api/show/reload")
    async def api_show_reload():
        """Re-read cues.json after a script edit, without restarting."""
        manager.show.load()
        return manager.show.catalog()

    @app.post("/api/show/fire/{cue_id}")
    async def api_show_fire(cue_id: str):
        try:
            cue = manager.fire_cue(cue_id)
            return {"fired": cue["id"], "duration_s": cue.get("duration_s")}
        except InvalidSystemState as e:
            return JSONResponse(status_code=409,
                                content={"error": "invalid_state", "state": e.state})
        except ShowError as e:
            return JSONResponse(status_code=400, content={"error": str(e)})

    @app.post("/api/show/stop")
    async def api_show_stop():
        return manager.stop_cue()

    # ----- show mode (editing the script from the board) -----
    #
    # Every write re-synthesises the affected line, which is a network call to
    # the TTS model taking a few seconds. They run on a worker thread so the
    # dashboard stays responsive — the operator board polls status, and a
    # blocked event loop would freeze the board mid-edit.

    @app.get("/api/show/motions")
    async def api_show_motions():
        """Valid emotion / dance / direction names, for the editor's pickers."""
        return await asyncio.to_thread(show_editor.motion_catalog)

    @app.put("/api/show/cue/{cue_id}")
    async def api_show_cue_update(cue_id: str, body: dict = Body(...)):
        try:
            result = await asyncio.to_thread(show_editor.update_cue, cue_id, body)
        except ShowEditError as e:
            return JSONResponse(status_code=400, content={"error": str(e)})
        except Exception as e:
            log.exception("show cue update failed")
            return JSONResponse(status_code=500, content={"error": str(e)})
        manager.show.load()
        return {**result, **manager.show.catalog()}

    @app.post("/api/show/cue")
    async def api_show_cue_add(body: dict = Body(...)):
        section_id = body.pop("section_id", None)
        if not section_id:
            return JSONResponse(status_code=400,
                                content={"error": "section_id is required"})
        try:
            result = await asyncio.to_thread(show_editor.add_cue, section_id, body)
        except ShowEditError as e:
            return JSONResponse(status_code=400, content={"error": str(e)})
        except Exception as e:
            log.exception("show cue add failed")
            return JSONResponse(status_code=500, content={"error": str(e)})
        manager.show.load()
        return {**result, **manager.show.catalog()}

    @app.delete("/api/show/cue/{cue_id}")
    async def api_show_cue_delete(cue_id: str):
        # Refuse while it is playing: deleting the WAV out from under the
        # streaming thread is a race with nothing useful on the other side.
        if manager.show.playing == cue_id:
            return JSONResponse(
                status_code=409,
                content={"error": "that cue is playing right now — stop it first"})
        try:
            result = await asyncio.to_thread(show_editor.delete_cue, cue_id)
        except ShowEditError as e:
            return JSONResponse(status_code=400, content={"error": str(e)})
        except Exception as e:
            log.exception("show cue delete failed")
            return JSONResponse(status_code=500, content={"error": str(e)})
        manager.show.load()
        return {**result, **manager.show.catalog()}

    @app.post("/api/show/rebuild")
    async def api_show_rebuild(force: bool = False):
        try:
            result = await asyncio.to_thread(show_editor.rebuild_all, force)
        except Exception as e:
            log.exception("show rebuild failed")
            return JSONResponse(status_code=500, content={"error": str(e)})
        manager.show.load()
        return {**result, **manager.show.catalog()}

    @app.get("/show")
    async def show_page():
        return FileResponse(STATIC_DIR / "show.html")

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        await ws.accept()
        await broadcaster.register(ws)
        try:
            # Immediate snapshot so a fresh/refreshed tab reconstructs state.
            await ws.send_json(manager.snapshot())
            # We don't expect client messages, but we must keep receiving to
            # detect disconnects.
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        except Exception:
            log.debug("ws receive ended", exc_info=True)
        finally:
            broadcaster.unregister(ws)

    @app.get("/")
    async def index():
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    return app
