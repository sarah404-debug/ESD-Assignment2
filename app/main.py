import contextvars
import json
import logging
import math
import os
import random
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal
from fastapi import Depends, FastAPI, Header, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException
from prometheus_client import Counter, Gauge, Histogram, Summary, generate_latest, CONTENT_TYPE_LATEST

from quest_core import store

# title/version/description appear at the top of the OpenAPI spec (/openapi.json)
# and the interactive docs page (/docs).
app = FastAPI(
    title="Quest App API",
    version="2.0.0",
    description="Get small real-life side quests and record whether you completed, skipped or abandoned them.",
)

# Serve the small one-page UI (app/static/index.html) at /ui.
app.mount("/ui", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="ui")

# ── Logging ─────────────────────────────────────────────────
# Every log line is one JSON object written to stdout (Docker captures stdout).
# request_id lives in a context variable so any log line written while handling
# a request automatically carries that request's ID.
request_id_var = contextvars.ContextVar("request_id", default="-")

# Extra fields we allow on a log line (passed via logger.info(..., extra={...})).
LOG_FIELDS = (
    "event", "method", "path", "endpoint", "status", "duration_ms",
    "instance_id", "category", "difficulty", "quest_status",
)

class JsonFormatter(logging.Formatter):
    def format(self, record):
        entry = {
            "time": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "service": "quest-app",
            "severity": record.levelname,
            "message": record.getMessage(),
            "request_id": request_id_var.get(),
        }
        for field in LOG_FIELDS:
            if hasattr(record, field):
                entry[field] = getattr(record, field)
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry)

logger = logging.getLogger("quest-app")
logger.setLevel(logging.INFO)
logger.propagate = False
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(JsonFormatter())
logger.addHandler(_handler)

# ── Errors ──────────────────────────────────────────────────
# Every error the API returns has the same JSON shape:
#   {"error": "<short_code>", "message": "<text for humans>", "request_id": "<id>"}
# "error" is a fixed code a program can check; "request_id" matches the log line
# for this request in Kibana.
class ApiError(Exception):
    """Raised by our own code for an expected problem, e.g. an unknown quest.
    `headers` are extra response headers, e.g. Retry-After on a 429."""
    def __init__(self, status_code: int, error: str, message: str, headers=None):
        self.status_code = status_code
        self.error = error
        self.message = message
        self.headers = headers

def error_response(status_code: int, error: str, message: str, details=None, headers=None):
    body = {"error": error, "message": message, "request_id": request_id_var.get()}
    if details:
        body["details"] = details
    return JSONResponse(status_code=status_code, content=body, headers=headers)

@app.exception_handler(ApiError)
async def handle_api_error(request: Request, exc: ApiError):
    return error_response(exc.status_code, exc.error, exc.message, headers=exc.headers)

# Errors FastAPI raises by itself, e.g. an unknown path (404) or the wrong method (405).
HTTP_ERROR_CODES = {404: "not_found", 405: "method_not_allowed"}

@app.exception_handler(StarletteHTTPException)
async def handle_http_error(request: Request, exc: StarletteHTTPException):
    error = HTTP_ERROR_CODES.get(exc.status_code, "http_error")
    return error_response(exc.status_code, error, str(exc.detail))

# Input that fails validation (wrong type, missing field, value not allowed).
@app.exception_handler(RequestValidationError)
async def handle_validation_error(request: Request, exc: RequestValidationError):
    details = [
        {"field": ".".join(str(part) for part in e["loc"]), "problem": e["msg"]}
        for e in exc.errors()
    ]
    return error_response(422, "validation_error", "The request has invalid input.", details)

# ── API models ──────────────────────────────────────────────
# These describe the JSON that goes in and out of the API. FastAPI uses them
# to validate input and to build the OpenAPI spec, so the spec always matches the code.

# The only values a quest's status can have. FastAPI rejects anything else with 422.
QuestStatus = Literal["in_progress", "completed", "skipped", "abandoned"]

