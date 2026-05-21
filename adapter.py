"""Business API platform plugin for Hermes.

This adapter intentionally reuses the built-in API server's Responses
implementation and response_store.db. It adds a small business control plane:

* ``GET /api/responses/{response_id}/context`` returns the transcript snapshot
  and usage/model metadata for the end of that response.
* ``POST /api/files`` uploads a file under a configured workspace root.
* ``GET /api/files`` downloads a file from the configured workspace root.

The plugin runs as its own gateway platform and port so it does not require
core route-extension hooks in ``gateway.platforms.api_server``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import socket as _socket
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import quote

try:
    from aiohttp import web

    AIOHTTP_AVAILABLE = True
except ImportError:  # pragma: no cover - aiohttp is available in gateway installs
    AIOHTTP_AVAILABLE = False
    web = None  # type: ignore[assignment]

from gateway.config import Platform, PlatformConfig
from gateway.platforms.api_server import (
    APIServerAdapter,
    _openai_error,
    cors_middleware,
    security_headers_middleware,
)
from gateway.platforms.base import SendResult, is_network_accessible

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_WORKSPACE_ROOT = "/opt/workspace"
DEFAULT_MAX_UPLOAD_BYTES = 100 * 1024 * 1024
MAX_FILE_FIELD_BYTES = 256 * 1024
# Block dangerous characters (controls, path separators, Windows forbidden).
# Chinese and other Unicode is preserved.
_INVALID_FILENAME_CHARS = re.compile(r"[\x00-\x1f\x7f\\/:*?\"<>|]+")
_CONTROL_CHARS = re.compile(r"[\r\n\x00]")


def _coerce_port(value: Any, default: int = DEFAULT_PORT) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _config_value(extra: dict, key: str, env_name: str, default: Any = "") -> Any:
    value = extra.get(key)
    if value in {None, ""}:
        value = os.getenv(env_name, default)
    return value


def _safe_filename(filename: str) -> str:
    name = Path(str(filename or "")).name.strip().strip(".")
    name = _INVALID_FILENAME_CHARS.sub("_", name)
    name = re.sub(r"\s+", " ", name).strip()
    if not name:
        name = f"upload-{uuid.uuid4().hex[:12]}.bin"
    return name[:180]


class BusinessAPIAdapter(APIServerAdapter):
    """Business-facing HTTP adapter with Hermes response context inspection."""

    def __init__(self, config: PlatformConfig):
        super().__init__(config)
        extra = config.extra or {}
        self.platform = Platform("business_api")
        self._host = str(_config_value(extra, "host", "BUSINESS_API_HOST", DEFAULT_HOST) or DEFAULT_HOST)
        self._port = _coerce_port(
            _config_value(extra, "port", "BUSINESS_API_PORT", DEFAULT_PORT),
            DEFAULT_PORT,
        )
        self._api_key = str(_config_value(extra, "key", "BUSINESS_API_KEY", "") or "")
        self._workspace_root = self._resolve_workspace_root(
            _config_value(extra, "workspace_root", "BUSINESS_API_WORKSPACE_ROOT", DEFAULT_WORKSPACE_ROOT)
        )
        self._max_upload_bytes = _coerce_int(
            _config_value(extra, "max_upload_bytes", "BUSINESS_API_MAX_UPLOAD_BYTES", DEFAULT_MAX_UPLOAD_BYTES),
            DEFAULT_MAX_UPLOAD_BYTES,
        )
        self._turn_usage_by_task: dict[int, Dict[str, Any]] = {}
        self._last_agent: Any = None

    @property
    def name(self) -> str:
        return "Business API"

    @staticmethod
    def _resolve_workspace_root(value: Any) -> Path:
        root = Path(str(value or DEFAULT_WORKSPACE_ROOT)).expanduser()
        try:
            root.mkdir(parents=True, exist_ok=True)
            return root.resolve()
        except Exception:
            fallback = Path(DEFAULT_WORKSPACE_ROOT)
            try:
                fallback.mkdir(parents=True, exist_ok=True)
                return fallback.resolve()
            except Exception:
                # Last resort: stay profile-local rather than failing import.
                from hermes_constants import get_hermes_home

                local = get_hermes_home() / "business-api-workspace"
                local.mkdir(parents=True, exist_ok=True)
                return local.resolve()

    def _create_agent(self, *args, **kwargs) -> Any:
        """Create an agent with the business_api platform/toolset identity.

        Mirrors ``APIServerAdapter._create_agent`` but resolves the platform
        toolset for ``business_api``. If no explicit platform_toolsets entry is
        configured, Hermes auto-generates ``hermes-business_api`` for plugin
        platforms, which includes the core tools.
        """
        from run_agent import AIAgent
        from gateway.run import (
            GatewayRunner,
            _load_gateway_config,
            _resolve_gateway_model,
            _resolve_runtime_agent_kwargs,
        )
        from hermes_cli.tools_config import _get_platform_tools

        ephemeral_system_prompt = kwargs.get("ephemeral_system_prompt")
        session_id = kwargs.get("session_id")
        stream_delta_callback = kwargs.get("stream_delta_callback")
        tool_progress_callback = kwargs.get("tool_progress_callback")
        tool_start_callback = kwargs.get("tool_start_callback")
        tool_complete_callback = kwargs.get("tool_complete_callback")
        gateway_session_key = kwargs.get("gateway_session_key")

        runtime_kwargs = _resolve_runtime_agent_kwargs()
        reasoning_config = GatewayRunner._load_reasoning_config()
        model = _resolve_gateway_model()
        user_config = _load_gateway_config()
        enabled_toolsets = sorted(_get_platform_tools(user_config, "business_api"))
        max_iterations = int(os.getenv("HERMES_MAX_ITERATIONS", "90"))
        fallback_model = GatewayRunner._load_fallback_model()

        agent = AIAgent(
            model=model,
            **runtime_kwargs,
            max_iterations=max_iterations,
            quiet_mode=True,
            verbose_logging=False,
            ephemeral_system_prompt=ephemeral_system_prompt or None,
            enabled_toolsets=enabled_toolsets,
            session_id=session_id,
            platform="business_api",
            stream_delta_callback=stream_delta_callback,
            tool_progress_callback=tool_progress_callback,
            tool_start_callback=tool_start_callback,
            tool_complete_callback=tool_complete_callback,
            session_db=self._ensure_session_db(),
            fallback_model=fallback_model,
            reasoning_config=reasoning_config,
            gateway_session_key=gateway_session_key,
        )

        # Store so _context_window_info can read compression info after
        # run_in_executor finishes (agent is created inside a thread).
        self._last_agent = agent

        return agent

    async def connect(self) -> bool:
        """Start the business API aiohttp server."""
        if not AIOHTTP_AVAILABLE:
            logger.warning("[%s] aiohttp not installed", self.name)
            return False

        try:
            self._workspace_root.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            logger.error("[%s] Could not create workspace root %s: %s", self.name, self._workspace_root, exc)
            return False

        if is_network_accessible(self._host) and not self._api_key:
            logger.error(
                "[%s] Refusing to start: binding to %s requires BUSINESS_API_KEY.",
                self.name,
                self._host,
            )
            return False

        if is_network_accessible(self._host) and self._api_key:
            try:
                from hermes_cli.auth import has_usable_secret

                if not has_usable_secret(self._api_key, min_length=8):
                    logger.error("[%s] Refusing to start with placeholder BUSINESS_API_KEY.", self.name)
                    return False
            except ImportError:
                pass

        try:
            with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as sock:
                sock.settimeout(1)
                sock.connect(("127.0.0.1", self._port))
            logger.error("[%s] Port %d already in use.", self.name, self._port)
            return False
        except (ConnectionRefusedError, OSError):
            pass

        try:
            mws = [mw for mw in (cors_middleware, security_headers_middleware) if mw is not None]
            self._app = web.Application(middlewares=mws, client_max_size=self._max_upload_bytes)
            self._app["api_server_adapter"] = self
            self._app.router.add_get("/health", self._handle_health)
            self._app.router.add_get("/v1/health", self._handle_health)
            self._app.router.add_get("/v1/models", self._handle_models)
            self._app.router.add_get("/v1/capabilities", self._handle_capabilities)
            self._app.router.add_post("/v1/responses", self._handle_responses)
            self._app.router.add_get("/v1/responses/{response_id}", self._handle_get_response)
            self._app.router.add_delete("/v1/responses/{response_id}", self._handle_delete_response)
            self._app.router.add_get(
                "/api/responses/{response_id}/context",
                self._handle_response_context,
            )
            self._app.router.add_post("/api/files", self._handle_file_upload)
            self._app.router.add_get("/api/files", self._handle_file_download)

            self._runner = web.AppRunner(self._app)
            await self._runner.setup()
            self._site = web.TCPSite(self._runner, self._host, self._port)
            await self._site.start()

            self._mark_connected()
            if not self._api_key:
                logger.warning("[%s] No BUSINESS_API_KEY configured; only safe for loopback development.", self.name)
            logger.info(
                "[%s] listening on http://%s:%d (workspace: %s)",
                self.name,
                self._host,
                self._port,
                self._workspace_root,
            )
            return True
        except Exception as exc:
            logger.error("[%s] Failed to start: %s", self.name, exc, exc_info=True)
            return False

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        del chat_id, content, reply_to, metadata
        return SendResult(success=False, error="Business API uses HTTP request/response, not send().")

    def _extended_usage_from_result(self, result: Dict[str, Any], fallback_usage: Dict[str, int]) -> Dict[str, Any]:
        input_tokens = int(result.get("input_tokens") or fallback_usage.get("input_tokens") or 0)
        output_tokens = int(result.get("output_tokens") or fallback_usage.get("output_tokens") or 0)
        cache_read = int(result.get("cache_read_tokens") or 0)
        cache_write = int(result.get("cache_write_tokens") or 0)
        reasoning = int(result.get("reasoning_tokens") or 0)
        prompt_tokens = int(result.get("prompt_tokens") or (input_tokens + cache_read + cache_write))
        completion_tokens = int(result.get("completion_tokens") or output_tokens)
        total_tokens = int(
            result.get("total_tokens")
            or fallback_usage.get("total_tokens")
            or (prompt_tokens + completion_tokens)
        )
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_tokens": cache_read,
            "cache_write_tokens": cache_write,
            "reasoning_tokens": reasoning,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        }

    async def _run_agent(self, *args, **kwargs) -> tuple:
        result, usage = await super()._run_agent(*args, **kwargs)
        if isinstance(result, dict):
            usage = self._extended_usage_from_result(result, usage or {})
            # Enrich usage with context window and compression info.
            usage.update(self._context_window_info())
            task = asyncio.current_task()
            if task is not None:
                usage_meta = dict(usage)
                if result.get("session_id"):
                    usage_meta["_session_id"] = result["session_id"]
                self._turn_usage_by_task[id(task)] = usage_meta
        return result, usage

    def _context_window_info(self) -> Dict[str, Any]:
        """Read context window usage and compression count from the agent."""
        info: Dict[str, Any] = {}
        agent = self._last_agent
        if agent is None:
            return info

        cc = getattr(agent, "context_compressor", None)
        if cc is not None:
            context_length = getattr(cc, "context_length", 0) or 0
            last_prompt = getattr(cc, "last_prompt_tokens", 0) or 0
            compression_count = getattr(cc, "compression_count", 0) or 0

            if context_length > 0:
                info["context_window"] = context_length
                info["context_used"] = last_prompt
                usage_pct = min(100, round(last_prompt / context_length * 100, 1))
                info["context_usage_pct"] = usage_pct

            if compression_count > 0:
                info["compression_count"] = compression_count

        self._last_agent = None
        return info

    def _enrich_stored_for_streaming(self, response_ids: list) -> None:
        """Enrich the ResponseStore snapshot after a streaming response.

        The parent's SSE writer persists a snapshot with only 3 usage fields.
        We add extended usage, context window info, and session_id here.
        """
        if not response_ids:
            return

        # Pop the most recent enriched usage entry from the task dict.
        usage = None
        if self._turn_usage_by_task:
            usage = self._turn_usage_by_task.pop(next(iter(self._turn_usage_by_task)))

        if not usage:
            return

        actual_session_id = usage.pop("_session_id", None) if isinstance(usage, dict) else None

        for rid in response_ids:
            try:
                stored = self._response_store.get(rid)
                if not isinstance(stored, dict):
                    continue
                if stored.get("turn_usage"):
                    # Already enriched (e.g. non-streaming call).
                    continue
                if isinstance(usage, dict) and usage:
                    stored["turn_usage"] = usage
                    stored_response = stored.get("response")
                    if isinstance(stored_response, dict):
                        stored_response["usage"] = usage
                if actual_session_id:
                    stored["session_id"] = actual_session_id
                self._response_store.put(rid, stored)
            except Exception:
                logger.debug("Failed to enrich streaming response metadata for %s", rid, exc_info=True)

    async def _handle_responses(self, request: "web.Request") -> "web.Response":
        """Run the base Responses handler, then enrich stored usage metadata.

        For non-streaming: enriches the response body and stored data inline.
        For streaming: the SSE writer in the parent writes everything directly
        to the socket and persists a snapshot with only 3 usage fields; we
        enrich that stored snapshot afterward with extended usage and context
        window info.
        """
        task = asyncio.current_task()
        task_key = id(task) if task is not None else 0

        # For streaming we need to discover which response_ids were written
        # to the store, since POST /v1/responses has no response_id in the URL.
        captured_ids: list = []
        original_put = self._response_store.put

        def _capturing_put(rid, data, **kw):
            captured_ids.append(rid)
            return original_put(rid, data, **kw)

        self._response_store.put = _capturing_put

        try:
            response = await super()._handle_responses(request)
        finally:
            self._response_store.put = original_put

        # Streaming path — parent returns StreamResponse, enrichment code
        # below won't apply to the body, but we can still enrich the
        # ResponseStore snapshot that _write_sse_responses persisted.
        if not isinstance(response, web.Response):
            self._enrich_stored_for_streaming(captured_ids)
            return response

        if response.content_type != "application/json":
            return response

        try:
            payload = response.body
            if not payload:
                return response
            import json

            data = json.loads(payload.decode(response.charset or "utf-8"))
            response_id = data.get("id")
            if not response_id or data.get("object") != "response":
                return response
            stored = self._response_store.get(response_id)
            if not isinstance(stored, dict):
                return response
            usage = self._turn_usage_by_task.pop(task_key, None) or data.get("usage") or {}
            actual_session_id = None
            if isinstance(usage, dict):
                actual_session_id = usage.pop("_session_id", None)
            if isinstance(usage, dict):
                stored["turn_usage"] = usage
                data["usage"] = usage
            if actual_session_id:
                stored["session_id"] = actual_session_id
            stored["model"] = data.get("model")
            stored["created_at"] = data.get("created_at")
            stored_response = stored.get("response")
            if isinstance(stored_response, dict):
                stored_response["usage"] = usage
            self._response_store.put(response_id, stored)
            headers = {
                key: value
                for key, value in response.headers.items()
                if key.lower() not in {"content-type", "content-length"}
            }
            response = web.json_response(data, headers=headers, status=response.status)
        except Exception:
            logger.debug("Failed to enrich business response metadata", exc_info=True)
        return response

    async def _handle_response_context(self, request: "web.Request") -> "web.Response":
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        response_id = request.match_info["response_id"]
        if _CONTROL_CHARS.search(response_id):
            return web.json_response(_openai_error("Invalid response id"), status=400)

        stored = self._response_store.get(response_id)
        if stored is None:
            return web.json_response(_openai_error(f"Response not found: {response_id}"), status=404)

        response_obj = stored.get("response") or {}
        session_id = stored.get("session_id")
        session = None
        session_usage = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "reasoning_tokens": 0,
            "total_tokens": 0,
            "api_call_count": 0,
            "estimated_cost_usd": None,
            "cost_status": None,
            "cost_source": None,
            "billing_provider": None,
            "billing_base_url": None,
            "billing_mode": None,
            "model": response_obj.get("model") or stored.get("model"),
        }
        if session_id:
            try:
                db = self._ensure_session_db()
                session = db.get_session(session_id) if db is not None else None
            except Exception:
                session = None
        if isinstance(session, dict):
            input_tokens = int(session.get("input_tokens") or 0)
            output_tokens = int(session.get("output_tokens") or 0)
            cache_read = int(session.get("cache_read_tokens") or 0)
            cache_write = int(session.get("cache_write_tokens") or 0)
            reasoning = int(session.get("reasoning_tokens") or 0)
            session_usage.update(
                {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cache_read_tokens": cache_read,
                    "cache_write_tokens": cache_write,
                    "reasoning_tokens": reasoning,
                    "total_tokens": input_tokens + output_tokens + cache_read + cache_write,
                    "api_call_count": int(session.get("api_call_count") or 0),
                    "estimated_cost_usd": session.get("estimated_cost_usd"),
                    "cost_status": session.get("cost_status"),
                    "cost_source": session.get("cost_source"),
                    "billing_provider": session.get("billing_provider"),
                    "billing_base_url": session.get("billing_base_url"),
                    "billing_mode": session.get("billing_mode"),
                    "model": session.get("model") or session_usage["model"],
                }
            )

        include_messages = str(request.query.get("include_messages", "true")).strip().lower()
        include_messages_bool = include_messages not in {"0", "false", "no", "off"}
        response_model = response_obj.get("model") or stored.get("model")
        actual_model = session_usage.get("model") or response_model
        turn = stored.get("turn_usage") or response_obj.get("usage") or {}
        context = {
            "object": "business_api.response_context",
            "response_id": response_id,
            "session_id": session_id,
            "status": response_obj.get("status"),
            "created_at": response_obj.get("created_at") or stored.get("created_at"),
            "model": actual_model,
            "response_model": response_model,
            "usage": {
                "turn": turn,
                "session_total": session_usage,
            },
            "context_window": {
                "total": turn.get("context_window"),
                "used": turn.get("context_used"),
                "usage_pct": turn.get("context_usage_pct"),
                "compression_count": turn.get("compression_count"),
            },
            "instructions": stored.get("instructions"),
            "response": response_obj,
        }
        if include_messages_bool:
            context["messages"] = stored.get("conversation_history") or []
        return web.json_response(context)

    def _resolve_upload_target(self, target_path: str) -> Path:
        raw = str(target_path or "").strip()
        if _CONTROL_CHARS.search(raw):
            raise ValueError("target_path contains invalid control characters")

        if not raw:
            base = self._workspace_root
        else:
            raw_path = Path(raw)
            base = None
            if raw_path.is_absolute():
                resolved_raw = raw_path.resolve()
                try:
                    resolved_raw.relative_to(self._workspace_root)
                    base = resolved_raw
                except ValueError:
                    # A leading slash is accepted as a workspace-root relative
                    # path unless it is a real absolute path inside the root.
                    if re.match(r"^[A-Za-z]:[\\/]", raw):
                        raise ValueError("target_path must stay inside workspace_root")

            if base is None:
                base = self._workspace_root / raw.lstrip("/\\")

        resolved = base.resolve()
        try:
            resolved.relative_to(self._workspace_root)
        except ValueError as exc:
            raise ValueError("target_path must stay inside workspace_root") from exc
        return resolved

    def _validate_download_filename(self, file_name: str) -> str:
        raw = str(file_name or "").strip()
        if not raw:
            raise ValueError("file_name is required")
        if _CONTROL_CHARS.search(raw):
            raise ValueError("file_name contains invalid control characters")
        if raw in {".", ".."} or "/" in raw or "\\" in raw:
            raise ValueError("file_name must be a plain file name")
        return raw

    def _resolve_download_target(self, target_path: str, file_name: str) -> Path:
        target_dir = self._resolve_upload_target(target_path)
        safe_name = self._validate_download_filename(file_name)
        target = (target_dir / safe_name).resolve()
        try:
            target.relative_to(self._workspace_root)
        except ValueError as exc:
            raise ValueError("file path must stay inside workspace_root") from exc
        return target

    @staticmethod
    def _content_disposition(file_name: str) -> str:
        fallback = re.sub(r'[^A-Za-z0-9._-]+', "_", file_name).strip("._")
        if not fallback:
            fallback = "download.bin"
        return f'attachment; filename="{fallback[:180]}"; filename*=UTF-8\'\'{quote(file_name)}'

    async def _handle_file_upload(self, request: "web.Request") -> "web.Response":
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        if not request.content_type.startswith("multipart/"):
            return web.json_response(_openai_error("Expected multipart/form-data.", code="invalid_content_type"), status=400)

        try:
            reader = await request.multipart()
        except Exception:
            return web.json_response(_openai_error("Invalid multipart request.", code="invalid_multipart"), status=400)

        target_path = ""
        overwrite = False
        conversation_id = ""
        saved = None

        try:
            while True:
                part = await reader.next()
                if part is None:
                    break
                if part.name == "target_path":
                    target_path = (await part.text())[:MAX_FILE_FIELD_BYTES]
                elif part.name == "overwrite":
                    overwrite = _truthy((await part.text())[:64])
                elif part.name == "conversation_id":
                    conversation_id = (await part.text())[:256]
                elif part.name == "file":
                    target_dir = self._resolve_upload_target(target_path)
                    safe_name = _safe_filename(part.filename or "")
                    target_dir.mkdir(parents=True, exist_ok=True)
                    dest = target_dir / safe_name
                    if not overwrite:
                        stem = dest.stem
                        suffix = dest.suffix
                        counter = 1
                        while dest.exists():
                            dest = target_dir / f"{stem}-{counter}{suffix}"
                            counter += 1
                    else:
                        dest = dest.resolve()
                        try:
                            dest.relative_to(self._workspace_root)
                        except ValueError as exc:
                            raise ValueError("upload destination must stay inside workspace_root") from exc

                    total = 0
                    with dest.open("wb") as handle:
                        while True:
                            chunk = await part.read_chunk(size=1024 * 1024)
                            if not chunk:
                                break
                            total += len(chunk)
                            if total > self._max_upload_bytes:
                                try:
                                    dest.unlink(missing_ok=True)
                                except Exception:
                                    pass
                                return web.json_response(
                                    _openai_error("Uploaded file too large.", code="file_too_large"),
                                    status=413,
                                )
                            handle.write(chunk)
                    saved = {
                        "file_id": f"file_{uuid.uuid4().hex[:24]}",
                        "filename": safe_name,
                        "path": str(dest),
                        "workspace_root": str(self._workspace_root),
                        "size": total,
                        "conversation_id": conversation_id or None,
                        "created_at": int(time.time()),
                    }
                else:
                    # Drain ignored fields so aiohttp can continue parsing.
                    await part.release()
        except ValueError as exc:
            return web.json_response(_openai_error(str(exc), code="invalid_target_path"), status=400)
        except Exception as exc:
            logger.error("[%s] upload failed: %s", self.name, exc, exc_info=True)
            return web.json_response(_openai_error(f"Upload failed: {exc}", err_type="server_error"), status=500)

        if saved is None:
            return web.json_response(_openai_error("Missing file field.", code="missing_file"), status=400)
        return web.json_response({"object": "business_api.file", **saved}, status=201)

    async def _handle_file_download(self, request: "web.Request") -> "web.StreamResponse":
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        try:
            target = self._resolve_download_target(
                request.query.get("path", ""),
                request.query.get("file_name", ""),
            )
        except ValueError as exc:
            return web.json_response(_openai_error(str(exc), code="invalid_file_path"), status=400)

        if not target.exists() or not target.is_file():
            return web.json_response(_openai_error("File not found.", code="file_not_found"), status=404)

        headers = {
            "Content-Disposition": self._content_disposition(target.name),
            "X-Business-API-Workspace-Root": str(self._workspace_root),
        }
        return web.FileResponse(path=target, headers=headers)


def check_requirements() -> bool:
    return AIOHTTP_AVAILABLE and _business_api_configured()


def validate_config(config: PlatformConfig) -> bool:
    extra = config.extra or {}
    return bool(extra) or _truthy(os.getenv("BUSINESS_API_ENABLED")) or bool(os.getenv("BUSINESS_API_KEY"))


def _business_api_configured() -> bool:
    """Return True only when the operator explicitly configured the API."""
    if _truthy(os.getenv("BUSINESS_API_ENABLED")) or os.getenv("BUSINESS_API_KEY"):
        return True
    try:
        from hermes_cli.config import read_raw_config

        cfg = read_raw_config()
    except Exception:
        cfg = {}
    candidates = []
    if isinstance(cfg, dict):
        top = cfg.get("business_api")
        if isinstance(top, dict):
            candidates.append(top)
        platforms = cfg.get("platforms")
        if isinstance(platforms, dict):
            nested = platforms.get("business_api")
            if isinstance(nested, dict):
                candidates.append(nested)
    for block in candidates:
        if _truthy(block.get("enabled")):
            return True
        extra = block.get("extra")
        if isinstance(extra, dict) and extra.get("key"):
            return True
        if block.get("key"):
            return True
    return False


def _env_enablement() -> Optional[dict]:
    if not (_truthy(os.getenv("BUSINESS_API_ENABLED")) or os.getenv("BUSINESS_API_KEY")):
        return None
    seed: dict[str, Any] = {}
    for env_name, key in (
        ("BUSINESS_API_HOST", "host"),
        ("BUSINESS_API_PORT", "port"),
        ("BUSINESS_API_KEY", "key"),
        ("BUSINESS_API_WORKSPACE_ROOT", "workspace_root"),
        ("BUSINESS_API_MAX_UPLOAD_BYTES", "max_upload_bytes"),
    ):
        value = os.getenv(env_name)
        if value:
            seed[key] = int(value) if key in {"port", "max_upload_bytes"} and value.isdigit() else value
    return seed


def _apply_yaml_config(yaml_cfg: dict, platform_cfg: dict) -> Optional[dict]:
    del yaml_cfg
    extra = dict(platform_cfg.get("extra") or {})
    for key in ("host", "port", "key", "workspace_root", "max_upload_bytes", "cors_origins", "model_name"):
        if key in platform_cfg and key not in extra:
            extra[key] = platform_cfg[key]
    if _truthy(platform_cfg.get("enabled")) and not os.getenv("BUSINESS_API_ENABLED"):
        os.environ["BUSINESS_API_ENABLED"] = "true"
    if extra.get("key") and not os.getenv("BUSINESS_API_KEY"):
        os.environ["BUSINESS_API_KEY"] = str(extra["key"])
    return extra


def register(ctx) -> None:
    ctx.register_platform(
        name="business_api",
        label="Business API",
        adapter_factory=lambda cfg: BusinessAPIAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=validate_config,
        required_env=[],
        install_hint="pip install aiohttp",
        env_enablement_fn=_env_enablement,
        apply_yaml_config_fn=_apply_yaml_config,
        emoji="API",
        allow_update_command=False,
        platform_hint=(
            "Requests arrive through a private business HTTP API. Prefer concise, "
            "machine-readable answers when the user asks for structured output."
        ),
    )
