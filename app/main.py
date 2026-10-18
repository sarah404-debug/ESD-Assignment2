import contextvars
import json
import logging
import os
import random
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.staticfiles import StaticFiles
from prometheus_client import Counter, Gauge, Histogram, Summary, generate_latest, CONTENT_TYPE_LATEST

app = FastAPI()

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

# ── Fault injection switch (Part E1) ───────────────────────
# Off by default. To turn on, set these two environment variables (see
# docker-compose.yml / .env). FAULT_EVERY_N=0 means "never trigger".
# Every Nth call to GET /quest/new sleeps for FAULT_DELAY_MS milliseconds,
# simulating a slow dependency. This is easy to switch off again afterwards.
FAULT_DELAY_MS = int(os.environ.get("FAULT_DELAY_MS", "0"))
FAULT_EVERY_N = int(os.environ.get("FAULT_EVERY_N", "0"))
_fault_request_count = 0  # counts calls to /quest/new, to know when "every Nth" is due

# ── Quest pool ──────────────────────────────────────────────
QUESTS = [
    {"id": 1, "category": "social", "difficulty": "easy", "text": "Call someone you haven't spoken to in a while."},
    {"id": 2, "category": "reflective", "difficulty": "easy", "text": "Write down 3 things you're avoiding right now."},
    {"id": 3, "category": "physical", "difficulty": "medium", "text": "Take a 20-minute walk with no phone."},
    {"id": 4, "category": "creative", "difficulty": "easy", "text": "Doodle something for 5 minutes, no judging it."},
    {"id": 5, "category": "social", "difficulty": "hard", "text": "Tell someone something you appreciate about them, out loud."},
]

quest_instances = {}

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
        response.headers["X-Request-ID"] = request_id
        return response
    except Exception:
        logger.exception("unhandled exception", extra={"event": "unhandled_exception"})
        raise
    finally:
        # Use the route template (e.g. /quest/{instance_id}/complete), NOT the real URL,
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

# ── Endpoints ───────────────────────────────────────────────
@app.get("/")
def read_root():
    return {"message": "Hello, quester!"}

@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

@app.get("/quest/new")
def get_new_quest():
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
    instance_id = str(uuid.uuid4())

    quest_instances[instance_id] = {
        "instance_id": instance_id,
        "quest_id": quest["id"],
        "category": quest["category"],
        "difficulty": quest["difficulty"],
        "text": quest["text"],
        "status": "in_progress",
        "created_at": time.time(),  # remembered so we can measure time-to-resolve later
    }

    # Update metrics
    quests_generated_total.labels(category=quest["category"]).inc()
    quests_in_progress.labels(category=quest["category"]).inc()

    duration = time.time() - start_time
    quest_generation_latency_seconds.observe(duration)

    logger.info("quest generated", extra={
        "event": "quest_generated",
        "instance_id": instance_id,
        "category": quest["category"],
        "difficulty": quest["difficulty"],
    })

    return quest_instances[instance_id]

def _resolve_quest(instance_id: str, new_status: str):
    if instance_id not in quest_instances:
        logger.warning("quest not found", extra={
            "event": "quest_not_found",
            "instance_id": instance_id[:64],  # cap length: this value comes from the URL
        })
        raise HTTPException(status_code=404, detail="Quest instance not found")

    instance = quest_instances[instance_id]

    if instance["status"] != "in_progress":
        logger.warning("quest already resolved", extra={
            "event": "quest_already_resolved",
            "instance_id": instance_id,
            "quest_status": instance["status"],
        })
        raise HTTPException(status_code=400, detail=f"Quest is already '{instance['status']}'")

    instance["status"] = new_status
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
        "instance_id": instance_id,
        "category": category,
        "difficulty": difficulty,
        "quest_status": new_status,
    })

    return instance

@app.post("/quest/{instance_id}/complete")
def complete_quest(instance_id: str):
    return _resolve_quest(instance_id, "completed")

@app.post("/quest/{instance_id}/skip")
def skip_quest(instance_id: str):
    return _resolve_quest(instance_id, "skipped")

@app.post("/quest/{instance_id}/abandon")
def abandon_quest(instance_id: str):
    return _resolve_quest(instance_id, "abandoned")

@app.get("/quest/status")
def quest_status():
    return list(quest_instances.values())