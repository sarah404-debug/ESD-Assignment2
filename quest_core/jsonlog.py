"""JSON logging shared by the HTTP and gRPC services.

Every log line is one JSON object written to stdout (Docker captures stdout,
Filebeat ships it to Elasticsearch). Both services use the same fields, so
their logs can be searched together in Kibana; "service" tells them apart.

request_id lives in a context variable, so any log line written while
handling a request automatically carries that request's ID.
"""
import contextvars
import json
import logging
import re
import sys
import uuid
from datetime import datetime, timezone

request_id_var = contextvars.ContextVar("request_id", default="-")

# A caller may send its own request ID (HTTP header X-Request-ID, gRPC metadata
# x-request-id), so one ID follows a task across both services. Only short,
# plain IDs are accepted, so a caller can't inject odd text into the logs.
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def pick_request_id(incoming):
    """Use the caller's request ID if it is valid; otherwise make a new one."""
    if incoming and _REQUEST_ID_PATTERN.match(incoming):
        return incoming
    return uuid.uuid4().hex[:12]

# Extra fields we allow on a log line (passed via logger.info(..., extra={...})).
LOG_FIELDS = (
    "event", "method", "path", "endpoint", "status", "duration_ms",
    "instance_id", "category", "difficulty", "quest_status",
    "rpc_method", "grpc_code",
)


class JsonFormatter(logging.Formatter):
    def __init__(self, service):
        super().__init__()
        self.service = service

    def format(self, record):
        entry = {
            "time": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "service": self.service,
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


def get_logger(service):
    """A logger that writes JSON lines to stdout, tagged with `service`."""
    logger = logging.getLogger(service)
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        logger.propagate = False
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter(service))
        logger.addHandler(handler)
    return logger
