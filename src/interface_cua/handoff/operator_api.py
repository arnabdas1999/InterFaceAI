"""Operator API + console, served in-process on the run's event loop.

Every request is authenticated (bearer token -> operator identity) and authorized (role + tenant
scope). The console and the ``cua operator`` CLI are both clients of this API.

Visibility model: before claiming, an operator sees a *masked* snapshot (triage). Once they hold the
lease they get a live CDP screencast of the real screen, because the accountable person in control must
see what they operate. Frames are streamed, never persisted.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

from interface_cua.domain.interventions import Resolution, ResolutionKind
from interface_cua.handoff.auth import AuthError, Operator, Role
from interface_cua.handoff.manager import HandoffError, InterventionManager


class ClaimBody(BaseModel):
    operator_id: str | None = None
    expect_version: int | None = None


class TokenBody(BaseModel):
    operator_id: str | None = None
    token: int


class ActBody(TokenBody):
    kind: str
    x: float | None = None
    y: float | None = None
    text: str | None = None
    key: str | None = None


class ResolveBody(BaseModel):
    operator_id: str | None = None
    kind: ResolutionKind
    token: int | None = None
    note: str = ""
    step_id: str | None = None
    action_hash: str | None = None


def create_operator_app(manager: InterventionManager) -> FastAPI:
    app = FastAPI(title="CUA operator API", docs_url=None, redoc_url=None, openapi_url=None)
    directory = manager.directory

    def who(request: Request, claimed_id: str | None = None) -> Operator:
        try:
            op = directory.authenticate(request.headers.get("authorization"))
        except AuthError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
        if claimed_id is not None and claimed_id != op.id:
            raise HTTPException(status_code=403, detail=f"token belongs to {op.id}, not {claimed_id}")
        return op

    def allow(op: Operator, role: Role, iv_id: str | None) -> None:
        """Role check, plus tenant scope when the request concerns a specific intervention."""
        try:
            if iv_id is not None:
                directory.require(op, role, manager.get(iv_id).tenant_id)
            elif role not in op.roles:
                raise AuthError(403, f"{op.id} lacks the {role} role")
        except AuthError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc

    def guard(fn: Any) -> Any:
        try:
            return fn()
        except HandoffError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc

    @app.get("/runs/current/control")
    async def control(request: Request) -> dict[str, Any]:
        allow(who(request), "operator", None)
        return manager.control()

    @app.get("/interventions")
    async def list_interventions(request: Request) -> list[dict[str, Any]]:
        op = who(request)
        allow(op, "operator", None)
        return [i.model_dump(mode="json", exclude={"human_actions"}) for i in manager.interventions.values() if op.can_serve(i.tenant_id)]

    @app.get("/interventions/{iv_id}")
    async def get_intervention(iv_id: str, request: Request) -> Any:
        guard(lambda: manager.get(iv_id))
        allow(who(request), "operator", iv_id)
        return manager.get(iv_id).model_dump(mode="json")

    @app.get("/interventions/{iv_id}/screenshot")
    async def screenshot(iv_id: str, request: Request) -> Response:
        guard(lambda: manager.get(iv_id))
        allow(who(request), "operator", iv_id)
        env = manager.env
        assert env is not None
        png = await env.adapter.screenshot_masked(env.input_values)
        return Response(png, media_type="image/png", headers={"Cache-Control": "no-store"})

    @app.get("/interventions/{iv_id}/controls")
    async def controls(iv_id: str, request: Request) -> list[dict[str, Any]]:
        guard(lambda: manager.get(iv_id))
        allow(who(request), "operator", iv_id)
        env = manager.env
        assert env is not None
        obs, _ = await env.adapter.observe(seq=0, input_values=env.input_values)
        return [
            {
                "role": c.role,
                "name": c.name,
                "frame": c.frame_index,
                "enabled": c.enabled,
                "checked": c.checked,
                "x": c.bbox.center[0] if c.bbox else None,
                "y": c.bbox.center[1] if c.bbox else None,
            }
            for c in obs.controls
        ]

    @app.get("/interventions/{iv_id}/stream")
    async def stream(iv_id: str, token: int, request: Request) -> StreamingResponse:
        """Live screencast (server-sent events, JPEG frames), only for the operator holding the lease."""
        guard(lambda: manager.get(iv_id))
        op = who(request)
        allow(op, "operator", iv_id)
        env = manager.env
        assert env is not None
        try:
            env.lease.check(token, f"human:{op.id}")
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=f"live view is only for the operator in control: {exc}") from exc
        queue = manager.subscribe_frames()

        async def frames() -> AsyncIterator[bytes]:
            try:
                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        frame = await asyncio.wait_for(queue.get(), timeout=1.0)
                    except TimeoutError:
                        if env.lease.owner != f"human:{op.id}":
                            return  # control moved on; the live view ends with it
                        continue
                    yield b"data: " + frame + b"\n\n"
            finally:
                manager.unsubscribe_frames(queue)

        return StreamingResponse(frames(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})

    @app.post("/interventions/{iv_id}/claim")
    async def claim(iv_id: str, body: ClaimBody, request: Request) -> Any:
        guard(lambda: manager.get(iv_id))
        op = who(request, body.operator_id)
        allow(op, "operator", iv_id)
        result = guard(lambda: manager.claim(iv_id, op.id, body.expect_version))
        await manager.sync_fence()  # the headed window accepts this operator's input before the claim returns
        await manager.start_live_view()
        return result

    @app.post("/interventions/{iv_id}/heartbeat")
    async def heartbeat(iv_id: str, body: TokenBody, request: Request) -> Any:
        guard(lambda: manager.get(iv_id))
        op = who(request, body.operator_id)
        allow(op, "operator", iv_id)
        result = guard(lambda: manager.heartbeat(iv_id, op.id, body.token))
        await manager.sync_fence()
        return result

    @app.post("/interventions/{iv_id}/act")
    async def act(iv_id: str, body: ActBody, request: Request) -> JSONResponse:
        guard(lambda: manager.get(iv_id))
        op = who(request, body.operator_id)
        allow(op, "operator", iv_id)
        try:
            entry = await manager.act(iv_id, op.id, body.token, body.kind, x=body.x, y=body.y, text=body.text, key=body.key)
        except HandoffError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
        return JSONResponse({k: v for k, v in entry.items() if k != "target"})

    @app.post("/interventions/{iv_id}/resolve")
    async def resolve(iv_id: str, body: ResolveBody, request: Request) -> dict[str, Any]:
        guard(lambda: manager.get(iv_id))
        op = who(request, body.operator_id)
        # Approving or denying an irreversible action is a supervisor decision; everything else needs the operator role.
        allow(op, "supervisor" if body.kind in {"approve", "deny"} else "operator", iv_id)
        res = Resolution(kind=body.kind, operator_id=op.id, note=body.note, step_id=body.step_id, action_hash=body.action_hash)
        iv = guard(lambda: manager.resolve(iv_id, res, body.token))
        await manager.sync_fence()  # closed again before the response: the human no longer controls the window
        await manager.stop_live_view()
        return {"status": iv.status, "resolution": iv.resolution.model_dump() if iv.resolution else None, "control": manager.control()}

    @app.get("/console/{iv_id}", response_class=HTMLResponse)
    async def console(iv_id: str) -> str:
        return CONSOLE_HTML.replace("__IV__", iv_id)

    return app


class OperatorServer:
    def __init__(self, manager: InterventionManager, port: int) -> None:
        config = uvicorn.Config(create_operator_app(manager), host="127.0.0.1", port=port, log_level="warning", access_log=False)
        self.server = uvicorn.Server(config)
        self.server.capture_signals = contextlib.nullcontext  # type: ignore[method-assign,assignment]
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self.server.serve())
        for _ in range(100):
            if self.server.started:
                return
            await asyncio.sleep(0.05)
        raise RuntimeError("operator API failed to start (port in use?)")

    async def stop(self) -> None:
        self.server.should_exit = True
        if self._task:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._task, timeout=5)


CONSOLE_HTML = r"""<!doctype html><html><head><meta charset="utf-8"><title>Operator console</title>
<style>body{font:14px system-ui,sans-serif;margin:16px;background:#f6f6f4;color:#222}#ctx{background:#fff;border:1px solid #ccc;padding:10px;margin-bottom:10px}
#shot{border:2px solid #444;cursor:crosshair;max-width:100%}button{margin:2px}code{background:#eee;padding:1px 4px}.warn{color:#a00}
#mode{font-weight:bold}</style></head>
<body><h2>Intervention <code>__IV__</code></h2>
<div>API token <input id="api" type="password" size="40" placeholder="cuaop_..."> <button onclick="load()">Sign in</button></div>
<div id="ctx">sign in with your operator token</div>
<div><button onclick="claim()">Claim live session</button> <span id="tok"></span> <span id="mode"></span></div>
<p>Click on the screen to click in the live session. <input id="txt" placeholder="text to type"> <button onclick="typeText()">Type</button>
<button onclick="press('Enter')">Enter</button> <button onclick="press('Tab')">Tab</button></p>
<img id="shot"><p>
<input id="note" size="40" placeholder="note for the audit trail">
<button onclick="resolve('resume')">Hand back: resume</button><button onclick="resolve('complete')">I completed it</button>
<button onclick="resolve('abort')">Abort run</button> | <button onclick="resolve('approve')">Approve action</button><button onclick="resolve('deny')">Deny action</button></p>
<pre id="log"></pre>
<script>
const IV='__IV__'; const KEY='cua-lease-'+IV; let iv=null; let streaming=false; let hbTimer=null;
// The lease token is a fencing number, not a credential: keeping it in sessionStorage lets a reload resume control.
let token=null; try{token=Number(sessionStorage.getItem(KEY))||null}catch(e){}
function keep(t){token=t;try{t?sessionStorage.setItem(KEY,String(t)):sessionStorage.removeItem(KEY)}catch(e){}tok.textContent=t?'lease token '+t:''}
// No blocking dialogs on this page: a native prompt freezes timers, and with them the heartbeat.
function startHb(s){if(hbTimer)clearInterval(hbTimer);hbTimer=setInterval(hb,Math.max(1,(s||30)/3)*1000)}
function lost(why){if(!token)return;keep(null);if(hbTimer)clearInterval(hbTimer);mode.innerHTML='<span class=warn>control lost ('+why+'): claim again to continue</span>'}
const log=m=>document.getElementById('log').textContent=m+'\n'+document.getElementById('log').textContent;
const H=()=>({'authorization':'Bearer '+document.getElementById('api').value,'content-type':'application/json'});
async function j(u,o){const r=await fetch(u,{...(o||{}),headers:H()});const t=await r.json();if(!r.ok)throw new Error(t.detail||r.status);return t}
async function snapshot(){if(streaming)return;const r=await fetch('/interventions/'+IV+'/screenshot',{headers:H()});if(r.ok){shot.src=URL.createObjectURL(await r.blob());mode.textContent='masked snapshot (triage view)'}}
async function load(){try{iv=await j('/interventions/'+IV);const c=await j('/runs/current/control');
ctx.innerHTML=`<b>${iv.kind}</b> (${iv.reason_code}) at step <code>${iv.step_id}</code>: ${iv.explanation}<br>
capability: <code>${iv.capability||iv.goal}</code> tenant: <code>${iv.tenant_id}</code> route: <code>${iv.route}</code><br>expected: ${iv.expected||'-'}<br>observed: ${iv.observed||'-'}<br>
${iv.bound_action?'<span class=warn>action awaiting approval: '+iv.bound_action+'</span><br>':''}
control: owner <code>${c.owner}</code>, expected <code>${c.expected_owner}</code>, state <code>${c.state}</code>, version ${c.version}. status: ${iv.status}`;
if(token&&(c.version!==token||c.state!=='HUMAN_ACTIVE'))lost('the lease changed: '+c.state);snapshot()}catch(e){log(e)}}
async function claim(){try{const r=await j('/interventions/'+IV+'/claim',{method:'POST',body:JSON.stringify({})});
keep(r.token);startHb(r.heartbeat_s);live();load()}catch(e){log(e)}}
async function live(){try{const r=await fetch('/interventions/'+IV+'/stream?token='+token,{headers:H()});if(!r.ok){log('live view: '+r.status);return}
streaming=true;mode.textContent='LIVE (you are in control)';const rd=r.body.getReader();const dec=new TextDecoder();let buf='';
for(;;){const {value,done}=await rd.read();if(done)break;buf+=dec.decode(value,{stream:true});let i;while((i=buf.indexOf('\n\n'))>=0){const ev=buf.slice(0,i);buf=buf.slice(i+2);
if(ev.startsWith('data: '))shot.src='data:image/jpeg;base64,'+ev.slice(6)}}}catch(e){log(e)}finally{streaming=false;mode.textContent='live view ended'}}
async function hb(){if(token)try{await j('/interventions/'+IV+'/heartbeat',{method:'POST',body:JSON.stringify({token})})}catch(e){log(e);lost(String(e.message||e))}}
async function act(b){try{const r=await j('/interventions/'+IV+'/act',{method:'POST',body:JSON.stringify({token,...b})});log(JSON.stringify(r));setTimeout(snapshot,300)}catch(e){log(e)}}
shot.onclick=e=>{const sx=shot.naturalWidth/shot.clientWidth;act({kind:'click',x:e.offsetX*sx,y:e.offsetY*sx})};
function typeText(){act({kind:'type',text:txt.value})} function press(k){act({kind:'press',key:k})}
async function resolve(kind){try{const r=await j('/interventions/'+IV+'/resolve',{method:'POST',
body:JSON.stringify({token,kind,note:note.value||'',action_hash:kind==='approve'?iv.bound_action_hash:null})});log(JSON.stringify(r));
if(hbTimer)clearInterval(hbTimer);keep(null);mode.textContent='handed back ('+kind+')';load()}catch(e){log(e)}}
setInterval(()=>{if(document.getElementById('api').value)load()},3000);
if(token){tok.textContent='lease token '+token;mode.textContent='sign in to resume control';
  api.addEventListener('change',()=>{startHb(30);live()},{once:true})}
</script></body></html>"""