class Quest(BaseModel):
    instance_id: uuid.UUID
    quest_id: int
    category: str
    difficulty: str
    text: str
    status: QuestStatus
    created_at: float  # Unix timestamp (seconds)

# Body of PATCH /quests/{id}: the status to move the quest to.
# "in_progress" is not allowed here (a quest can't be un-finished); anything
# other than these three values is rejected with 422.
class QuestUpdate(BaseModel):
    status: Literal["completed", "skipped", "abandoned"]

class ErrorDetail(BaseModel):
    field: str
    problem: str

# The shape every error has (see "Errors" above).
class ErrorResponse(BaseModel):
    error: str
    message: str
    request_id: str
    details: list[ErrorDetail] | None = None

# Error responses listed in the spec, so readers see the error codes each endpoint can return.
NOT_FOUND = {404: {"model": ErrorResponse, "description": "No quest with this ID"}}
CONFLICT = {409: {"model": ErrorResponse, "description": "Quest is already resolved with a different status"}}
INVALID = {422: {"model": ErrorResponse, "description": "Invalid input (bad ID, body or query value)"}}
RATE_LIMITED = {429: {
    "model": ErrorResponse,
    "description": "Too many requests from this client; wait and try again",
    "headers": {"Retry-After": {
        "description": "Seconds to wait before the next request will be accepted",
        "schema": {"type": "integer"},
    }},
}}

# ── Rate limiting ───────────────────────────────────────────
# POST /quests allows at most RATE_LIMIT_REQUESTS calls per client IP address
# in each RATE_LIMIT_WINDOW_SECONDS window (a "fixed window": the window starts
# at the client's first call and the count resets when it ends). Extra calls get
# 429 Too Many Requests with a Retry-After header saying when to come back.
# Counts live in this process's memory, so this works for a single instance.
RATE_LIMIT_REQUESTS = int(os.environ.get("RATE_LIMIT_REQUESTS", "30"))
RATE_LIMIT_WINDOW_SECONDS = int(os.environ.get("RATE_LIMIT_WINDOW_SECONDS", "10"))

_rate_windows = {}  # client IP -> [window start time, calls accepted in this window]
_rate_lock = threading.Lock()

def rate_limit_create(request: Request, response: Response):
    client = request.client.host if request.client else "unknown"
    now = time.time()
    with _rate_lock:
        window = _rate_windows.get(client)
        if window is None or now - window[0] >= RATE_LIMIT_WINDOW_SECONDS:
            window = [now, 0]  # first call, or the old window ended: start a new one
            _rate_windows[client] = window

        if window[1] >= RATE_LIMIT_REQUESTS:
            retry_after = max(1, math.ceil(window[0] + RATE_LIMIT_WINDOW_SECONDS - now))
            logger.warning("rate limit exceeded", extra={"event": "rate_limited", "endpoint": "/quests"})
            raise ApiError(
                429, "rate_limited",
                f"Too many requests: limit is {RATE_LIMIT_REQUESTS} per {RATE_LIMIT_WINDOW_SECONDS}s. "
                f"Try again in {retry_after}s.",
                headers={"Retry-After": str(retry_after)},
            )

        window[1] += 1
        remaining = RATE_LIMIT_REQUESTS - window[1]

    # Let accepted callers see how many calls they have left in this window.
    response.headers["RateLimit-Limit"] = str(RATE_LIMIT_REQUESTS)
    response.headers["RateLimit-Remaining"] = str(remaining)

# ── Fault injection switch (Part E1) ───────────────────────
# Off by default. To turn on, set these two environment variables (see
# docker-compose.yml / .env). FAULT_EVERY_N=0 means "never trigger".
# Every Nth call to POST /quests sleeps for FAULT_DELAY_MS milliseconds,
# simulating a slow dependency. This is easy to switch off again afterwards.
FAULT_DELAY_MS = int(os.environ.get("FAULT_DELAY_MS", "0"))
FAULT_EVERY_N = int(os.environ.get("FAULT_EVERY_N", "0"))
_fault_request_count = 0  # counts calls to POST /quests, to know when "every Nth" is due

