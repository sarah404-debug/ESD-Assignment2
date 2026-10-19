"""gRPC server for the Quest App: QuestService.ResolveQuest.

This is the gRPC version of HTTP PATCH /quests/{instance_id}. It runs in its
own process/container, but uses the same business rules (quest_core/rules.py)
and the same database (quest_core/store.py) as the HTTP app. It does NOT call
the HTTP app.

Run from the project folder:
    python -m grpc_service.server
Ports: GRPC_PORT (default 50051) for gRPC, METRICS_PORT (default 9101) for
Prometheus (/metrics).
"""
import logging
import os
import signal
import time
from concurrent import futures

import grpc
from prometheus_client import Counter, Gauge, Histogram, start_http_server

from questpb import quest_pb2, quest_pb2_grpc
from quest_core import rules
from quest_core.jsonlog import get_logger, pick_request_id, request_id_var

GRPC_PORT = int(os.environ.get("GRPC_PORT", "50051"))
METRICS_PORT = int(os.environ.get("METRICS_PORT", "9101"))  # Prometheus scrapes /metrics here

logger = get_logger("quest-grpc")

# ── Metrics ─────────────────────────────────────────────────
# Recorded by RequestLoggingInterceptor for every RPC, measured at the SERVER
# (from when the call reaches the interceptor until the handler returns).
# Labels have small fixed value sets: the RPC method name and the gRPC status
# code name. Quest IDs and request IDs are never labels.

# COUNTER: calls finished, by method and status code (OK, NOT_FOUND, ...).
# Error rate = calls with grpc_code != "OK" divided by all calls.
grpc_server_handled_total = Counter(
    "grpc_server_handled_total",
    "gRPC calls finished on the server, by method and status code",
    ["grpc_method", "grpc_code"],
)

# HISTOGRAM: how long each call takes, in seconds. A ResolveQuest call does
# one SQLite read and write, so most take about 1-10 ms; the small buckets show
# that detail, and the larger ones (up to 2.5 s, above the client's 2 s
# deadline) leave room to see a slow server.
grpc_server_handling_seconds = Histogram(
    "grpc_server_handling_seconds",
    "Time the server takes to handle a gRPC call",
    ["grpc_method"],
    buckets=[0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5],
)

# GAUGE: calls being handled right now (goes up when a call starts, down when
# it ends). Shows concurrency, and a build-up of calls if the server slows down.
grpc_server_in_flight = Gauge(
    "grpc_server_in_flight",
    "gRPC calls currently being handled by the server",
    ["grpc_method"],
)

# Outcome enum (from quest.proto) -> status string used by the shared rules.
OUTCOME_TO_STATUS = {
    quest_pb2.OUTCOME_COMPLETED: "completed",
    quest_pb2.OUTCOME_SKIPPED: "skipped",
    quest_pb2.OUTCOME_ABANDONED: "abandoned",
}


def _to_message(quest):
    """Shared-rules quest (a dict) -> Quest protobuf message."""
    return quest_pb2.Quest(
        instance_id=quest["instance_id"],
        quest_id=quest["quest_id"],
        category=quest["category"],
        difficulty=quest["difficulty"],
        text=quest["text"],
        status=quest["status"],
        created_at=quest["created_at"],
    )


class QuestService(quest_pb2_grpc.QuestServiceServicer):
    """The server handler: fills in the method the generated base class leaves empty."""

    def ResolveQuest(self, request, context):
        status = OUTCOME_TO_STATUS.get(request.outcome)
        if status is None:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT,
                          "outcome must be OUTCOME_COMPLETED, OUTCOME_SKIPPED or OUTCOME_ABANDONED")

        # The same rules the HTTP app uses; their errors become gRPC status codes.
        try:
            quest, changed = rules.resolve_quest(request.instance_id, status)
        except rules.InvalidInput as error:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(error))
        except rules.QuestNotFound as error:
            logger.warning("quest not found", extra={
                "event": "quest_not_found",
                "instance_id": request.instance_id[:64],  # cap length: this value comes from the caller
            })
            context.abort(grpc.StatusCode.NOT_FOUND, str(error))
        except rules.QuestAlreadyResolved as error:
            logger.warning("quest already resolved", extra={
                "event": "quest_already_resolved",
                "instance_id": request.instance_id,
                "quest_status": error.current_status,
            })
            context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(error))

        logger.info(
            "quest resolved" if changed else "quest already has the requested status",
            extra={
                "event": "quest_resolved" if changed else "quest_resolve_repeated",
                "instance_id": quest["instance_id"],
                "category": quest["category"],
                "difficulty": quest["difficulty"],
                "quest_status": quest["status"],
            },
        )
        return quest_pb2.ResolveQuestResponse(quest=_to_message(quest), changed=changed)


