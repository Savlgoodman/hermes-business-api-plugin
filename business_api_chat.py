"""Interactive smoke tester for the Hermes Business API platform.

Usage:
  set BUSINESS_API_KEY=...
  uv run python scripts/business_api_chat.py

The script keeps ``previous_response_id`` between turns and prints the
response-context usage metadata after each assistant reply.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

import requests


def _format_tokens(n: int) -> str:
    """Format token count with K/M suffix for display."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _load_hermes_env() -> None:
    """Load ~/.hermes/.env for direct python script runs."""
    env_path = os.path.expanduser("~/.hermes/.env")
    if not os.path.exists(env_path):
        return
    try:
        with open(env_path, "r", encoding="utf-8-sig") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip("'\"")
                if key and key not in os.environ:
                    os.environ[key] = value
    except OSError:
        return


def _usage_line(usage: dict[str, Any]) -> str:
    keys = (
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "reasoning_tokens",
        "total_tokens",
        "api_call_count",
        "estimated_cost_usd",
    )
    parts = []
    for key in keys:
        value = usage.get(key)
        if value not in (None, "", 0):
            parts.append(f"{key}={value}")
    return ", ".join(parts) if parts else "(no usage yet)"


def _output_text(response: dict[str, Any]) -> str:
    texts: list[str] = []
    for item in response.get("output") or []:
        if isinstance(item, dict):
            for content in item.get("content") or []:
                if isinstance(content, dict) and content.get("text"):
                    texts.append(str(content["text"]))
    return "\n".join(texts).strip() or json.dumps(response.get("output"), ensure_ascii=False, indent=2)


def _request_json(method: str, url: str, *, headers: dict[str, str], **kwargs: Any) -> dict[str, Any]:
    resp = requests.request(method, url, headers=headers, timeout=kwargs.pop("timeout", 600), **kwargs)
    if resp.status_code >= 400:
        raise RuntimeError(f"{method} {url} failed: HTTP {resp.status_code}\n{resp.text}")
    return resp.json()


def main() -> int:
    _load_hermes_env()

    parser = argparse.ArgumentParser(description="Chat through Hermes Business API and print context metadata.")
    parser.add_argument("--base-url", default=os.getenv("BUSINESS_API_BASE_URL", "http://127.0.0.1:8765"))
    parser.add_argument("--api-key", default=os.getenv("BUSINESS_API_KEY"))
    parser.add_argument("--model", default=os.getenv("BUSINESS_API_MODEL", ""))
    parser.add_argument("--conversation", default=os.getenv("BUSINESS_API_CONVERSATION", "business-api-smoke"))
    parser.add_argument("--system", default=os.getenv("BUSINESS_API_SYSTEM", ""))
    parser.add_argument("--once", default="", help="Send one message and exit.")
    parser.add_argument("--no-context-messages", action="store_true")
    args = parser.parse_args()

    if not args.api_key:
        print("Missing BUSINESS_API_KEY. Set it in the environment or pass --api-key.", file=sys.stderr)
        return 2

    base_url = args.base_url.rstrip("/")
    headers = {"Authorization": f"Bearer {args.api_key}", "Content-Type": "application/json"}
    previous_response_id: str | None = None

    try:
        health = _request_json("GET", f"{base_url}/health", headers=headers, timeout=30)
        print(f"Connected: {health}")
    except Exception as exc:
        print(f"Could not reach Business API at {base_url}: {exc}", file=sys.stderr)
        return 1

    def send_turn(message: str) -> None:
        nonlocal previous_response_id
        payload: dict[str, Any] = {
            "input": message,
            "store": True,
        }
        if previous_response_id:
            payload["previous_response_id"] = previous_response_id
        elif args.conversation:
            payload["conversation"] = args.conversation
        if args.model:
            payload["model"] = args.model
        if args.system:
            payload["instructions"] = args.system

        started = time.time()
        response = _request_json("POST", f"{base_url}/v1/responses", headers=headers, json=payload)
        previous_response_id = response["id"]

        # Usage is already included in the /v1/responses response body.
        # Only call /context endpoint when session-level cumulative usage
        # or full conversation history is needed.
        resp_usage = response.get("usage") or {}

        print("\nAssistant:")
        print(_output_text(response))
        print("\nUsage:")
        print(f"  response_id: {previous_response_id}")
        print(f"  model:       {response.get('model')}")
        print(f"  latency:     {time.time() - started:.2f}s")
        print(f"  turn:        {_usage_line(resp_usage)}")

        # Display context window occupancy if available
        ctx_total = resp_usage.get("context_window")
        ctx_used = resp_usage.get("context_used")
        if ctx_total and ctx_used is not None:
            pct = resp_usage.get("context_usage_pct", "??")
            comp = resp_usage.get("compression_count", 0)
            comp_note = f", compressed {comp}x" if comp else ""
            print(f"  context:     {_format_tokens(ctx_used)}/{_format_tokens(ctx_total)} ({pct}%{comp_note})")

        # Fetch session-level cumulative usage from context endpoint
        if not args.no_context_messages:
            include = "true"
        else:
            include = "false"
        try:
            context = _request_json(
                "GET",
                f"{base_url}/api/responses/{previous_response_id}/context?include_messages={include}",
                headers=headers,
                timeout=30,
            )
            session_usage = (context.get("usage") or {}).get("session_total") or {}
            session_total_tokens = session_usage.get("total_tokens", 0)
            if session_total_tokens > 0:
                print(f"  session:     {_usage_line(session_usage)}")
            if not args.no_context_messages:
                print(f"  messages:    {len(context.get('messages') or [])}")
        except Exception:
            # Context endpoint is optional — don't fail the turn if unavailable
            pass

    if args.once:
        send_turn(args.once)
        return 0

    print("Type a message. Use /quit to exit, /reset to clear previous_response_id.")
    while True:
        try:
            message = input("\nYou> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not message:
            continue
        if message in {"/quit", "/exit"}:
            return 0
        if message == "/reset":
            previous_response_id = None
            print("Conversation chain reset.")
            continue
        try:
            send_turn(message)
        except Exception as exc:
            print(f"Request failed: {exc}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