# ── Quest pool ──────────────────────────────────────────────
QUESTS = [
    {"id": 1, "category": "social", "difficulty": "easy", "text": "Call someone you haven't spoken to in a while."},
    {"id": 2, "category": "reflective", "difficulty": "easy", "text": "Write down 3 things you're avoiding right now."},
    {"id": 3, "category": "physical", "difficulty": "medium", "text": "Take a 20-minute walk with no phone."},
    {"id": 4, "category": "creative", "difficulty": "easy", "text": "Doodle something for 5 minutes, no judging it."},
    {"id": 5, "category": "social", "difficulty": "hard", "text": "Tell someone something you appreciate about them, out loud."},
]

# Quests (and Idempotency-Keys) are stored in the shared SQLite database in
# quest_core/store.py, the single source of truth for both services.

# ── Metrics ─────────────────────────────────────────────────
# COUNTER: something that only ever goes up.
quests_generated_total = Counter(
    "quests_generated_total",
    "Total number of quests generated",
    ["category"]
)

quests_completed_total = Counter(
    "quests_completed_total",
    "Total number of quests completed",
    ["category", "difficulty"]
)

quests_skipped_total = Counter(
    "quests_skipped_total",
    "Total number of quests skipped",
    ["category"]
)

quests_abandoned_total = Counter(
    "quests_abandoned_total",
    "Total number of quests abandoned",
    ["category"]
)

# GAUGE: something that goes up AND down.
quests_in_progress = Gauge(
    "quests_in_progress",
    "Number of quests currently in progress",
    ["category"]
)

# HISTOGRAM: measures how long something takes, sorted into buckets.
# Custom buckets: quest generation takes microseconds, so the buckets start very small.
# The larger buckets (0.1, 0.5, 1) leave room to see a slow request later.
quest_generation_latency_seconds = Histogram(
    "quest_generation_latency_seconds",
    "Time taken to generate a new quest",
    buckets=[0.00001, 0.00005, 0.0001, 0.0005, 0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1]
)

# SUMMARY: tracks a running count and sum of observations (so we can compute an average).
# Here: how many seconds pass between a quest being generated and being resolved.
quest_time_to_resolve_seconds = Summary(
    "quest_time_to_resolve_seconds",
    "Seconds between a quest being generated and being resolved",
    ["status"]
)

# APPLICATION METRICS: every HTTP request, recorded by the middleware below.
# COUNTER: how many requests, split by method, endpoint and status code (so failures are visible).
http_requests_total = Counter(
    "http_requests_total",
    "Total HTTP requests handled by the app",
    ["method", "endpoint", "status"]
)

# HISTOGRAM: how long each request takes end to end.
http_request_duration_seconds = Histogram(
    "http_request_duration_seconds",
    "Time taken to handle an HTTP request",
    ["method", "endpoint"],
    buckets=[0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5]
)

# ── Middleware ──────────────────────────────────────────────
@app.middleware("http")
async def track_requests(request: Request, call_next):
    # Don't count or log Prometheus scraping /metrics itself.
    if request.url.path == "/metrics":
        return await call_next(request)

    # Give every request its own ID; all log lines for this request will carry it.
    request_id = uuid.uuid4().hex[:12]
    request_id_var.set(request_id)

    start = time.time()
    status_code = 500  # assume failure unless the request finishes normally

    try:
        response = await call_next(request)
        status_code = response.status_code
    except Exception:
        # A bug in our code: log it with the stack trace, but still answer
        # in the same error format as every other error.
        logger.exception("unhandled exception", extra={"event": "unhandled_exception"})
        response = error_response(500, "internal_error", "Something went wrong on the server.")
    finally:
        # Use the route template (e.g. /quests/{instance_id}), NOT the real URL,
        # so we don't create a new time series for every instance_id.
        route = request.scope.get("route")
        endpoint = route.path if route else "unmatched"
        duration = time.time() - start

        http_requests_total.labels(
            method=request.method, endpoint=endpoint, status=str(status_code)
        ).inc()
        http_request_duration_seconds.labels(
            method=request.method, endpoint=endpoint
        ).observe(duration)

        # INFO for success, WARNING for client errors (4xx), ERROR for server errors (5xx).
        if status_code >= 500:
            level = logging.ERROR
        elif status_code >= 400:
            level = logging.WARNING
        else:
            level = logging.INFO
        logger.log(level, "request completed", extra={
            "event": "request_completed",
            "method": request.method,
            "path": request.url.path[:100],
            "endpoint": endpoint,
            "status": status_code,
            "duration_ms": round(duration * 1000, 2),
        })

    response.headers["X-Request-ID"] = request_id
    return response

