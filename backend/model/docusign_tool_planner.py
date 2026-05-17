"""
Docusign MCP tool planner.

The Docusign MCP server exposes tool schemas dynamically. This planner keeps the
first implementation conservative: deterministic routing for the documented
Navigator/Maestro tools, then argument shaping from simple prompt hints.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass
class DocusignOperation:
    tool_name: str
    arguments: dict[str, Any]
    display: str
    requires_confirmation: bool = False


def _find_tool(tools: list[dict[str, Any]], candidates: list[str]) -> str:
    names = {str(tool.get("name", "")): str(tool.get("name", "")) for tool in tools}
    lowered = {name.lower(): name for name in names.values()}
    for candidate in candidates:
        if candidate in names:
            return candidate
        found = lowered.get(candidate.lower())
        if found:
            return found
    return candidates[0]


def _extract_quoted_value(prompt: str) -> str:
    for quote in ['"', "'"]:
        if quote in prompt:
            parts = prompt.split(quote)
            if len(parts) >= 3 and parts[1].strip():
                return parts[1].strip()
    return ""


def _extract_after(prompt: str, anchors: list[str]) -> str:
    text = prompt.strip()
    lower = text.lower()
    for anchor in anchors:
        idx = lower.rfind(anchor)
        if idx != -1:
            candidate = text[idx + len(anchor):].strip(" :#.\n\t")
            if candidate:
                return candidate.split()[0].strip(",.;")
    return ""


def _agreement_filters(prompt: str) -> dict[str, Any]:
    text = prompt.lower()
    filters: dict[str, Any] = {}

    quoted = _extract_quoted_value(prompt)
    if quoted:
        if "counterparty" in text or "party" in text or "with " in text:
            filters["counterparty"] = quoted
        elif "type" in text:
            filters["agreementType"] = quoted
        else:
            filters["query"] = quoted

    if "active" in text:
        filters["status"] = "active"
    elif "completed" in text or "signed" in text:
        filters["status"] = "completed"
    elif "draft" in text:
        filters["status"] = "draft"

    for kind in ["supplier", "vendor", "customer", "nda", "msa", "sow"]:
        if kind in text:
            filters.setdefault("agreementType", kind.upper() if kind in {"nda", "msa", "sow"} else kind)

    return filters


def _looks_like_confirmation(prompt: str) -> bool:
    text = prompt.lower()
    return any(word in text for word in ["confirm", "approved", "yes trigger", "go ahead"])


def plan_docusign_operation(
    prompt: str,
    intent: str,
    params: dict[str, Any],
    tools: list[dict[str, Any]],
) -> tuple[DocusignOperation, str]:
    from backend.model.llm_clients import call_llm_for_ds_operation
    from backend.config import settings

    # Clean up the schema significantly so Groq context doesn't explode.
    # Keep only the minimal fields the LLM needs: tool `name` and each property's
    # `enum` (if present) or `type`. Do NOT include long descriptions.
    
    prompt_lower = prompt.lower()
    # Basic intent routing to drastically reduce tool count sent to LLM
    target_keywords = ["envelope", "template", "user", "account", "maestro"]
    found_keywords = [kw for kw in target_keywords if kw in prompt_lower]
    
    simplified_tools = []
    for t in tools:
        name = t.get("name", "")
        # If we found keywords, only include tools that match those keywords loosely
        if found_keywords and not any(kw.lower() in name.lower() for kw in found_keywords):
            # Special case for getUserInfo
            if name != "getUserInfo":
                continue
                
        t_clean = {"name": name}
        if "inputSchema" in t and "properties" in t["inputSchema"]:
            props: dict[str, dict] = {}
            for k, v in t["inputSchema"]["properties"].items():
                if k == "accountId":
                    continue
                entry: dict = {}
                if "enum" in v:
                    entry["enum"] = v["enum"]
                else:
                    entry["type"] = v.get("type", "string")
                props[k] = entry
            if props:
                t_clean["properties"] = props
        simplified_tools.append(t_clean)

    # Use LLM to pick tool and arguments
    result_json = call_llm_for_ds_operation(prompt, json.dumps(simplified_tools))
    print(f"RAW GROQ RESULT: {result_json}")
    try:
        parsed = json.loads(result_json)
        tool_name = parsed.get("tool_name", "getTemplates")  # Fallback
        args = parsed.get("arguments", {})
    except Exception:
        tool_name = "getTemplates"
        args = {}

    operation = DocusignOperation(
        tool_name=tool_name,
        arguments=args,
        display=json.dumps({"tool": tool_name, "arguments": args}, indent=2),
    )
    return operation, settings.groq_model
