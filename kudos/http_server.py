from __future__ import annotations

import json
import mimetypes
import os
import logging
import re
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .service import (
    AuthenticationError,
    KudosError,
    KudosService,
    RateLimitError,
)

logger = logging.getLogger(__name__)


class KudosHandler(BaseHTTPRequestHandler):
    service: KudosService
    tokens: dict[str, str]
    web_root: Path

    def log_message(self, format_string: str, *args) -> None:
        logger.info(
            "HTTP %s %s %s",
            self.client_address[0],
            self.command,
            urlparse(self.path).path,
        )

    def send_json(self, status: int, payload: dict, headers: dict | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (headers or {}).items():
            self.send_header(key, str(value))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("Invalid Content-Length.") from None
        if length <= 0 or length > 16_384:
            raise ValueError("Request body must be between 1 and 16384 bytes.")
        try:
            payload = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ValueError("Request body must contain valid JSON.") from None
        if not isinstance(payload, dict):
            raise ValueError("Request body must be a JSON object.")
        return payload

    def read_optional_json(self) -> dict:
        content_length = self.headers.get("Content-Length")
        if content_length is None or content_length == "0":
            return {}
        return self.read_json()

    def actor_id(self) -> str:
        authorization = self.headers.get("Authorization", "")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise AuthenticationError("Bearer authentication is required.")
        actor_id = self.tokens.get(token)
        if actor_id is None:
            raise AuthenticationError("Invalid bearer token.")
        self.service.employee(actor_id)
        return actor_id

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.serve_index()
            return
        try:
            actor_id = self.actor_id()
            query = parse_qs(parsed.query)
            if parsed.path == "/api/employees":
                employees = self.service.list_employees(
                    actor_id, query.get("query", [""])[0]
                )
                self.send_json(200, {"items": employees})
            elif parsed.path == "/api/kudos":
                limit_text = query.get("limit", ["20"])[0]
                try:
                    limit = int(limit_text)
                except ValueError:
                    self.send_json(422, {"error": "Limit must be an integer."})
                    return
                result = self.service.feed(
                    actor_id, limit, query.get("cursor", [None])[0]
                )
                self.send_json(200, result)
            elif parsed.path == "/api/moderation/kudos":
                items = self.service.moderation_queue(
                    actor_id, int(query.get("limit", ["50"])[0])
                )
                self.send_json(200, {"items": items})
            else:
                self.send_json(404, {"error": "Not found."})
        except KudosError as error:
            self.handle_error(error)
        except ValueError as error:
            self.send_json(422, {"error": str(error)})
        except Exception:
            self.handle_unexpected_error()

    def do_POST(self) -> None:
        try:
            actor_id = self.actor_id()
            parsed = urlparse(self.path)
            if parsed.path == "/api/kudos":
                payload = self.read_json()
                result = self.service.create_kudos(
                    actor_id,
                    payload.get("recipient_id"),
                    payload.get("message"),
                )
                self.send_json(201, result)
                return

            match = re.fullmatch(r"/api/kudos/([0-9a-fA-F-]+)/(?P<action>hide|restore)", parsed.path)
            if match:
                payload = self.read_optional_json()
                result = self.service.moderate(
                    actor_id,
                    match.group(1),
                    match.group("action"),
                    payload.get("reason"),
                )
                self.send_json(200, result)
                return
            self.send_json(404, {"error": "Not found."})
        except ValueError as error:
            self.send_json(400, {"error": str(error)})
        except KudosError as error:
            self.handle_error(error)
        except Exception:
            self.handle_unexpected_error()

    def do_DELETE(self) -> None:
        try:
            actor_id = self.actor_id()
            match = re.fullmatch(r"/api/kudos/([0-9a-fA-F-]+)", urlparse(self.path).path)
            if not match:
                self.send_json(404, {"error": "Not found."})
                return
            payload = self.read_optional_json()
            result = self.service.moderate(
                actor_id, match.group(1), "delete", payload.get("reason")
            )
            self.send_json(200, result)
        except ValueError as error:
            self.send_json(400, {"error": str(error)})
        except KudosError as error:
            self.handle_error(error)
        except Exception:
            self.handle_unexpected_error()

    def handle_error(self, error: KudosError) -> None:
        headers = {}
        if isinstance(error, RateLimitError):
            headers["Retry-After"] = error.retry_after
        self.send_json(error.status_code, {"error": str(error)}, headers)

    def handle_unexpected_error(self) -> None:
        correlation_id = str(uuid.uuid4())
        logger.exception("Unexpected request failure (correlation_id=%s)", correlation_id)
        self.send_json(
            500,
            {
                "error": "An unexpected server error occurred.",
                "correlation_id": correlation_id,
            },
        )

    def serve_index(self) -> None:
        index = self.web_root / "index.html"
        body = index.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(str(index))[0] or "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def log_error(self, format_string: str, *args) -> None:
        super().log_error(format_string, *args)


def create_server(
    service: KudosService,
    tokens: dict[str, str],
    host: str = "127.0.0.1",
    port: int = 8000,
) -> ThreadingHTTPServer:
    handler = type(
        "ConfiguredKudosHandler",
        (KudosHandler,),
        {
            "service": service,
            "tokens": tokens,
            "web_root": Path(__file__).resolve().parent.parent / "web",
        },
    )
    return ThreadingHTTPServer((host, port), handler)


def tokens_from_environment() -> dict[str, str]:
    raw = os.environ.get("KUDOS_TOKENS_JSON", "")
    if not raw:
        raise RuntimeError("Set KUDOS_TOKENS_JSON before starting the demo server.")
    try:
        tokens = json.loads(raw)
    except json.JSONDecodeError as error:
        raise RuntimeError("KUDOS_TOKENS_JSON must be a valid JSON object.") from error
    if (
        not isinstance(tokens, dict)
        or not tokens
        or any(
            not isinstance(token, str)
            or not token
            or not isinstance(employee_id, str)
            or not employee_id
            for token, employee_id in tokens.items()
        )
    ):
        raise RuntimeError(
            "KUDOS_TOKENS_JSON must map non-empty bearer tokens to employee IDs."
        )
    return tokens