# ── Endpoints ───────────────────────────────────────────────
@app.get("/", summary="Health check")
def read_root():
    return {"message": "Hello, quester!"}

# Left out of the OpenAPI spec: it is for Prometheus, not for API callers.
@app.get("/metrics", include_in_schema=False)
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

# Creating a quest changes data, so it is a POST (not a GET) on the /quests
# collection. It answers 201 Created and a Location header with the new quest's address.
#
# Idempotency: POST is not safe to repeat (each call makes a new quest), so a
# caller that may retry sends an Idempotency-Key header (any unique value, e.g. a
# UUID). The first request with a key creates the quest; any repeat with the same
# key gets that same quest back instead of a second one.
#
# Rate limited: see rate_limit_create above. The check runs before this function.
@app.post(
    "/quests",
    status_code=201,
    response_model=Quest,
    summary="Create a new quest",
    dependencies=[Depends(rate_limit_create)],
    responses={
        201: {
            "description": "Quest created (or, for a repeated Idempotency-Key, the quest that key already created)",
            "headers": {
                "Location": {
                    "description": "Address of the quest, e.g. /quests/{instance_id}",
                    "schema": {"type": "string"},
                },
                "Idempotent-Replayed": {
                    "description": "\"true\" when this is a repeat of an earlier request with the same Idempotency-Key",
                    "schema": {"type": "string"},
                },
                "RateLimit-Limit": {
                    "description": "Calls allowed per window for this client",
                    "schema": {"type": "integer"},
                },
                "RateLimit-Remaining": {
                    "description": "Calls this client has left in the current window",
                    "schema": {"type": "integer"},
                },
            },
        },
        **INVALID,
        **RATE_LIMITED,
    },
)
def create_quest(
    response: Response,
    idempotency_key: Annotated[str | None, Header(
        min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$",
        description="Optional unique value; repeating a request with the same key will not create a second quest",
    )] = None,
):
    global _fault_request_count

    # Fault injection (Part E1): if enabled, delay every Nth call to this
    # endpoint. Placed before start_time so it inflates the overall HTTP
    # response time (seen by the middleware and Grafana), while the
    # quest_generation_latency_seconds histogram below still measures only
    # the app's own work, unaffected by the injected delay.
    if FAULT_EVERY_N > 0:
        _fault_request_count += 1
        if _fault_request_count % FAULT_EVERY_N == 0:
            time.sleep(FAULT_DELAY_MS / 1000)

    start_time = time.time()  # mark when we started, so we can measure duration

    quest = random.choice(QUESTS)
    new_instance = {
        "instance_id": str(uuid.uuid4()),
        "quest_id": quest["id"],
        "category": quest["category"],
        "difficulty": quest["difficulty"],
        "text": quest["text"],
        "status": "in_progress",
        "created_at": time.time(),  # remembered so we can measure time-to-resolve later
    }

    # Saves the quest, unless this Idempotency-Key was used before: then nothing
    # is saved and we get back the quest that key already created.
    instance, created = store.insert_quest(new_instance, idempotency_key)
    response.headers["Location"] = f"/quests/{instance['instance_id']}"

    if not created:
        logger.info("repeated create request, returning existing quest", extra={
            "event": "quest_create_replayed",
            "instance_id": instance["instance_id"],
        })
        response.headers["Idempotent-Replayed"] = "true"
        return instance

    # Update metrics
    quests_generated_total.labels(category=quest["category"]).inc()
    quests_in_progress.labels(category=quest["category"]).inc()

    duration = time.time() - start_time
    quest_generation_latency_seconds.observe(duration)

    logger.info("quest generated", extra={
        "event": "quest_generated",
        "instance_id": instance["instance_id"],
        "category": quest["category"],
        "difficulty": quest["difficulty"],
    })

    return instance

