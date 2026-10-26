"""verify-write — wiring only; the logic lives in verify_write.py.

Two hooks, no tools, no network at import:

* ``post_tool_call``  — after a mutation runs, read the target back and record the verdict.
* ``pre_llm_call``    — before the model's next turn, hand it any write that did not
  read back clean, so "exit 0" can never be reported as "done" by accident.

``/verify-write`` prints the current ledger for the session.
"""

from __future__ import annotations

import logging
from typing import Any

from . import verify_write as vw

logger = logging.getLogger(__name__)

_FALLBACK_SESSION = "default"


def _session(session_id: str, task_id: str = "") -> str:
    return session_id or task_id or _FALLBACK_SESSION


def _on_post_tool_call(
    tool_name: str = "",
    args: dict[str, Any] | None = None,
    result: Any = None,
    status: str = "",
    session_id: str = "",
    task_id: str = "",
    **_: Any,
) -> None:
    """Observer hook: never raises, never blocks the tool."""
    try:
        if status and status != "ok":
            return  # the tool already failed loudly; nothing silent to chase
        vw.record(tool_name, args or {}, result, _session(session_id, task_id))
    except Exception as exc:  # pragma: no cover - hooks must not break the turn
        logger.debug("verify-write post_tool_call: %s", exc)


def _on_pre_llm_call(
    session_id: str = "",
    task_id: str = "",
    **_: Any,
) -> dict[str, str] | None:
    try:
        text = vw.summary(_session(session_id, task_id))
        return {"context": text} if text else None
    except Exception as exc:  # pragma: no cover
        logger.debug("verify-write pre_llm_call: %s", exc)
        return None


def register(ctx) -> None:
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)

    def _show(raw: str) -> str:
        """`/verify-write` has no session id, so it prints the whole ledger unless one is given."""
        key = (raw or "").strip()
        ledger = {key: vw.entries(key)} if key else vw.snapshot()
        ledger = {k: v for k, v in ledger.items() if v}
        if not ledger:
            return "verify-write: nothing recorded yet this process"
        total = sum(len(rows) for rows in ledger.values())
        out = [f"verify-write: {total} tracked action(s) in {len(ledger)} session(s)"]
        for session, rows in sorted(ledger.items()):
            out.append(f"[{session}]")
            out.extend(e.line() for e in rows)
        return "\n".join(out)

    try:
        ctx.register_command("verify-write", _show, description="Show the verify-write ledger for this session")
    except Exception as exc:  # older host without slash commands
        logger.debug("verify-write: register_command unavailable: %s", exc)
