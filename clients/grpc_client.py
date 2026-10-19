"""gRPC client for the Quest App: calls QuestService.ResolveQuest directly.

It talks to the gRPC service through the generated stub (questpb/quest_pb2_grpc.py),
not through the HTTP app. Every call has a deadline: if the server has not
answered in time, gRPC cancels the call with DEADLINE_EXCEEDED.

Examples (run from the project folder, with the venv's Python):
    python clients/grpc_client.py 0b614e9e-2f05-4061-93df-07b4c70a63a4 completed
    python clients/grpc_client.py --new skipped          # create a quest over HTTP first
    python clients/grpc_client.py abc completed          # fails: INVALID_ARGUMENT
    python clients/grpc_client.py --new completed --deadline 0.001   # fails: DEADLINE_EXCEEDED
"""
import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

import grpc

# Make the project folder importable, so `questpb` is found from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from questpb import quest_pb2, quest_pb2_grpc  # noqa: E402

OUTCOMES = {
    "completed": quest_pb2.OUTCOME_COMPLETED,
    "skipped": quest_pb2.OUTCOME_SKIPPED,
    "abandoned": quest_pb2.OUTCOME_ABANDONED,
    "unspecified": quest_pb2.OUTCOME_UNSPECIFIED,  # to test the server's validation
}


def create_quest_over_http(http_url):
    """Helper for --new: make a fresh quest with HTTP POST /quests and return its ID."""
    request = urllib.request.Request(http_url + "/quests", method="POST")
    with urllib.request.urlopen(request, timeout=5) as response:
        quest = json.loads(response.read())
    print(f"(HTTP POST /quests created quest {quest['instance_id']})")
    return quest["instance_id"]


def main():
    parser = argparse.ArgumentParser(description="Resolve a quest through the gRPC service")
    parser.add_argument("instance_id", nargs="?", help="quest ID (leave out when using --new)")
    parser.add_argument("outcome", choices=OUTCOMES, help="how the quest was resolved")
    parser.add_argument("--new", action="store_true", help="create a quest over HTTP first and resolve that one")
    parser.add_argument("--target", default="localhost:50051", help="gRPC server address")
    parser.add_argument("--http-url", default="http://127.0.0.1:8000", help="HTTP app, only used by --new")
    parser.add_argument("--deadline", type=float, default=2.0, help="seconds to wait for the answer")
    args = parser.parse_args()

    if args.new:
        instance_id = create_quest_over_http(args.http_url)
    elif args.instance_id:
        instance_id = args.instance_id
    else:
        parser.error("give a quest ID, or use --new")

    request = quest_pb2.ResolveQuestRequest(instance_id=instance_id, outcome=OUTCOMES[args.outcome])
    print(f"gRPC  QuestService.ResolveQuest  target={args.target}  deadline={args.deadline:g}s")
    print(f"      request: instance_id={instance_id}  outcome={quest_pb2.Outcome.Name(request.outcome)}")

    with grpc.insecure_channel(args.target) as channel:
        stub = quest_pb2_grpc.QuestServiceStub(channel)  # the generated client stub
        start = time.time()
        try:
            response = stub.ResolveQuest(request, timeout=args.deadline)
        except grpc.RpcError as error:
            took_ms = (time.time() - start) * 1000
            print(f"FAILED  status={error.code().name}  ({took_ms:.0f} ms)")
            print(f"        details: {error.details()}")
            sys.exit(1)

    took_ms = (time.time() - start) * 1000
    quest = response.quest
    print(f"OK      status=OK  ({took_ms:.0f} ms)")
    print(f"        quest {quest.instance_id}: {quest.status} "
          f"({quest.category}, {quest.difficulty}) \"{quest.text}\"")
    print(f"        changed={response.changed}"
          + ("" if response.changed else "  (it already had this outcome: a safe repeat)"))


if __name__ == "__main__":
    main()
