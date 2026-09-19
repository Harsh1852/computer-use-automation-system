"""The operator console.

Intentionally minimal: four endpoints and some inline HTML. The console is
not the interesting part — the mechanism underneath it is — and a polished UI
here would be a worse use of the page than making the control transfer real.
What it does have to be is *honest about who holds the session*, and it is.

The one design decision worth defending is the two VNC endpoints.

The monitor view (`:6080`) is served by an `x11vnc` started with `-viewonly`.
The interactive view (`:6081`) is a second `x11vnc` on the same display
without that flag. View-only is therefore enforced by the server that owns the
socket, not by a `view_only=true` parameter in a noVNC URL that anybody can
edit. The console embeds the monitor endpoint by default and only swaps the
iframe to the interactive one once the lease has actually transferred — so the
UI state follows the authority, rather than being the authority.
"""

from __future__ import annotations

import html
from typing import Any

from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .intervention import InterventionRequest, InterventionStore
from .lease import LeaseManager, LeaseViolation

MONITOR_PORT = 6080
INTERACTIVE_PORT = 6081

_STYLE = """
body { font-family: -apple-system, Segoe UI, Roboto, sans-serif; margin: 0;
       background: #12151a; color: #e6e6e6; }
header { background: #1c2027; padding: 12px 20px; border-bottom: 1px solid #2c323c; }
h1 { font-size: 15px; margin: 0; font-weight: 600; letter-spacing: .3px; }
main { padding: 20px; max-width: 1180px; }
a { color: #7fb3ff; }
table { border-collapse: collapse; width: 100%; font-size: 13px; }
th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #262b33; }
th { color: #8b93a1; font-weight: 500; }
.card { background: #191d24; border: 1px solid #262b33; border-radius: 6px;
        padding: 16px; margin-bottom: 16px; }
.k { color: #8b93a1; width: 170px; display: inline-block; }
.pill { padding: 2px 8px; border-radius: 10px; font-size: 11px; }
.automation { background: #14361f; color: #7ee2a8; }
.operator { background: #4a2a12; color: #ffc078; }
button { font: inherit; padding: 7px 16px; border-radius: 5px; border: 0;
         cursor: pointer; margin-right: 8px; }
.take { background: #2f6fd0; color: #fff; }
.release { background: #2a8a52; color: #fff; }
.abort { background: #8a2a2a; color: #fff; }
iframe { width: 100%; height: 620px; border: 1px solid #262b33; border-radius: 6px;
         background: #000; }
code { color: #ffc078; }
.empty { color: #8b93a1; }
"""


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><html><head><title>{html.escape(title)}</title>"
        f"<style>{_STYLE}</style></head><body>"
        f"<header><h1>CUA OPERATOR CONSOLE &mdash; {html.escape(title)}</h1></header>"
        f"<main>{body}</main></body></html>"
    )


