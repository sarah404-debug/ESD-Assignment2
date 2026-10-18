"""Quest caller: acts like another service that calls the Quest App's HTTP API.

It creates several quests in one run with POST /quests (standard library only,
no installs needed).

Protection against a slow or broken Quest App:
  - Timeout: give up on any single request after TIMEOUT_SECONDS.
  - Retry with backoff: if a request fails with a transient error, try again
    up to MAX_ATTEMPTS times in total, waiting longer before each new try.
    This is only safe because every retry sends the same Idempotency-Key, so
    the app never creates a second quest for the same call.

Run from the project folder while the app is running:
    python clients/quest_caller.py
    python clients/quest_caller.py --calls 20 --interval 0.5
"""
import argparse
import json
import socket
import time
import urllib.error
import urllib.request
import uuid

# ── Settings ────────────────────────────────────────────────
TIMEOUT_SECONDS = 2.0  # longest we wait for one response before giving up
MAX_ATTEMPTS = 3       # 1 first try + up to 2 retries
BACKOFF_SECONDS = 0.5  # wait before retry 1; doubles each time (0.5 s, then 1 s)


# ── Errors ──────────────────────────────────────────────────
class TransientError(Exception):
    """A failure that may go away if we try again later: a timeout, the app
    being unreachable, a 5xx server error, or 429 Too Many Requests."""

class PermanentError(Exception):
    """A failure that trying again will not fix, e.g. 422 invalid input."""


# ── One HTTP request ────────────────────────────────────────
def create_quest(base_url, idempotency_key):
    """Send one POST /quests. Returns the new quest (a dict).

    Raises TransientError or PermanentError if it fails. The Idempotency-Key
    means that if this exact request is ever sent again, the app returns the
    same quest instead of creating a second one.
    """
    request = urllib.request.Request(
        base_url + "/quests",
        method="POST",
        headers={"Idempotency-Key": idempotency_key},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        # The app answered, but with an error status.
        try:
            message = json.loads(error.read()).get("message", "")
        except ValueError:
            message = ""
        if error.code >= 500 or error.code == 429:
            raise TransientError(f"HTTP {error.code} {message}".strip())
        raise PermanentError(f"HTTP {error.code} {message}".strip())
    except (socket.timeout, TimeoutError):
        raise TransientError(f"timed out after {TIMEOUT_SECONDS}s")
    except urllib.error.URLError as error:
        # Could not connect at all (app stopped?), or a timeout while connecting.
        if isinstance(error.reason, (socket.timeout, TimeoutError)):
            raise TransientError(f"timed out after {TIMEOUT_SECONDS}s")
        raise TransientError(f"cannot reach the app ({error.reason})")


# ── Retry with backoff ──────────────────────────────────────
def create_quest_with_retry(base_url, idempotency_key, log):
    """Call create_quest, retrying transient failures with growing waits.

    Every attempt uses the SAME idempotency_key, so if an earlier attempt
    actually reached the app (e.g. it timed out on our side but the app still
    created the quest), the retry gets that same quest back, not a duplicate.
    Permanent errors are not retried: trying again would not help.
    """
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            quest = create_quest(base_url, idempotency_key)
            if attempt > 1:
                log(f"attempt {attempt} succeeded")
            return quest
        except TransientError as error:
            if attempt == MAX_ATTEMPTS:
                log(f"attempt {attempt} failed: {error}; no retries left")
                raise
            wait = BACKOFF_SECONDS * 2 ** (attempt - 1)  # 0.5 s, 1 s, ...
            log(f"attempt {attempt} failed: {error}; retrying in {wait:g}s")
            time.sleep(wait)


# ── Main ────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Call the Quest App like another service would")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--calls", type=int, default=10, help="how many quests to create")
    parser.add_argument("--interval", type=float, default=1.0, help="seconds between calls")
    args = parser.parse_args()

    print(f"Calling POST {args.base_url}/quests {args.calls} times "
          f"(timeout {TIMEOUT_SECONDS}s, up to {MAX_ATTEMPTS} attempts, "
          f"backoff from {BACKOFF_SECONDS}s doubling)\n")

    results = {"ok": 0, "failed": 0}
    for number in range(1, args.calls + 1):
        key = str(uuid.uuid4())  # one key per quest we want, reused by its retries
        start = time.time()

        def log(message, number=number):
            print(f"call {number:>2}:         {message}")

        try:
            quest = create_quest_with_retry(args.base_url, key, log)
            took_ms = (time.time() - start) * 1000
            print(f"call {number:>2}: OK      quest {quest['instance_id'][:8]} "
                  f"({quest['category']}, {quest['difficulty']}) in {took_ms:.0f} ms")
            results["ok"] += 1
        except (TransientError, PermanentError) as error:
            kind = "transient" if isinstance(error, TransientError) else "permanent"
            print(f"call {number:>2}: FAILED  {error} [{kind}]")
            results["failed"] += 1
        time.sleep(args.interval)

    print(f"\nDone: {results['ok']} succeeded, {results['failed']} failed")


if __name__ == "__main__":
    main()
