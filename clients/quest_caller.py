"""Quest caller: acts like another service that calls the Quest App's HTTP API.

It creates several quests in one run with POST /quests (standard library only,
no installs needed).

Protection against a slow or broken Quest App:
  - Timeout: give up on any single request after TIMEOUT_SECONDS.
  - Retry with backoff: if a request fails with a transient error, try again
    up to MAX_ATTEMPTS times in total, waiting longer before each new try.
    This is only safe because every retry sends the same Idempotency-Key, so
    the app never creates a second quest for the same call.
  - Circuit breaker: after FAILURE_THRESHOLD failed requests in a row, stop
    calling the app at all for RECOVERY_SECONDS (calls fail immediately
    instead). Then let one trial request through: if it works, go back to
    normal; if not, block again for another RECOVERY_SECONDS.

Run from the project folder while the app is running:
    python clients/quest_caller.py
    python clients/quest_caller.py --calls 40 --interval 1
"""
import argparse
import json
import math
import socket
import time
import urllib.error
import urllib.request
import uuid

# ── Settings ────────────────────────────────────────────────
TIMEOUT_SECONDS = 2.0    # longest we wait for one response before giving up
MAX_ATTEMPTS = 3         # 1 first try + up to 2 retries
BACKOFF_SECONDS = 0.5    # wait before retry 1; doubles each time (0.5 s, then 1 s)
FAILURE_THRESHOLD = 5    # failed requests in a row that open the circuit breaker
RECOVERY_SECONDS = 10.0  # how long the breaker blocks calls before a trial request

START_TIME = time.time()


def log(message):
    """Print a line with the seconds since the caller started."""
    print(f"[{time.time() - START_TIME:6.1f}s] {message}")


# ── Errors ──────────────────────────────────────────────────
class TransientError(Exception):
    """A failure that may go away if we try again later: a timeout, the app
    being unreachable, a 5xx server error, or 429 Too Many Requests."""

class PermanentError(Exception):
    """A failure that trying again will not fix, e.g. 422 invalid input."""

class CircuitOpenError(Exception):
    """The circuit breaker is open, so the request was not sent at all."""


# ── Circuit breaker ─────────────────────────────────────────
class CircuitBreaker:
    """Like a fuse box. Three states:

    CLOSED     normal: requests go through; failures in a row are counted.
    OPEN       too many failures: requests are blocked (not sent) until
               RECOVERY_SECONDS have passed.
    HALF_OPEN  recovery time is over: one trial request is let through.
               Success -> CLOSED. Failure -> OPEN again.
    """

    def __init__(self, failure_threshold, recovery_seconds):
        self.failure_threshold = failure_threshold
        self.recovery_seconds = recovery_seconds
        self.state = "CLOSED"
        self.failures_in_a_row = 0
        self.opened_at = 0.0

    def before_request(self):
        """Call before sending a request. Raises CircuitOpenError to block it."""
        if self.state == "OPEN":
            waited = time.time() - self.opened_at
            if waited < self.recovery_seconds:
                raise CircuitOpenError(
                    f"circuit open, request not sent "
                    f"(trial in {math.ceil(self.recovery_seconds - waited)}s)")
            self.state = "HALF_OPEN"
            log("breaker: HALF-OPEN, recovery time over, sending one trial request")

    def record_success(self):
        if self.state == "HALF_OPEN":
            log("breaker: CLOSED, trial request worked, back to normal")
        self.state = "CLOSED"
        self.failures_in_a_row = 0

    def record_failure(self):
        if self.state == "HALF_OPEN":
            self._open("trial request failed")
            return
        self.failures_in_a_row += 1
        if self.state == "CLOSED" and self.failures_in_a_row >= self.failure_threshold:
            self._open(f"{self.failures_in_a_row} failed requests in a row")

    def _open(self, reason):
        self.state = "OPEN"
        self.opened_at = time.time()
        log(f"breaker: OPEN ({reason}), blocking requests for {self.recovery_seconds:g}s")


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
    except ConnectionError as error:
        # The connection broke mid-request (e.g. the app was stopped).
        raise TransientError(f"connection lost ({error})")


# ── Retry with backoff (through the breaker) ────────────────
def create_quest_protected(base_url, idempotency_key, breaker, prefix):
    """Call create_quest with the circuit breaker and retries.

    Every attempt first asks the breaker; if it is open, we stop at once
    (CircuitOpenError) and do not retry. Every attempt uses the SAME
    idempotency_key, so if an earlier attempt actually reached the app (e.g. it
    timed out on our side but the app still created the quest), the retry gets
    that same quest back, not a duplicate. Permanent errors are not retried.
    """
    for attempt in range(1, MAX_ATTEMPTS + 1):
        breaker.before_request()
        try:
            quest = create_quest(base_url, idempotency_key)
        except TransientError as error:
            if attempt == MAX_ATTEMPTS:
                log(f"{prefix}attempt {attempt} failed: {error}; no retries left")
                breaker.record_failure()
                raise
            wait = BACKOFF_SECONDS * 2 ** (attempt - 1)  # 0.5 s, 1 s, ...
            log(f"{prefix}attempt {attempt} failed: {error}; retrying in {wait:g}s")
            breaker.record_failure()
            time.sleep(wait)
            continue
        except PermanentError:
            breaker.record_success()  # the app is up and answering; our input was the problem
            raise
        breaker.record_success()
        if attempt > 1:
            log(f"{prefix}attempt {attempt} succeeded")
        return quest


# ── Main ────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Call the Quest App like another service would")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--calls", type=int, default=10, help="how many quests to create")
    parser.add_argument("--interval", type=float, default=1.0, help="seconds between calls")
    args = parser.parse_args()

    print(f"Calling POST {args.base_url}/quests {args.calls} times\n"
          f"  timeout {TIMEOUT_SECONDS:g}s | up to {MAX_ATTEMPTS} attempts, backoff "
          f"{BACKOFF_SECONDS:g}s doubling | breaker opens after {FAILURE_THRESHOLD} failures "
          f"in a row, recovery {RECOVERY_SECONDS:g}s\n")

    breaker = CircuitBreaker(FAILURE_THRESHOLD, RECOVERY_SECONDS)
    results = {"succeeded": 0, "failed": 0, "blocked": 0}
    for number in range(1, args.calls + 1):
        key = str(uuid.uuid4())  # one key per quest we want, reused by its retries
        prefix = f"call {number:>2}: "
        start = time.time()
        try:
            quest = create_quest_protected(args.base_url, key, breaker, prefix)
            took_ms = (time.time() - start) * 1000
            log(f"{prefix}OK       quest {quest['instance_id'][:8]} "
                f"({quest['category']}, {quest['difficulty']}) in {took_ms:.0f} ms")
            results["succeeded"] += 1
        except CircuitOpenError as error:
            log(f"{prefix}BLOCKED  {error}")
            results["blocked"] += 1
        except (TransientError, PermanentError) as error:
            kind = "transient" if isinstance(error, TransientError) else "permanent"
            log(f"{prefix}FAILED   {error} [{kind}]")
            results["failed"] += 1
        time.sleep(args.interval)

    print(f"\nDone: {results['succeeded']} succeeded, {results['failed']} failed, "
          f"{results['blocked']} blocked by the circuit breaker")


if __name__ == "__main__":
    main()
