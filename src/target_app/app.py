"""SynthCore: a deliberately legacy-looking synthetic back-office app used as the automation target.

Traits on purpose: server-rendered, nested tables, unlabeled inputs (labels live in adjacent
cells), no test IDs, an iframe account panel, a native confirm(), and out-of-band fault
injection under /__admin (which the automation allowlist never permits).
"""

from __future__ import annotations

import asyncio
import os
import re
import secrets
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from target_app.branding import BRANDING, PRODUCTS
from target_app.state import KNOWN_FAULTS, RESERVED_MEMBER_IDS, TargetState

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
COOKIE = "SYNTHSESSID"
SLUGS = {
    "share_savings": "savings",
    "share_draft": "draft",
    "money_market": "money-market",
    "share_certificate": "certificate",
}
SLUG_TO_KIND = {v: k for k, v in SLUGS.items()}
NICKNAME_RE = re.compile(r"^[A-Za-z0-9 ]{1,24}$")


class FaultRequest(BaseModel):
    name: str
    params: dict[str, Any] = {}
    count: int = 1


def create_app(tenant: str | None = None) -> FastAPI:
    tenant = tenant or os.environ.get("SYNTH_TENANT", "tenant-a")
    brand = BRANDING[tenant]
    # The same variables the automation's secret provider reads (secret_ref synthcore/operator), so one
    # setting in .env drives both sides. The defaults are the documented synthetic account.
    load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)
    username = os.environ.get("CUA_SECRET_SYNTHCORE_OPERATOR_USERNAME", "operator1")
    password = os.environ.get("CUA_SECRET_SYNTHCORE_OPERATOR_PASSWORD", "synthetic-only-pass")
    state = TargetState()
    app = FastAPI(title="SynthCore (synthetic)", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.target = state

    def render(request: Request, name: str, status: int = 200, **ctx: Any) -> HTMLResponse:
        ctx.setdefault("user", _user(request))
        return TEMPLATES.TemplateResponse(request, name, {"b": brand, **ctx}, status_code=status)

    def _user(request: Request) -> str | None:
        sid = request.cookies.get(COOKIE)
        sess = state.sessions.get(sid) if sid else None
        return sess.user if sess else None

    def session_ok(request: Request) -> bool:
        sid = request.cookies.get(COOKIE)
        if state.take("session_expire") is not None:
            state.expire(sid)
            return False
        return state.touch(sid)

    def expired() -> RedirectResponse:
        return RedirectResponse("/login?expired=1", status_code=302)

    def app_error(request: Request, frame: bool = False) -> HTMLResponse:
        ref = "ERR-" + secrets.token_hex(3).upper()
        return render(
            request,
            "frame_error.html" if frame else "error.html",
            status=500,
            title="Application Error",
            message="An unexpected error occurred while processing your request.",
            ref=ref,
        )

    # --- auth ---------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    async def root(request: Request) -> Response:
        return RedirectResponse("/main" if state.touch(request.cookies.get(COOKIE)) else "/login", 302)

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request, expired: int = 0) -> Response:
        msg = "Your session has expired. Please sign on again." if expired else None
        return render(request, "login.html", msg=msg, user=None)

    @app.post("/login")
    async def login(request: Request, usr: str = Form(""), pwd: str = Form("")) -> Response:
        if not (secrets.compare_digest(usr, username) and secrets.compare_digest(pwd, password)):
            return render(request, "login.html", status=401, msg="Invalid user or password.", user=None)
        sid = state.new_session(usr)
        # Legacy-style session token echoed into the URL; automation must canonicalize it away.
        resp = RedirectResponse(f"/main?jsessionid={secrets.token_hex(8)}", status_code=302)
        resp.set_cookie(COOKIE, sid, httponly=True, samesite="lax")
        return resp

    @app.get("/logout")
    async def logout(request: Request) -> Response:
        state.expire(request.cookies.get(COOKIE))
        resp = RedirectResponse("/login", 302)
        resp.delete_cookie(COOKIE)
        return resp

    @app.get("/main", response_class=HTMLResponse)
    async def main(request: Request) -> Response:
        if not session_ok(request):
            return expired()
        if brand.get("ui_mode") == "classic":
            return render(request, "frameset.html")  # legacy shell: banner + nav + content frames
        return render(request, "main.html")

    @app.get("/banner", response_class=HTMLResponse)
    async def banner(request: Request) -> Response:
        if not state.touch(request.cookies.get(COOKIE)):
            return expired()
        return render(request, "banner.html")

    @app.get("/nav", response_class=HTMLResponse)
    async def nav(request: Request) -> Response:
        if not state.touch(request.cookies.get(COOKIE)):
            return expired()
        return render(request, "nav.html")

    # --- member inquiry -------------------------------------------------------
    @app.get("/members/search", response_class=HTMLResponse)
    async def search(request: Request) -> Response:
        if not session_ok(request):
            return expired()
        dup = state.take("duplicate_control") is not None
        return render(request, "search.html", duplicate=dup, err=None)

    @app.get("/members/lookup", response_class=HTMLResponse)
    async def lookup(request: Request, mbr: str = "", lnm: str = "") -> Response:
        if not session_ok(request):
            return expired()
        slow = state.take("slow_search")
        if slow is not None:
            await asyncio.sleep(int(slow.get("delay_ms", 2500)) / 1000)
        if state.take("http_500", route="lookup") is not None:
            return app_error(request)
        redirect = state.take("offsite_redirect")
        if redirect is not None:
            return RedirectResponse(redirect.get("url", "https://example.org/"), 302)
        mbr = mbr.strip()
        if not re.fullmatch(r"\d{5}", mbr):
            return render(request, "search.html", duplicate=False, err=f"{brand['member_id']} must be exactly 5 digits.")
        if mbr in RESERVED_MEMBER_IDS:
            return render(request, "search.html", duplicate=False, err=f"{brand['member_id']} {mbr} is reserved and cannot be displayed.")
        if mbr not in state.members:
            return render(request, "results.html")
        return RedirectResponse(f"/members/{mbr}", 302)

    @app.get("/members/{mid}", response_class=HTMLResponse)
    async def detail(request: Request, mid: str) -> Response:
        if not session_ok(request):
            return expired()
        if state.take("http_500", route="detail") is not None:
            return app_error(request)
        m = state.members.get(mid)
        if m is None:
            return render(request, "results.html")
        popup = state.take("popup")
        return render(
            request,
            "detail.html",
            mid=mid,
            m=m,
            view_label="Show Accounts" if state.take("caption_drift") is not None else brand["view_accounts"],
            notice=state.take("system_notice") is not None,
            unknown=state.take("unknown_modal") is not None,
            popup_url=(popup or {}).get("url", "https://example.com/promo") if popup is not None else None,
        )

    @app.post("/members/{mid}/close", response_class=HTMLResponse)
    async def close_account(request: Request, mid: str) -> Response:
        if not session_ok(request):
            return expired()
        state.ledger.append({"kind": "close_request", "member_id": mid})
        return render(request, "error.html", title="Closure Requested", message="Closure request queued.", ref="-")

    # --- account panel (iframe) ------------------------------------------------
    @app.get("/blank", response_class=HTMLResponse)
    async def blank(request: Request) -> Response:
        return render(request, "blank.html")

    @app.get("/members/{mid}/accounts", response_class=HTMLResponse)
    async def accounts(request: Request, mid: str) -> Response:
        if not session_ok(request):
            return expired()
        if state.take("permission_denied") is not None:
            return render(
                request,
                "frame_error.html",
                status=403,
                title="Access Denied",
                message="You do not have permission to view account balances (ERR-SEC-403).",
                ref="SEC-403",
            )
        if state.take("http_500", route="accounts") is not None:
            return app_error(request, frame=True)
        m = state.members.get(mid)
        if m is None:
            return app_error(request, frame=True)
        rows = [{**a, "slug": SLUGS[a["kind"]], "label": brand[a["kind"]]} for a in m["accounts"]]
        return render(request, "accounts.html", mid=mid, accounts=rows)

    @app.get("/members/{mid}/accounts/{slug}", response_class=HTMLResponse)
    async def account_detail(request: Request, mid: str, slug: str) -> Response:
        if not session_ok(request):
            return expired()
        m = state.members.get(mid)
        kind = SLUG_TO_KIND.get(slug)
        acct = next((a for a in (m or {}).get("accounts", []) if a["kind"] == kind), None)
        if acct is None:
            return app_error(request, frame=True)
        return render(request, "account_detail.html", mid=mid, a={**acct, "label": brand[acct["kind"]]})

    # --- sub-account wizard ----------------------------------------------------
    @app.get("/members/{mid}/subaccounts/new", response_class=HTMLResponse)
    async def sub_new(request: Request, mid: str) -> Response:
        if not session_ok(request):
            return expired()
        if mid not in state.members:
            return render(request, "results.html")
        return render(request, "sub_new.html", mid=mid, products=PRODUCTS, err=None)

    @app.post("/members/{mid}/subaccounts/review", response_class=HTMLResponse)
    async def sub_review(request: Request, mid: str, prd: str = Form(""), nck: str = Form("")) -> Response:
        if not session_ok(request):
            return expired()
        err = None
        if prd not in PRODUCTS:
            err = "Please select a product."
        elif not NICKNAME_RE.fullmatch(nck.strip()):
            err = "Nickname must be 1-24 letters, digits, or spaces."
        if err:
            return render(request, "sub_new.html", mid=mid, products=PRODUCTS, err=err)
        return render(request, "sub_review.html", mid=mid, prd=prd, nck=nck.strip())

    @app.post("/members/{mid}/subaccounts/commit", response_class=HTMLResponse)
    async def sub_commit(request: Request, mid: str, prd: str = Form(""), nck: str = Form("")) -> Response:
        if not session_ok(request):
            return expired()
        if mid not in state.members or prd not in PRODUCTS:
            return app_error(request)
        entry = state.commit_sub_account(mid, prd, nck.strip())
        return render(request, "sub_done.html", mid=mid, e=entry)

    # --- out-of-band admin (never on the automation allowlist) ----------------
    @app.get("/__admin/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "tenant": tenant, "product": "SynthCore", "version": "4.2.7"}

    @app.post("/__admin/reset")
    async def reset() -> dict[str, Any]:
        state.reset()
        return {"ok": True}

    @app.get("/__admin/faults")
    async def list_faults() -> dict[str, Any]:
        return {
            "known": sorted(KNOWN_FAULTS),
            "armed": {k: {"params": v.params, "remaining": v.remaining} for k, v in state.faults.items()},
        }

    @app.post("/__admin/faults")
    async def set_fault(req: FaultRequest) -> Response:
        try:
            state.arm(req.name, req.params, req.count)
        except KeyError:
            return JSONResponse({"error": f"unknown fault {req.name}"}, status_code=400)
        return JSONResponse({"ok": True, "armed": req.name, "count": req.count})

    @app.delete("/__admin/faults")
    async def clear_faults() -> dict[str, Any]:
        state.clear_faults()
        return {"ok": True}

    @app.get("/__admin/ledger")
    async def ledger() -> dict[str, Any]:
        return {"entries": state.ledger}

    return app


app = create_app()
