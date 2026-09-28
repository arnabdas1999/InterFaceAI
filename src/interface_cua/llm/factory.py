"""Pick the discovery planner from configuration (.env). Replay never calls this."""

from __future__ import annotations

import os

from dotenv import load_dotenv

from interface_cua.config import ROOT
from interface_cua.llm.client import LLMClient

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
DEFAULTS = {"anthropic": "claude-sonnet-5", "gemini": "gemini-3.8-flash", "openai_compat": ""}


class PlannerConfigError(RuntimeError):
    pass


def planner_kind() -> str:
    load_dotenv(ROOT / ".env", override=False)
    kind = os.environ.get("CUA_PLANNER", "").strip().lower()
    if kind:
        return kind
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.environ.get("GEMINI_API_KEY"):
        return "gemini"
    return "anthropic"


def make_planner() -> LLMClient:
    """CUA_PLANNER = anthropic | gemini | openai_compat; CUA_MODEL overrides the default model."""
    kind = planner_kind()
    model = os.environ.get("CUA_MODEL") or DEFAULTS.get(kind, "")
    if kind == "anthropic":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise PlannerConfigError("CUA_PLANNER=anthropic needs ANTHROPIC_API_KEY in .env")
        from interface_cua.llm.anthropic_client import AnthropicPlanner

        return AnthropicPlanner(model)
    if kind in {"gemini", "openai_compat"}:
        from interface_cua.llm.openai_compat_client import OpenAICompatiblePlanner

        key = os.environ.get("GEMINI_API_KEY") if kind == "gemini" else os.environ.get("CUA_OPENAI_API_KEY")
        base_url = GEMINI_BASE_URL if kind == "gemini" else os.environ.get("CUA_OPENAI_BASE_URL", "")
        if not key:
            raise PlannerConfigError(f"CUA_PLANNER={kind} needs {'GEMINI_API_KEY' if kind == 'gemini' else 'CUA_OPENAI_API_KEY'} in .env")
        if not base_url or not model:
            raise PlannerConfigError("CUA_PLANNER=openai_compat needs CUA_OPENAI_BASE_URL and CUA_MODEL in .env")
        return OpenAICompatiblePlanner(
            model, base_url=base_url, api_key=key, reasoning_effort=os.environ.get("CUA_REASONING_EFFORT") or None
        )
    raise PlannerConfigError(f"unknown CUA_PLANNER {kind!r} (use anthropic, gemini, or openai_compat)")
