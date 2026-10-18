"""Small load generator for the Quest App (standard library only, no installs).

It sends a steady stream of requests so Prometheus, Grafana and the logs have
something to show. Runs with the same --seed do the same thing, which makes it
usable as a repeatable test.

Examples:
    python scripts/load_test.py --duration 60 --rate 2
    python scripts/load_test.py --duration 180 --rate 5 --seed 7
"""
import argparse
import json
import random
import time
import urllib.error
import urllib.request


def call(base_url, method, path):
    """Send one request. Returns (status_code, parsed_json_or_None, seconds_taken)."""
    request = urllib.request.Request(base_url + path, method=method)
    start = time.time()
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            body = response.read()
            status = response.status
    except urllib.error.HTTPError as error:
        # 4xx/5xx answers still count as a response, so record them normally.
        body = error.read()
        status = error.code
    duration = time.time() - start
    try:
        data = json.loads(body)
    except ValueError:
        data = None
    return status, data, duration


def main():
    parser = argparse.ArgumentParser(description="Load generator for the Quest App")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--duration", type=int, default=120, help="seconds to run")
    parser.add_argument("--rate", type=float, default=2.0, help="new quests per second")
    parser.add_argument("--seed", type=int, default=42, help="same seed = same pattern")
    args = parser.parse_args()

    random.seed(args.seed)
    interval = 1.0 / args.rate
    start_time = time.time()
    end_time = start_time + args.duration
    next_send = start_time
    next_progress = start_time + 10

    status_counts = {}       # HTTP status code -> how many responses
    outcomes = {"completed": 0, "skipped": 0, "abandoned": 0, "left open": 0, "bad id": 0}
    new_quest_times = []     # client-side seconds for each GET /quest/new
    total_requests = 0

    def record(status):
        nonlocal total_requests
        total_requests += 1
        status_counts[status] = status_counts.get(status, 0) + 1

    print(f"Sending about {args.rate} new quests/second for {args.duration}s to {args.base_url}")

    try:
        while time.time() < end_time:
            # 1) Ask for a new quest.
            status, quest, took = call(args.base_url, "GET", "/quest/new")
            record(status)
            new_quest_times.append(took)

            # 2) Decide what the "user" does with it.
            roll = random.random()
            if status == 200 and quest:
                instance_id = quest["instance_id"]
                if roll < 0.55:
                    action, outcome = "complete", "completed"
                elif roll < 0.75:
                    action, outcome = "skip", "skipped"
                elif roll < 0.85:
                    action, outcome = "abandon", "abandoned"
                elif roll < 0.95:
                    action, outcome = None, "left open"   # stays in progress
                else:
                    action, outcome = "complete", "bad id"
                    instance_id = "fake-id"               # produces a 404 on purpose
                if action:
                    status, _, _ = call(args.base_url, "POST", f"/quest/{instance_id}/{action}")
                    record(status)
                outcomes[outcome] += 1

            # 3) Wait so we keep a steady pace.
            next_send += interval
            delay = next_send - time.time()
            if delay > 0:
                time.sleep(delay)

            if time.time() >= next_progress:
                print(f"  {int(time.time() - start_time)}s elapsed, {total_requests} requests sent")
                next_progress += 10
    except urllib.error.URLError as error:
        print(f"Could not reach the app: {error}. Is 'docker compose up -d' running?")
        return

    # Summary (client-side view, to compare with Prometheus and the logs).
    new_quest_times.sort()
    average = sum(new_quest_times) / len(new_quest_times)
    p95 = new_quest_times[int(0.95 * (len(new_quest_times) - 1))]
    print("\n--- Summary ---")
    print(f"Total requests: {total_requests}")
    print(f"Responses by status: {status_counts}")
    print(f"Quest outcomes: {outcomes}")
    print(f"GET /quest/new client-side time: average {average * 1000:.1f} ms, p95 {p95 * 1000:.1f} ms")


if __name__ == "__main__":
    main()