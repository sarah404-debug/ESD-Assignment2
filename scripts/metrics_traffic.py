"""Repeatable traffic for the Part C dashboards: "resolve a quest" through both versions.

Every step resolves one quest, alternating between HTTP (PATCH /quests/{id})
and gRPC (QuestService.ResolveQuest), so both versions get the same amount and
the same mix of traffic. Each step is one of these cases, picked at random
with a fixed seed (same seed = exactly the same sequence of cases):

    success    resolve an in-progress quest           HTTP 200 / gRPC OK
    repeat     resolve it, then send the same again   HTTP 200 / gRPC OK (twice)
    not_found  quest ID that does not exist           HTTP 404 / gRPC NOT_FOUND
    conflict   resolve, then ask for another outcome  HTTP 409 / gRPC FAILED_PRECONDITION
    invalid    ID that is not a UUID                  HTTP 422 / gRPC INVALID_ARGUMENT

Quests are created with HTTP POST /quests (the only way to create one). At the
default --rate 1 that stays well inside the POST /quests rate limit (30 per 10 s).

Run from the project folder while Docker is up (needs grpcio, e.g. the venv):
    venv\\Scripts\\python scripts\\metrics_traffic.py --duration 300
    venv\\Scripts\\python scripts\\metrics_traffic.py --duration 300 --seed 7
"""
import argparse
import json
import random
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

import grpc

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from questpb import quest_pb2, quest_pb2_grpc  # noqa: E402

UNKNOWN_ID = "00000000-0000-0000-0000-000000000000"
OUTCOMES = ["completed", "skipped", "abandoned"]
GRPC_OUTCOME = {
    "completed": quest_pb2.OUTCOME_COMPLETED,
    "skipped": quest_pb2.OUTCOME_SKIPPED,
    "abandoned": quest_pb2.OUTCOME_ABANDONED,
}
# Case -> how often it is picked (about 60% of steps succeed).
CASE_WEIGHTS = {"success": 45, "repeat": 15, "not_found": 15, "conflict": 15, "invalid": 10}


class Traffic:
    def __init__(self, http_url, stub, seed):
        self.http_url = http_url
        self.stub = stub
        self.seed = seed
        self.results = Counter()  # (door, result) -> count

    def http(self, method, path, request_id, body=None):
        request = urllib.request.Request(
            self.http_url + path, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"X-Request-ID": request_id, **({"Content-Type": "application/json"} if body else {})},
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def new_quest(self, request_id):
        status, body = self.http("POST", "/quests", request_id)
        if status != 201:
            raise RuntimeError(f"could not create a quest: HTTP {status} {body}")
        return body["instance_id"]

    def resolve(self, door, instance_id, outcome, request_id):
        """Resolve through one door; record and return the HTTP or gRPC status."""
        if door == "HTTP":
            status, _ = self.http("PATCH", f"/quests/{instance_id}", request_id, {"status": outcome})
            result = str(status)
        else:
            request = quest_pb2.ResolveQuestRequest(instance_id=instance_id, outcome=GRPC_OUTCOME[outcome])
            try:
                self.stub.ResolveQuest(request, timeout=2, metadata=(("x-request-id", request_id),))
                result = "OK"
            except grpc.RpcError as error:
                result = error.code().name
        self.results[(door, result)] += 1
        return result

    def step(self, number, case, door, rng):
        request_id = f"traffic-{self.seed}-{number}"
        outcome = rng.choice(OUTCOMES)
        if case in ("success", "repeat", "conflict"):
            instance_id = self.new_quest(request_id)
            self.resolve(door, instance_id, outcome, request_id)
            if case == "repeat":
                self.resolve(door, instance_id, outcome, request_id)
            elif case == "conflict":
                other = rng.choice([o for o in OUTCOMES if o != outcome])
                self.resolve(door, instance_id, other, request_id)
        elif case == "not_found":
            self.resolve(door, UNKNOWN_ID, outcome, request_id)
        else:  # invalid
            self.resolve(door, "abc", outcome, request_id)


def main():
    parser = argparse.ArgumentParser(description="Repeatable HTTP + gRPC traffic for the dashboards")
    parser.add_argument("--http-url", default="http://127.0.0.1:8000")
    parser.add_argument("--target", default="localhost:50051", help="gRPC server address")
    parser.add_argument("--duration", type=int, default=120, help="seconds to run")
    parser.add_argument("--rate", type=float, default=1.0, help="steps per second (both doors together)")
    parser.add_argument("--seed", type=int, default=42, help="same seed = same sequence of cases")
    args = parser.parse_args()

    rng = random.Random(args.seed)
    steps = int(args.duration * args.rate)
    interval = 1.0 / args.rate
    print(f"{steps} steps over {args.duration}s (seed {args.seed}), alternating HTTP and gRPC")

    with grpc.insecure_channel(args.target) as channel:
        traffic = Traffic(args.http_url, quest_pb2_grpc.QuestServiceStub(channel), args.seed)
        cases = Counter()
        start = time.time()
        for number in range(steps):
            case = rng.choices(list(CASE_WEIGHTS), weights=list(CASE_WEIGHTS.values()))[0]
            door = "HTTP" if number % 2 == 0 else "gRPC"
            traffic.step(number, case, door, rng)
            cases[case] += 1
            if (number + 1) % 30 == 0:
                print(f"  {int(time.time() - start)}s: {number + 1}/{steps} steps")
            delay = start + (number + 1) * interval - time.time()  # keep a steady pace
            if delay > 0:
                time.sleep(delay)

    print("\n--- Summary ---")
    print("Cases:", dict(sorted(cases.items())))
    for door in ("HTTP", "gRPC"):
        by_result = {r: n for (d, r), n in sorted(traffic.results.items()) if d == door}
        total = sum(by_result.values())
        errors = sum(n for r, n in by_result.items() if r not in ("200", "OK"))
        print(f"{door:4} resolve calls: {total}  {by_result}  error rate {100 * errors / max(total, 1):.0f}%")


if __name__ == "__main__":
    main()
