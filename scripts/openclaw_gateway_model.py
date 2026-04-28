from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any, Literal

from utils import extract_json_object


DEFAULT_MODEL = os.environ.get("OPENCLAW_PIPELINE_MODEL", "openai-codex/gpt-5.4")
DEEP_MODEL = os.environ.get("OPENCLAW_PIPELINE_DEEP_MODEL", DEFAULT_MODEL)
CHEAP_MODEL = os.environ.get("OPENCLAW_PIPELINE_CHEAP_MODEL", "openai-codex/gpt-5.4")
PRO_MODEL = os.environ.get("OPENCLAW_PIPELINE_PRO_MODEL", DEFAULT_MODEL)
DEFAULT_TIMEOUT_SEC = int(os.environ.get("OPENCLAW_PIPELINE_TIMEOUT_SEC", "180"))
OPENCLAW_BIN = (
    os.environ.get("OPENCLAW_BIN")
    or ("/home/natbanyat/.npm-global/bin/openclaw" if os.path.exists("/home/natbanyat/.npm-global/bin/openclaw") else None)
    or shutil.which("openclaw")
    or "openclaw"
)


class GatewayModelError(RuntimeError):
    pass


def gateway_model_available() -> bool:
    if OPENCLAW_BIN == "openclaw":
        return shutil.which("openclaw") is not None
    return shutil.which(OPENCLAW_BIN) is not None or os.path.exists(OPENCLAW_BIN)


def run_text(
    prompt: str,
    *,
    model: str | None = None,
    timeout: int = DEFAULT_TIMEOUT_SEC,
) -> str:
    resolved_model = DEFAULT_MODEL if model is None else model
    command = [OPENCLAW_BIN, "capability", "model", "run", "--gateway", "--json"]
    if resolved_model:
        command.extend(["--model", resolved_model])
    command.extend(["--prompt", prompt])

    try:
        proc = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001
        raise GatewayModelError(f"OpenClaw gateway model run failed to start: {exc}") from exc

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()

    if proc.returncode != 0:
        detail = stderr or stdout or f"exit {proc.returncode}"
        raise GatewayModelError(detail)

    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise GatewayModelError(f"OpenClaw gateway returned non-JSON output: {stdout[:400]}") from exc

    outputs = payload.get("outputs") or []
    text = "".join(str(item.get("text", "")) for item in outputs).strip()
    if not text:
        raise GatewayModelError(f"OpenClaw gateway returned no text: {stdout[:400]}")
    return text


def run_json(
    prompt: str,
    *,
    expected: Literal["array", "object"] = "object",
    model: str | None = None,
    timeout: int = DEFAULT_TIMEOUT_SEC,
) -> Any:
    text = run_text(prompt, model=model, timeout=timeout)
    json_text = _extract_json_array(text) if expected == "array" else extract_json_object(text)
    if not json_text:
        raise GatewayModelError(f"No JSON {expected} found in model response")
    try:
        return json.loads(json_text)
    except json.JSONDecodeError as exc:
        raise GatewayModelError(f"Invalid JSON {expected}: {exc}") from exc


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
