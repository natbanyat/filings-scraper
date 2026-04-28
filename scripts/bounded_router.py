from __future__ import annotations

"""
Bounded routing helper for low-risk JSON tasks.

Policy:
- refuse prohibited high-consequence task classes
- try the local router first
- fall back only to cheap bounded models that are actually callable here
- return parsed JSON plus route metadata
"""

import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

try:
    from dotenv import load_dotenv
    load_dotenv(_SCRIPTS_DIR.parent / ".env")
except ImportError:
    pass

from utils import extract_json_object
from openclaw_gateway_model import CHEAP_MODEL, GatewayModelError, gateway_model_available, run_text as run_gateway_text

log = logging.getLogger(__name__)

ROUTER_CLIENT = Path("/home/natbanyat/openclaw-router/router_client.py")
ROUTER_SOURCE = "openclaw-wsl"

ALLOWED_TASK_CLASSES = {
    "breaking_triage",
    "bounded_summary",
    "duplicate_check",
    "entity_extraction",
    "materiality_filter",
    "metric_extraction",
    "relevance_scoring",
    "short_extraction",
    "signal_triage",
    "tweet_triage",
}

PROHIBITED_TASK_CLASSES = {
    "architecture",
    "earnings_interpretation",
    "falsifier_relevance",
    "high_consequence",
    "investment_analysis",
    "market_missing",
    "memory_update",
    "thesis_work",
    "valuation",
}


class RoutingPolicyError(RuntimeError):
    pass


class BoundedRoutingError(RuntimeError):
    pass


@dataclass(frozen=True)
class RouteMeta:
    engine: str
    model: str
    tier: str
    reason: str


def available_fallbacks() -> list[str]:
    return ["openclaw-gateway"] if gateway_model_available() else []


def run_json_task(
    *,
    task_class: str,
    prompt: str,
    context: str = "",
    expected: Literal["array", "object"] = "array",
    max_tokens: int = 512,
    prefer_local_heavy: bool = False,
    fallback_order: tuple[str, ...] | None = None,
    router_client: Path = ROUTER_CLIENT,
) -> tuple[Any, RouteMeta]:
    _enforce_task_class(task_class)

    local = _try_local_router(
        prompt=prompt,
        context=context,
        expected=expected,
        max_tokens=max_tokens,
        prefer_local_heavy=prefer_local_heavy,
        router_client=router_client,
    )
    if local is not None:
        return local

    fallbacks = list(fallback_order) if fallback_order is not None else available_fallbacks()
    if not fallbacks:
        raise BoundedRoutingError(
            f"No callable bounded fallbacks available for task_class={task_class}."
        )

    full_prompt = _compose_prompt(prompt=prompt, context=context)
    errors: list[str] = []
    for fallback in fallbacks:
        try:
            raw = _run_fallback_model(fallback, full_prompt, max_tokens=max_tokens)
            parsed = _extract_expected_json(raw, expected)
            return parsed, RouteMeta(
                engine="fallback",
                model=fallback,
                tier="cheap_cloud",
                reason="local router unavailable, delegated, or unparseable",
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{fallback}: {exc}")
            log.warning("Bounded fallback %s failed: %s", fallback, exc)

    raise BoundedRoutingError(
        "All bounded routes failed: " + " | ".join(errors)
    )


def _enforce_task_class(task_class: str) -> None:
    if task_class in PROHIBITED_TASK_CLASSES:
        raise RoutingPolicyError(
            f"Task class {task_class!r} is prohibited from bounded routing."
        )
    if task_class not in ALLOWED_TASK_CLASSES:
        raise RoutingPolicyError(
            f"Task class {task_class!r} is not allowlisted for bounded routing."
        )


def _compose_prompt(*, prompt: str, context: str) -> str:
    if not context.strip():
        return prompt
    return f"{prompt}\n\nSUPPLEMENTAL CONTEXT:\n{context.strip()}"


def _try_local_router(
    *,
    prompt: str,
    context: str,
    expected: Literal["array", "object"],
    max_tokens: int,
    prefer_local_heavy: bool,
    router_client: Path,
) -> tuple[Any, RouteMeta] | None:
    if not router_client.exists():
        log.warning("Local router client missing at %s", router_client)
        return None

    command = [
        sys.executable,
        str(router_client),
        prompt,
        "--max-tokens",
        str(max_tokens),
        "--source",
        ROUTER_SOURCE,
    ]
    if context.strip():
        command.extend(["--context", context])
    if prefer_local_heavy:
        command.append("--poll")

    try:
        proc = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("Local router execution failed: %s", exc)
        return None

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    if not stdout:
        log.warning("Local router returned no stdout. stderr=%s", stderr[:300])
        return None

    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        log.warning("Local router returned non-JSON wrapper: %.300s", stdout)
        return None

    if payload.get("delegate_to_cowork") or payload.get("tier") == "cowork":
        return None
    if not payload.get("success", False):
        return None

    response = str(payload.get("response", "")).strip()
    if not response:
        return None

    try:
        parsed = _extract_expected_json(response, expected)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "Local router response was not valid %s JSON: %s. Raw: %.300s",
            expected,
            exc,
            response,
        )
        return None

    return parsed, RouteMeta(
        engine="local_router",
        model=str(payload.get("tier") or "local_router"),
        tier=str(payload.get("tier") or "local_router"),
        reason=str(payload.get("reason") or "local router success"),
    )


def _run_fallback_model(name: str, prompt: str, *, max_tokens: int) -> str:
    if name == "openclaw-gateway":
        return _run_openclaw_gateway(prompt)
    raise BoundedRoutingError(f"Unknown bounded fallback: {name}")


def _run_openclaw_gateway(prompt: str) -> str:
    model = os.environ.get("OPENCLAW_BOUNDED_MODEL") or os.environ.get("OPENCLAW_PIPELINE_CHEAP_MODEL") or CHEAP_MODEL
    try:
        return run_gateway_text(prompt, model=model, timeout=180)
    except GatewayModelError as exc:
        raise BoundedRoutingError(str(exc)) from exc


def _extract_expected_json(text: str, expected: Literal["array", "object"]) -> Any:
    if expected == "array":
        json_text = _extract_json_array(text)
    else:
        json_text = extract_json_object(text)
    if not json_text:
        raise BoundedRoutingError(f"No JSON {expected} found")
    return json.loads(json_text)


def _extract_json_array(text: str) -> str | None:
    start = text.find("[")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        char = text[i]
        if escape:
            escape = False
            continue
        if char == "\\":
            escape = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None
