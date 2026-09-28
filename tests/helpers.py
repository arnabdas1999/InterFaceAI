"""Shared test helpers: scripted planners that choose controls by visible name, not by number."""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

from interface_cua.discovery.recorder import DiscoveryRequest, InputDecl, OutputDecl
from interface_cua.domain.types import OutputType
from interface_cua.llm.client import CheckProposal, DiscoveryContext, DiscoveryDecision


def handle_for(ctx: DiscoveryContext, name: str, role: str | None = None) -> str:
    for line in ctx.observation_text.splitlines():
        m = re.match(r"^\s*(\d+) \| (\w+) \| '(.*)' \|", line)
        if m and m.group(3) == name and (role is None or m.group(2) == role):
            return m.group(1)
    raise AssertionError(f"control {name!r} not on screen:\n{ctx.observation_text[:1500]}")


def balance_request(**overrides: object) -> DiscoveryRequest:
    data: dict[str, object] = {
        "goal": "Look up member by member ID and read their current Share Savings balance.",
        "tenant_id": "tenant-a",
        "entry": "member_search",
        "capability_id": "read-savings-balance",
        "capability_name": "Read share savings balance",
        "inputs": [InputDecl(name="member_id", type="string", pattern=r"^\d{5}$", description="5-digit member ID", sensitivity="pii_low")],
        "expected_outputs": [OutputDecl(name="savings_balance", type=OutputType.MONEY)],
        "max_steps": 15,
    }
    data.update(overrides)
    return DiscoveryRequest.model_validate(data)


def subaccount_request(**overrides: object) -> DiscoveryRequest:
    data: dict[str, object] = {
        "goal": "Start opening a sub-account for the member and stop at the review screen. Do not open the account.",
        "tenant_id": "tenant-a",
        "entry": "member_search",
        "capability_id": "open-sub-account",
        "inputs": [
            InputDecl(name="member_id", pattern=r"^\d{5}$", description="5-digit member ID", sensitivity="pii_low"),
            InputDecl(name="product", type="enum", enum=["share_savings", "money_market", "share_certificate"], sensitivity="public"),
            InputDecl(name="nickname", pattern=r"^[A-Za-z0-9 ]{1,24}$", sensitivity="pii_low"),
        ],
        "max_steps": 15,
    }
    data.update(overrides)
    return DiscoveryRequest.model_validate(data)


def subaccount_script(ctx: DiscoveryContext) -> DiscoveryDecision:
    text, hist = ctx.observation_text, " ".join(ctx.history)
    if "Main route: /members/search" in text:
        if "Type input member_id" not in hist:
            return DiscoveryDecision(action="type", handle=handle_for(ctx, "Member ID"), input_name="member_id")
        return DiscoveryDecision(action="click", handle=handle_for(ctx, "Search", "button"))
    if "Review New Sub-Account" in text:
        return DiscoveryDecision(
            action="done",
            checkpoint=[
                CheckProposal(label_text="Member ID", equals_input="member_id"),
                CheckProposal(label_text="Product", equals_input="product"),
                CheckProposal(label_text="Nickname", equals_input="nickname"),
            ],
        )
    if "/subaccounts/new" in text:
        if "Select input product" not in hist:
            return DiscoveryDecision(action="select", handle=handle_for(ctx, "Product"), input_name="product")
        if "Type input nickname" not in hist:
            return DiscoveryDecision(action="type", handle=handle_for(ctx, "Nickname"), input_name="nickname")
        return DiscoveryDecision(action="click", handle=handle_for(ctx, "Continue", "button"))
    return DiscoveryDecision(action="click", handle=handle_for(ctx, "Open Sub-Account", "link"))


def balance_script(ctx: DiscoveryContext) -> DiscoveryDecision:
    """Scripted stand-in for the model on the happy path (offline tests only)."""
    text = ctx.observation_text
    done = list(ctx.history)
    if "Main route: /members/search" in text and not any("Type input member_id" in h for h in done):
        return DiscoveryDecision(action="type", handle=handle_for(ctx, "Member ID"), input_name="member_id", rationale="enter id")
    if "Main route: /members/search" in text:
        return DiscoveryDecision(action="click", handle=handle_for(ctx, "Search", "button"), rationale="search")
    if "Current Balance" in text and "savings_balance" not in ctx.extracted:
        return DiscoveryDecision(
            action="extract",
            output_name="savings_balance",
            output_type=OutputType.MONEY,
            label_text="Current Balance",
            frame_index=1,
            intent_risk="read_only",
            rationale="read balance",
        )
    if "savings_balance" in ctx.extracted:
        return DiscoveryDecision(
            action="done",
            checkpoint=[
                CheckProposal(label_text="Member ID", frame_index=1, equals_input="member_id"),
                CheckProposal(label_text="Account Type", frame_index=1, equals_text="Share Savings"),
            ],
            rationale="balance read",
        )
    if "Share Savings" in text and "Account Type" in text:
        return DiscoveryDecision(action="click", handle=handle_for(ctx, "Share Savings", "link"), rationale="open savings")
    return DiscoveryDecision(action="click", handle=handle_for(ctx, "View Accounts"), rationale="show accounts")


# --- operator identities for handoff tests -----------------------------------------------------
TOKENS: dict[str, str] = {}  # operator id -> bearer token, filled by conftest
TEST_OPERATORS = [
    {"id": "supervisor-1", "roles": ["operator", "supervisor"], "tenants": ["tenant-a", "tenant-b"]},
    {"id": "scripted-operator", "roles": ["operator"], "tenants": ["tenant-a", "tenant-b"]},
    {"id": "outsider", "roles": ["operator"], "tenants": ["tenant-b"]},
    *({"id": i, "roles": ["operator"], "tenants": ["tenant-a"]} for i in ("alice", "bob", "carol", "dana", "op-1", "a", "b")),
]


def auth(operator_id: str) -> dict[str, str]:
    return {"authorization": f"Bearer {TOKENS[operator_id]}"}


def operator_client(base_url: str, default: str = "supervisor-1") -> httpx.AsyncClient:
    """Test client that authenticates as the operator named in the JSON body (``default`` otherwise).
    The server derives identity from the token and rejects a body ``operator_id`` that does not match."""

    async def sign(request: httpx.Request) -> None:
        who = default
        if request.content:
            body: Any = json.loads(request.content)
            who = body.get("operator_id") or default
        if "authorization" not in request.headers:
            request.headers["authorization"] = f"Bearer {TOKENS[who]}"

    return httpx.AsyncClient(base_url=base_url, timeout=30, event_hooks={"request": [sign]})
