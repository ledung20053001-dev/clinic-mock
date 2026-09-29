import json
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse

from clinic_mock.auth import parse_bearer
from clinic_mock.config import settings
from clinic_mock.errors import ApiError, api_error_handler
from clinic_mock.logger import logger
from clinic_mock.routes import harness, health, v1
from clinic_mock.store import seed_default
from clinic_mock.tracing import instrument_app, setup_tracing


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_tracing()
    seed_default()
    logger.info("Clinic mock started on {}:{}", settings.app.HOST, settings.app.PORT)
    yield
    logger.info("Clinic mock shutting down.")


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app.PROJECT_NAME,
        version=settings.app.VERSION,
        debug=settings.app.DEBUG,
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        # 1. Assign a request id (echoed into the error envelope and trace attribute).
        rid = request.headers.get("X-Request-Id") or f"req_{uuid4().hex[:12]}"
        request.state.request_id = rid

        # Public docs endpoints skip auth.
        if request.url.path in {
            "/openapi.json",
            "/docs",
            "/docs/oauth2-redirect",
            "/redoc",
            "/health",
        }:
            response = await call_next(request)
            response.headers["X-Request-Id"] = rid
            return response

        # 2. Resolve bearer token → Principal for every non-public path.
        # All routes (including /_harness/*) require auth; each tenant can only
        # see and mutate its own data.
        token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if not token:
            return JSONResponse(
                status_code=401,
                content={
                    "error": {
                        "code": "BAD_KEY",
                        "message": "Missing bearer token.",
                        "request_id": rid,
                    }
                },
                headers={"WWW-Authenticate": 'Bearer realm="clinic-mock"'},
            )
        try:
            request.state.principal = parse_bearer(token)
        except ValueError:
            return JSONResponse(
                status_code=401,
                content={
                    "error": {
                        "code": "BAD_KEY",
                        "message": "Invalid bearer token.",
                        "request_id": rid,
                    }
                },
                headers={"WWW-Authenticate": 'Bearer realm="clinic-mock"'},
            )

        response = await call_next(request)
        response.headers["X-Request-Id"] = rid
        return response

    @app.middleware("http")
    async def writelog_middleware(request: Request, call_next):
        """Capture every /v1/* WRITE into db.writelog (contract §4.3 step 5).

        Reads (`GET /v1/*` — slot lists, patients, etc.) are captured by the
        routes themselves via `_record_writelog_read()`. The middleware keeps
        the read path stateless: capturing response body across the
        Starlette middleware boundary interferes with response delivery, so
        each read endpoint opts in by calling the helper after assembling the
        response dict.
        """
        from clinic_mock.store import db, derive_writelog_op, now_iso

        method = request.method
        path = request.url.path
        op = derive_writelog_op(method, path)
        capture = op is not None and method != "GET"

        body_bytes = b""
        if capture:
            body_bytes = await request.body()  # cached by Starlette

        response = await call_next(request)

        if capture:
            entry: dict = {
                "at": now_iso(),
                "op": op,
                "method": method,
                "path": path,
                "status": response.status_code,
            }
            try:
                entry["body"] = json.loads(body_bytes) if body_bytes else None
            except (json.JSONDecodeError, ValueError):
                entry["body"] = None
            if_match = request.headers.get("If-Match")
            if if_match:
                try:
                    entry["if_match"] = int(if_match)
                except ValueError:
                    entry["if_match"] = if_match
            idem = request.headers.get("Idempotency-Key")
            if idem:
                entry["idempotency_key"] = idem
            # /v1/appointments/<id>[/<action>] — capture the affected appointment id.
            parts = path.rstrip("/").strip("/").split("/")
            if (
                len(parts) >= 3
                and parts[0] == "v1"
                and parts[1] == "appointments"
                and parts[2]
                and parts[2] != "appointments"
            ):
                entry["appointment_id"] = parts[2]
            db.writelog.append(entry)

        return response

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError):
        return await api_error_handler(request, exc)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        rid = getattr(request.state, "request_id", None)
        details = [
            {"field": ".".join(str(p) for p in err["loc"]), "issue": err["msg"]}
            for err in exc.errors()
        ]
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "code": "INVALID_REQUEST",
                    "message": "Request validation failed.",
                    "request_id": rid,
                    "details": details,
                }
            },
        )

    app.include_router(v1)
    app.include_router(harness)
    app.include_router(health)

    # Expose Bearer auth in Swagger UI so the "Authorize" button appears.
    # Auth itself runs in middleware above — this is purely a docs hint.
    def _custom_openapi():
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            routes=app.routes,
        )
        schema.setdefault("components", {})["securitySchemes"] = {
            "BearerAuth": {"type": "http", "scheme": "bearer"}
        }
        # Global security so Swagger's Authorize button actually attaches the
        # header to every "Try it out" call. Middleware still enforces auth on
        # non-public paths regardless of this hint.
        schema["security"] = [{"BearerAuth": []}]
        app.openapi_schema = schema
        return app.openapi_schema

    app.openapi = _custom_openapi

    instrument_app(app)
    return app


# Module-level app for `uvicorn clinic_mock.app:app`
app = create_app()