class RequestLoggingInterceptor(grpc.ServerInterceptor):
    """Runs around every RPC, like the HTTP app's middleware.

    - Request ID: uses the caller's `x-request-id` metadata if it sent one
      (so one task keeps the same ID across the HTTP and gRPC services),
      otherwise makes a new one. It is put in request_id_var, so every log
      line written during the call carries it, and sent back to the caller.
    - Writes one "rpc completed" log line per call with method, status code
      and duration (like the HTTP app's "request completed").
    - Records the gRPC metrics (see "Metrics" above): calls by method and
      status code, duration, and calls in flight. Measured at the server.
    - Turns an unexpected exception into INTERNAL and logs its stack trace.
    """

    def intercept_service(self, continuation, handler_call_details):
        handler = continuation(handler_call_details)
        if handler is None or handler.unary_unary is None:
            return handler  # unknown method, or a streaming one (we have none)

        method = handler_call_details.method.rsplit("/", 1)[-1]  # e.g. "ResolveQuest"
        incoming_id = dict(handler_call_details.invocation_metadata).get("x-request-id")
        inner = handler.unary_unary

        def logged_behavior(request, context):
            request_id = pick_request_id(incoming_id)
            request_id_var.set(request_id)
            context.send_initial_metadata((("x-request-id", request_id),))
            start = time.perf_counter()
            code = grpc.StatusCode.OK
            grpc_server_in_flight.labels(grpc_method=method).inc()
            try:
                return inner(request, context)
            except Exception:
                # context.code() is set if the handler called context.abort(...).
                code = context.code() or grpc.StatusCode.INTERNAL
                if context.code() is None:  # not one of our own aborts: a real bug
                    logger.exception("unhandled exception", extra={"event": "unhandled_exception"})
                    context.abort(code, "Something went wrong on the server.")
                raise
            finally:
                duration = time.perf_counter() - start
                grpc_server_in_flight.labels(grpc_method=method).dec()
                grpc_server_handled_total.labels(grpc_method=method, grpc_code=code.name).inc()
                grpc_server_handling_seconds.labels(grpc_method=method).observe(duration)

                # INFO for OK, WARNING for client errors, ERROR for server errors (as in the HTTP app).
                if code == grpc.StatusCode.INTERNAL:
                    level = logging.ERROR
                elif code != grpc.StatusCode.OK:
                    level = logging.WARNING
                else:
                    level = logging.INFO
                logger.log(level, "rpc completed", extra={
                    "event": "rpc_completed",
                    "rpc_method": method,
                    "grpc_code": code.name,
                    "duration_ms": round(duration * 1000, 2),
                })

        return grpc.unary_unary_rpc_method_handler(
            logged_behavior,
            request_deserializer=handler.request_deserializer,
            response_serializer=handler.response_serializer,
        )


def serve():
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=10),
        interceptors=[RequestLoggingInterceptor()],
    )
    quest_pb2_grpc.add_QuestServiceServicer_to_server(QuestService(), server)
    server.add_insecure_port(f"[::]:{GRPC_PORT}")
    server.start()
    logger.info(f"gRPC server listening on port {GRPC_PORT}", extra={"event": "server_started"})

    # A small HTTP server (in a background thread) for Prometheus: /metrics.
    start_http_server(METRICS_PORT)
    logger.info(f"metrics on http://0.0.0.0:{METRICS_PORT}/metrics", extra={"event": "metrics_started"})

    # `docker compose stop` sends SIGTERM: finish in-flight calls (up to 5 s), then exit.
    signal.signal(signal.SIGTERM, lambda *_: server.stop(grace=5))
    server.wait_for_termination()


if __name__ == "__main__":
    serve()
