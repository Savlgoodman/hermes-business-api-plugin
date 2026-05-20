"""Upload and download a tiny 123.txt file through the Hermes Business API.

Usage:
  python scripts/business_api_upload_smoke.py
"""

from __future__ import annotations

import argparse
import os
import sys
from io import BytesIO
from typing import Any

import requests


def _load_hermes_env() -> None:
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


def _request_json(method: str, url: str, *, headers: dict[str, str], **kwargs: Any) -> dict[str, Any]:
    resp = requests.request(method, url, headers=headers, timeout=kwargs.pop("timeout", 60), **kwargs)
    if resp.status_code >= 400:
        raise RuntimeError(f"{method} {url} failed: HTTP {resp.status_code}\n{resp.text}")
    return resp.json()


def _request_bytes(method: str, url: str, *, headers: dict[str, str], **kwargs: Any) -> bytes:
    resp = requests.request(method, url, headers=headers, timeout=kwargs.pop("timeout", 60), **kwargs)
    if resp.status_code >= 400:
        raise RuntimeError(f"{method} {url} failed: HTTP {resp.status_code}\n{resp.text}")
    return resp.content


def _server_parent_path(path: str, filename: str, fallback: str) -> str:
    """Return the server-side parent path without applying local OS path rules."""
    raw_path = str(path or "")
    raw_name = str(filename or "")
    if raw_path and raw_name and raw_path.replace("\\", "/").endswith(f"/{raw_name}"):
        return raw_path[: -len(raw_name)].rstrip("/\\")
    if "/" in raw_path:
        return raw_path.rsplit("/", 1)[0]
    if "\\" in raw_path:
        return raw_path.rsplit("\\", 1)[0]
    return fallback


def main() -> int:
    _load_hermes_env()

    parser = argparse.ArgumentParser(description="Upload and download 123.txt containing hello through Business API.")
    parser.add_argument("--base-url", default=os.getenv("BUSINESS_API_BASE_URL", "http://127.0.0.1:8765"))
    parser.add_argument("--api-key", default=os.getenv("BUSINESS_API_KEY"))
    parser.add_argument("--target-path", default="", help="Workspace-relative upload directory. Empty means root.")
    parser.add_argument("--content", default="hello")
    parser.add_argument("--filename", default="123.txt")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    if not args.api_key:
        print("Missing BUSINESS_API_KEY. Set it in ~/.hermes/.env or pass --api-key.", file=sys.stderr)
        return 2

    base_url = args.base_url.rstrip("/")
    headers = {"Authorization": f"Bearer {args.api_key}"}

    try:
        health = _request_json("GET", f"{base_url}/health", headers=headers)
        print(f"Connected: {health}")

        data = {
            "overwrite": "true" if args.overwrite else "false",
        }
        if args.target_path:
            data["target_path"] = args.target_path

        files = {
            "file": (
                args.filename,
                BytesIO(args.content.encode("utf-8")),
                "text/plain",
            )
        }
        uploaded = _request_json("POST", f"{base_url}/api/files", headers=headers, data=data, files=files)

        uploaded_filename = str(uploaded.get("filename") or args.filename)
        download_path = _server_parent_path(str(uploaded.get("path") or ""), uploaded_filename, args.target_path)
        downloaded = _request_bytes(
            "GET",
            f"{base_url}/api/files",
            headers=headers,
            params={
                "path": download_path,
                "file_name": uploaded_filename,
            },
        )
    except Exception as exc:
        print(f"Smoke test failed: {exc}", file=sys.stderr)
        return 1

    expected = args.content.encode("utf-8")
    if downloaded != expected:
        print(
            f"Downloaded content mismatch: expected {expected!r}, got {downloaded!r}",
            file=sys.stderr,
        )
        return 1

    print("Uploaded:")
    print(f"  file_id: {uploaded.get('file_id')}")
    print(f"  filename: {uploaded.get('filename')}")
    print(f"  path: {uploaded.get('path')}")
    print(f"  size: {uploaded.get('size')}")
    print("Downloaded:")
    print(f"  bytes: {len(downloaded)}")
    print(f"  content: {downloaded.decode('utf-8', errors='replace')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
