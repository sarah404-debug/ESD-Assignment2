"""Quest metrics shared by the HTTP and gRPC services.

Resolve metrics: a quest can be resolved through either service, so both
call record_resolved(). Each process exports its own copy of these counters;
Prometheus scrapes both, and dashboard queries use sum(...) to add them up.

Quests in progress: in Assignment 1 this was a Gauge that the HTTP app moved
up on create and down on resolve. With two services that broke (a gRPC
resolve never moved the HTTP app's gauge down) and it also reset to 0 on
restart. Now it is read from the shared database every time Prometheus
scrapes, so it always matches the source of truth. Only the HTTP app
registers it, so it is exported once.
"""
import time

from prometheus_client import REGISTRY, Counter, Summary
from prometheus_client.core import GaugeMetricFamily

from quest_core import rules, store

# COUNTER: quests resolved, one counter per outcome (names kept from Assignment 1).
quests_completed_total = Counter(
    "quests_completed_total",
    "Total number of quests completed",
    ["category", "difficulty"],
)
quests_skipped_total = Counter(
    "quests_skipped_total",
    "Total number of quests skipped",
    ["category"],
)
quests_abandoned_total = Counter(
    "quests_abandoned_total",
    "Total number of quests abandoned",
    ["category"],
)

# SUMMARY: running count and sum of seconds from creation to resolution
# (so dashboards can show the average).
quest_time_to_resolve_seconds = Summary(
    "quest_time_to_resolve_seconds",
    "Seconds between a quest being generated and being resolved",
    ["status"],
)


def record_resolved(quest):
    """Update the resolve metrics for a quest that has just been resolved."""
    status = quest["status"]
    category = quest["category"]
    quest_time_to_resolve_seconds.labels(status=status).observe(time.time() - quest["created_at"])
    if status == "completed":
        quests_completed_total.labels(category=category, difficulty=quest["difficulty"]).inc()
    elif status == "skipped":
        quests_skipped_total.labels(category=category).inc()
    elif status == "abandoned":
        quests_abandoned_total.labels(category=category).inc()


class QuestsInProgressCollector:
    """Exports quests_in_progress{category}, counted in the database at scrape time."""

    def collect(self):
        gauge = GaugeMetricFamily(
            "quests_in_progress",
            "Number of quests currently in progress (counted in the shared database)",
            labels=["category"],
        )
        counts = store.count_in_progress_by_category()
        # Every category appears, with 0 when it has no quests in progress.
        for category in sorted({q["category"] for q in rules.QUESTS}):
            gauge.add_metric([category], counts.get(category, 0))
        yield gauge


def register_in_progress_collector():
    """Call once, from the HTTP app only, so the gauge is exported by one service."""
    REGISTRY.register(QuestsInProgressCollector())
