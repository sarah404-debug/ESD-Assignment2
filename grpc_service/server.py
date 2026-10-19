"""gRPC server for the Quest App: QuestService.ResolveQuest.

This is the gRPC version of HTTP PATCH /quests/{instance_id}. It runs in its
own process/container, but uses the same business rules (quest_core/rules.py)
and the same database (quest_core/store.py) as the HTTP app. It does NOT call
the HTTP app.

Run from the project folder:
    python -m grpc_service.server
Port: GRPC_PORT (default 50051).
"""
import logging
import os
import signal
import time
import uuid
from concurrent import futures

import grpc

from questpb import quest_pb2, quest_pb2_grpc
from quest_core import rules
from quest_core.jsonlog import get_logger, request_id_var

GRPC_PORT = int(os.environ.get("GRPC_PORT", "50051"))

logger = get_logger("quest-grpc")

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
        request_id_var.set(uuid.uuid4().hex[:12])
        start = time.time()
        code = grpc.StatusCode.OK
        try:
            status = OUTCOME_TO_STATUS.get(request.outcome)
            if status is None:
                code = grpc.StatusCode.INVALID_ARGUMENT
                context.abort(code, "outcome must be OUTCOME_COMPLETED, OUTCOME_SKIPPED or OUTCOME_ABANDONED")

            # The same rules the HTTP app uses; their errors become gRPC status codes.
            try:
                quest, changed = rules.resolve_quest(request.instance_id, status)
            except rules.InvalidInput as error:
                code = grpc.StatusCode.INVALID_ARGUMENT
                context.abort(code, str(error))
            except rules.QuestNotFound as error:
                code = grpc.StatusCode.NOT_FOUND
                context.abort(code, str(error))
            except rules.QuestAlreadyResolved as error:
                code = grpc.StatusCode.FAILED_PRECONDITION
                context.abort(code, str(error))

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
        except Exception:
            if code == grpc.StatusCode.OK:  # not one of our own aborts: a real bug
                code = grpc.StatusCode.INTERNAL
                logger.exception("unhandled exception", extra={"event": "unhandled_exception"})
                context.abort(code, "Something went wrong on the server.")
            raise
        finally:
            # One line per call, like the HTTP app's "request completed".
            # INFO for OK, WARNING for client errors, ERROR for server errors (as in the HTTP app).
            if code == grpc.StatusCode.INTERNAL:
                level = logging.ERROR
            elif code != grpc.StatusCode.OK:
                level = logging.WARNING
            else:
                level = logging.INFO
            logger.log(level, "rpc completed", extra={
                "event": "rpc_completed",
                "rpc_method": "ResolveQuest",
                "grpc_code": code.name,
                "duration_ms": round((time.time() - start) * 1000, 2),
            })


def serve():
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    quest_pb2_grpc.add_QuestServiceServicer_to_server(QuestService(), server)
    server.add_insecure_port(f"[::]:{GRPC_PORT}")
    server.start()
    logger.info(f"gRPC server listening on port {GRPC_PORT}", extra={"event": "server_started"})

    # `docker compose stop` sends SIGTERM: finish in-flight calls (up to 5 s), then exit.
    signal.signal(signal.SIGTERM, lambda *_: server.stop(grace=5))
    server.wait_for_termination()


if __name__ == "__main__":
    serve()
