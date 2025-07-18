"""Titiler middlewares."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Set
from urllib.parse import urlencode

from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from titiler.core import telemetry


@dataclass(frozen=True)
class CacheControlMiddleware:
    """MiddleWare to add CacheControl in response headers.

    Args:
        app (ASGIApp): starlette/FastAPI application.
        cachecontrol (str): Cache-Control string to add to the response.
        exclude_path (set): Set of regex expression to use to filter the path.

    """

    app: ASGIApp
    cachecontrol: str | None = None
    cachecontrol_max_http_code: int = 500
    exclude_path: Set[str] = field(default_factory=set)

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        """Handle call."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message: Message):
            """Send Message."""
            if message["type"] == "http.response.start":
                response_headers = MutableHeaders(scope=message)
                if self.cachecontrol and not response_headers.get("Cache-Control"):
                    if (
                        scope["method"] in ["HEAD", "GET"]
                        and message["status"] < self.cachecontrol_max_http_code
                        and not any(
                            re.match(path, scope["path"]) for path in self.exclude_path
                        )
                    ):
                        response_headers["Cache-Control"] = self.cachecontrol

            await send(message)

        await self.app(scope, receive, send_wrapper)


@dataclass(frozen=True)
class TotalTimeMiddleware:
    """MiddleWare to add Total process time in response headers.

    Args:
        app (ASGIApp): starlette/FastAPI application.

    """

    app: ASGIApp

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        """Handle call."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        start_time = time.time()

        async def send_wrapper(message: Message):
            """Send Message."""
            nonlocal start_time

            if message["type"] == "http.response.start":
                response_headers = MutableHeaders(scope=message)
                process_time = time.time() - start_time
                app_time = "total;dur={}".format(round(process_time * 1000, 2))

                timings = response_headers.get("Server-Timing")
                response_headers["Server-Timing"] = (
                    f"{timings}, {app_time}" if timings else app_time
                )

            await send(message)

        await self.app(scope, receive, send_wrapper)


@dataclass(frozen=True)
class LoggerMiddleware:
    """MiddleWare to add logging."""

    app: ASGIApp
    logger: logging.Logger = field(
        default_factory=lambda: logging.getLogger("titiler.requests")
    )

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        """Handle call."""
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        request = Request(scope, receive=receive)
        data = {
            "http.method": request.method,
            "http.url": str(request.url),
            "http.scheme": request.url.scheme,
            "http.host": request.headers.get("host", request.url.hostname or "unknown"),
            "http.target": request.url.path
            + (f"?{request.url.query}" if request.url.query else ""),
            "http.user_agent": request.headers.get("user-agent"),
            "http.referer": next(
                (request.headers.get(attr) for attr in ["referer", "referrer"]),
                None,
            ),
            "http.request.header.content-length": request.headers.get("content-length"),
            "http.request.header.accept-encoding": request.headers.get(
                "accept-encoding"
            ),
            "http.request.header.origin": request.headers.get("origin"),
            "net.host.name": request.url.hostname,
            "net.host.port": request.url.port,
            "titiler.query_params": dict(request.query_params),
        }

        telemetry.add_span_attributes(telemetry.flatten_dict(data))

        exception: Exception | None = None
        try:
            await self.app(scope, receive, send)
        except Exception as e:
            exception = e

        if route := scope.get("route"):
            data["http.route"] = route.path

        data["titiler.path_params"] = request.path_params

        self.logger.info(
            f"Request received: {request.url.path} {request.method}",
            extra=data,
        )
        if exception:
            raise exception


@dataclass(frozen=True)
class LowerCaseQueryStringMiddleware:
    """Middleware to make URL parameters case-insensitive.
    taken from: https://github.com/tiangolo/fastapi/issues/826

    """

    app: ASGIApp

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        """Handle call."""
        if scope["type"] == "http":
            request = Request(scope)
            DECODE_FORMAT = "latin-1"
            query_items = [
                (k.lower(), v) for k, v in request.query_params.multi_items()
            ]
            query_string = urlencode(query_items, doseq=True)
            request.scope["query_string"] = query_string.encode(DECODE_FORMAT)

        await self.app(scope, receive, send)
