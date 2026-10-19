"""Quest business rules, shared by the HTTP service and the gRPC service.

Both services call these functions, so a quest is created and resolved by
exactly the same rules whichever way the request arrives:

  - A new quest is a random one from QUESTS, starting "in_progress".
  - A repeated create with the same Idempotency-Key returns the quest that key
    already created, instead of a new one.
  - An in-progress quest can be resolved to "completed", "skipped" or "abandoned".
  - Asking for the status a quest already has is a safe repeat: nothing changes.
  - Asking for a *different* status on an already-resolved quest is a conflict.

The rules know nothing about HTTP or gRPC. Problems are raised as the
exceptions below, and each service turns them into its own error format
(e.g. QuestNotFound -> HTTP 404 / gRPC NOT_FOUND). Metrics and logging also
stay in each service.
"""
import random
import time
import uuid

from quest_core import store

# Statuses a quest can be resolved to.
FINAL_STATUSES = ("completed", "skipped", "abandoned")

# The quests that can be handed out.
QUESTS = [
    {"id": 1, "category": "social", "difficulty": "easy", "text": "Call someone you haven't spoken to in a while."},
    {"id": 2, "category": "reflective", "difficulty": "easy", "text": "Write down 3 things you're avoiding right now."},
    {"id": 3, "category": "physical", "difficulty": "medium", "text": "Take a 20-minute walk with no phone."},
    {"id": 4, "category": "creative", "difficulty": "easy", "text": "Doodle something for 5 minutes, no judging it."},
    {"id": 5, "category": "social", "difficulty": "hard", "text": "Tell someone something you appreciate about them, out loud."},
]


# ── Errors ──────────────────────────────────────────────────
class QuestError(Exception):
    """Base class for an expected problem with a quest request."""

class InvalidInput(QuestError):
    """The request itself is invalid, e.g. a malformed ID or an unknown status."""

class QuestNotFound(QuestError):
    """No quest has this ID."""

class QuestAlreadyResolved(QuestError):
    """The quest was already resolved with a different status."""
    def __init__(self, current_status):
        super().__init__(f"Quest is already '{current_status}'")
        self.current_status = current_status


# ── Rules ───────────────────────────────────────────────────
def check_instance_id(instance_id):
    """Return the ID in standard UUID form, or raise InvalidInput."""
    try:
        return str(uuid.UUID(str(instance_id)))
    except ValueError:
        raise InvalidInput("instance_id must be a valid UUID")


def create_quest(idempotency_key=None):
    """Create a random in-progress quest. Returns (quest, created).

    If idempotency_key was used before, no new quest is made: the quest that
    key created is returned with created=False.
    """
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
    return store.insert_quest(new_instance, idempotency_key)


def get_quest(instance_id):
    """Return one quest, or raise QuestNotFound / InvalidInput."""
    quest = store.get_quest(check_instance_id(instance_id))
    if quest is None:
        raise QuestNotFound("Quest instance not found")
    return quest


def list_quests(status=None):
    """Return all quests, optionally only those with one status."""
    return store.list_quests(status)


def resolve_quest(instance_id, new_status):
    """Resolve a quest to new_status. Returns (quest, changed).

    changed=False means the quest already had new_status (a safe repeat).
    Raises InvalidInput, QuestNotFound or QuestAlreadyResolved.
    """
    if new_status not in FINAL_STATUSES:
        raise InvalidInput(f"status must be one of {', '.join(FINAL_STATUSES)}")
    quest = get_quest(instance_id)

    # Still in progress: change it, but only if it is still in progress at
    # that moment. If another request (HTTP or gRPC) resolved it in the
    # meantime, re-read it and let the checks below decide.
    if quest["status"] == "in_progress":
        if store.change_status(quest["instance_id"], "in_progress", new_status):
            quest["status"] = new_status
            return quest, True
        quest = get_quest(instance_id)

    if quest["status"] == new_status:
        return quest, False
    raise QuestAlreadyResolved(quest["status"])