# List all quests. Optional filter, e.g. GET /quests?status=in_progress
@app.get("/quests", response_model=list[Quest], summary="List quests", responses=INVALID)
def list_quests(status: QuestStatus | None = None):
    return store.list_quests(status)

# Look at one quest. instance_id must be a valid UUID; FastAPI rejects
# anything else (e.g. "abc") with 422 before this function runs.
@app.get(
    "/quests/{instance_id}",
    response_model=Quest,
    summary="Get one quest",
    responses={**NOT_FOUND, **INVALID},
)
def get_quest(instance_id: uuid.UUID):
    instance = store.get_quest(str(instance_id))
    if instance is None:
        raise ApiError(404, "quest_not_found", "Quest instance not found")
    return instance

def _resolve_quest(instance_id: str, new_status: str):
    instance = store.get_quest(instance_id)
    if instance is None:
        logger.warning("quest not found", extra={
            "event": "quest_not_found",
            "instance_id": instance_id[:64],  # cap length: this value comes from the URL
        })
        raise ApiError(404, "quest_not_found", "Quest instance not found")

    # Still in progress: change the status in the database, but only if it is
    # still in progress at that moment. If another request resolved it in the
    # meantime, re-read it and let the checks below decide.
    if instance["status"] == "in_progress":
        if store.change_status(instance_id, "in_progress", new_status):
            instance["status"] = new_status
            _record_resolved(instance)
            return instance
        instance = store.get_quest(instance_id)

    # Idempotency: asking for the status the quest already has (e.g. a retry of a
    # request that already worked) changes nothing and counts nothing twice.
    if instance["status"] == new_status:
        logger.info("quest already has the requested status", extra={
            "event": "quest_resolve_repeated",
            "instance_id": instance_id,
            "quest_status": new_status,
        })
        return instance

    # A *different* final status is a real conflict, e.g. skipping a completed quest.
    logger.warning("quest already resolved", extra={
        "event": "quest_already_resolved",
        "instance_id": instance_id,
        "quest_status": instance["status"],
    })
    # 409 Conflict: the request itself is fine, but it clashes with the quest's current state.
    raise ApiError(409, "quest_already_resolved", f"Quest is already '{instance['status']}'")

def _record_resolved(instance):
    """Metrics and log line for a quest that has just been resolved."""
    new_status = instance["status"]
    category = instance["category"]
    difficulty = instance["difficulty"]

    # Record how long this quest took to resolve (Summary).
    quest_time_to_resolve_seconds.labels(status=new_status).observe(
        time.time() - instance["created_at"]
    )

    # This quest is no longer "in progress", so the gauge goes DOWN.
    quests_in_progress.labels(category=category).dec()

    # And the right counter goes UP.
    if new_status == "completed":
        quests_completed_total.labels(category=category, difficulty=difficulty).inc()
    elif new_status == "skipped":
        quests_skipped_total.labels(category=category).inc()
    elif new_status == "abandoned":
        quests_abandoned_total.labels(category=category).inc()

    logger.info("quest resolved", extra={
        "event": "quest_resolved",
        "instance_id": instance["instance_id"],
        "category": category,
        "difficulty": difficulty,
        "quest_status": new_status,
    })

# Finish a quest: one endpoint on the quest itself replaces the old
# /complete, /skip and /abandon endpoints.
@app.patch(
    "/quests/{instance_id}",
    response_model=Quest,
    summary="Resolve a quest (complete, skip or abandon)",
    responses={**NOT_FOUND, **CONFLICT, **INVALID},
)
def update_quest(instance_id: uuid.UUID, update: QuestUpdate):
    return _resolve_quest(str(instance_id), update.status)