def create_operator_app(
    *,
    lease: LeaseManager,
    store: InterventionStore,
    vnc_host: str = "localhost",
    human: Any = None,
) -> FastAPI:
    app = FastAPI(title="CUA operator console", docs_url=None, redoc_url=None)

    def _monitor_url() -> str:
        return f"http://{vnc_host}:{MONITOR_PORT}/vnc.html?autoconnect=1&resize=scale&reconnect=1"

    def _interactive_url() -> str:
        return (
            f"http://{vnc_host}:{INTERACTIVE_PORT}"
            "/vnc.html?autoconnect=1&resize=scale&reconnect=1"
        )

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        rows = []
        for request in store.all():
            state = "OPEN" if request.open else (request.resolution or "closed")
            rows.append(
                "<tr>"
                f"<td><a href='/session/{html.escape(request.session_id)}'>"
                f"{html.escape(request.id)}</a></td>"
                f"<td>{html.escape(request.capability_id)}@{html.escape(request.capability_version)}</td>"
                f"<td>{html.escape(request.at_step)} &mdash; {html.escape(request.step_intent)}</td>"
                f"<td>{html.escape(request.reason.value)}</td>"
                f"<td>{html.escape(state)}</td>"
                "</tr>"
            )
        table = (
            "<table><tr><th>request</th><th>capability</th><th>step</th>"
            "<th>reason</th><th>state</th></tr>" + "".join(rows) + "</table>"
            if rows
            else "<p class='empty'>No intervention requests. "
            "A run raises one when it cannot safely proceed.</p>"
        )
        holder = lease.lease.holder
        return _page(
            "interventions",
            f"<div class='card'>Session <code>{html.escape(lease.lease.session_id)}</code> "
            f"is held by <span class='pill {holder}'>{holder}</span></div>"
            f"<div class='card'>{table}</div>",
        )

    @app.get("/session/{session_id}", response_class=HTMLResponse)
    def session(session_id: str) -> HTMLResponse:
        request = store.by_session(session_id) or next(
            (r for r in store.all() if r.session_id == session_id), None
        )
        if request is None:
            raise HTTPException(404, "no intervention for that session")
        return _page(session_id, _detail_html(request))

    def _detail_html(request: InterventionRequest) -> str:
        current = lease.lease
        operator_holds = current.holder == "operator"
        # The iframe follows the lease, and the lease is the authority. Until
        # control has actually transferred, the operator is looking at a
        # socket that cannot accept input at all.
        frame_url = _interactive_url() if operator_holds else _monitor_url()
        mode = (
            "INTERACTIVE &mdash; you are driving this session"
            if operator_holds
            else "MONITOR (view-only) &mdash; take control to interact"
        )
        params = "".join(
            f"<div><span class='k'>{html.escape(k)}</span><code>{html.escape(v)}</code></div>"
            for k, v in request.redacted_params.items()
        )
        if operator_holds:
            controls = (
                f"<form method='post' action='/session/{request.session_id}/release'>"
                "<input type='hidden' name='token' value='" + html.escape(current.token) + "'>"
                "<button class='release' type='submit'>Release &mdash; resume automation</button>"
                "</form>"
                f"<form method='post' action='/session/{request.session_id}/abort'>"
                "<input type='hidden' name='token' value='" + html.escape(current.token) + "'>"
                "<button class='abort' type='submit'>Abort the run</button></form>"
            )
        else:
            controls = (
                f"<form method='post' action='/session/{request.session_id}/take'>"
                "<button class='take' type='submit'>Take control</button></form>"
            )
        remaining = current.seconds_remaining
        countdown = (
            f"<div><span class='k'>hold expires in</span>{int(remaining)}s</div>"
            if remaining is not None
            else ""
        )
        return (
            "<div class='card'>"
            f"<div><span class='k'>capability</span><code>{html.escape(request.capability_id)}"
            f"@{html.escape(request.capability_version)}</code></div>"
            f"<div><span class='k'>goal</span>{html.escape(request.goal)}</div>"
            f"<div><span class='k'>stopped at</span><code>{html.escape(request.at_step)}</code> "
            f"&mdash; {html.escape(request.step_intent)}</div>"
            f"<div><span class='k'>reason</span>{html.escape(request.reason.value)}</div>"
            f"<div><span class='k'>expected</span>{html.escape(request.expected)}</div>"
            f"<div><span class='k'>observed</span>{html.escape(request.observed)}</div>"
            f"<div><span class='k'>escalation</span>#{request.escalation_count}</div>"
            f"{params}{countdown}"
            f"<div><span class='k'>held by</span>"
            f"<span class='pill {current.holder}'>{current.holder}</span></div>"
            "</div>"
            f"<div class='card'>{controls}<p class='empty'>{mode}</p>"
            f"<iframe src='{frame_url}'></iframe></div>"
        )

    @app.post("/session/{session_id}/take")
    def take(session_id: str) -> RedirectResponse:
        request = store.by_session(session_id)
        if request is None:
            raise HTTPException(404, "no open intervention for that session")
        try:
            lease.take(reason=f"operator took {request.id}")
        except LeaseViolation as exc:
            raise HTTPException(409, str(exc)) from exc
        return RedirectResponse(f"/session/{session_id}", status_code=303)

    @app.post("/session/{session_id}/release")
    def release(session_id: str, token: str = Form(...), note: str = Form("")) -> RedirectResponse:
        try:
            lease.release(token, note or "operator released the session")
        except LeaseViolation as exc:
            raise HTTPException(409, str(exc)) from exc
        return RedirectResponse(f"/session/{session_id}", status_code=303)

    @app.post("/session/{session_id}/abort")
    def abort(session_id: str, token: str = Form(...), note: str = Form("")) -> RedirectResponse:
        try:
            lease.abort(token, note or "operator aborted the run")
        except LeaseViolation as exc:
            raise HTTPException(409, str(exc)) from exc
        return RedirectResponse(f"/session/{session_id}", status_code=303)

    # A JSON face on the same mechanism, so the flow can be driven by a
    # scripted operator in tests exactly as a person drives it in the browser.

    @app.get("/api/lease")
    def api_lease() -> JSONResponse:
        return JSONResponse(lease.lease.model_dump(mode="json"))

    @app.get("/api/interventions")
    def api_interventions() -> JSONResponse:
        return JSONResponse([r.model_dump(mode="json") for r in store.all()])

    @app.post("/api/session/{session_id}/take")
    def api_take(session_id: str) -> JSONResponse:
        request = store.by_session(session_id)
        if request is None:
            raise HTTPException(404, "no open intervention for that session")
        try:
            token = lease.take(reason=f"operator took {request.id}")
        except LeaseViolation as exc:
            raise HTTPException(409, str(exc)) from exc
        return JSONResponse({"token": token, "interactive_url": _interactive_url()})

    @app.post("/api/session/{session_id}/release")
    def api_release(session_id: str, payload: dict) -> JSONResponse:
        try:
            lease.release(payload["token"], payload.get("note"))
        except LeaseViolation as exc:
            raise HTTPException(409, str(exc)) from exc
        return JSONResponse({"ok": True})

    @app.post("/api/session/{session_id}/abort")
    def api_abort(session_id: str, payload: dict) -> JSONResponse:
        try:
            lease.abort(payload["token"], payload.get("note"))
        except LeaseViolation as exc:
            raise HTTPException(409, str(exc)) from exc
        return JSONResponse({"ok": True})

    @app.get("/api/human-steps")
    def api_human_steps() -> JSONResponse:
        return JSONResponse(human.as_timeline() if human is not None else [])

    return app
