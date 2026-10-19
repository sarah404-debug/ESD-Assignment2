"""Run the same "resolve a quest" operation through both versions, side by side.

For each case it calls HTTP PATCH /quests/{id} and gRPC QuestService.ResolveQuest,
and prints the HTTP status code and the gRPC status code. Each case gets its
own request ID (shown in the output), so its log lines can be found in Kibana
with app.request_id : "<id>".

Cases: one success, then failures (unknown quest, already resolved, invalid input).

Run from the project folder while Docker is up (needs grpcio, e.g. the venv):
    venv\\Scripts\\python scripts\\compare_http_grpc.py
"""
import argparse
import json
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import grpc

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from questpb import quest_pb2, quest_pb2_grpc  # noqa: E402

UNKNOWN_ID = "00000000-0000-0000-0000-000000000000"
OUTCOME = {"completed": quest_pb2.OUTCOME_COMPLETED, "skipped": quest_pb2.OUTCOME_SKIPPED}


def http(base_url, method, path, request_id, body=None):
    """One HTTP call. Returns (status code, reason, parsed JSON body)."""
    request = urllib.request.Request(
        base_url + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"X-Request-ID": request_id, **({"Content-Type": "application/json"} if body else {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.reason, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, error.reason, json.loads(error.read())


def short(instance_id):
    """First 8 characters of a long ID, to keep lines short."""
    return instance_id[:8] + "..." if len(instance_id) > 8 else instance_id


def new_quest(base_url, request_id):
    return http(base_url, "POST", "/quests", request_id)[2]["instance_id"]


def run_http(base_url, instance_id, status, request_id):
    code, reason, body = http(base_url, "PATCH", f"/quests/{instance_id}", request_id, {"status": status})
    shown = f"quest status={body['status']}" if code == 200 else f'{{"error": "{body["error"]}", "message": "{body["message"]}"}}'
    print(f"  HTTP  PATCH /quests/{short(instance_id)} {{\"status\": \"{status}\"}}")
    print(f"        -> {code} {reason}   {shown}")


def run_grpc(stub, instance_id, status, request_id):
    request = quest_pb2.ResolveQuestRequest(instance_id=instance_id, outcome=OUTCOME.get(status, 0))
    print(f"  gRPC  ResolveQuest(instance_id={short(instance_id)}, outcome={quest_pb2.Outcome.Name(request.outcome)})")
    try:
        response = stub.ResolveQuest(request, timeout=2, metadata=(("x-request-id", request_id),))
        print(f"        -> OK   quest status={response.quest.status}, changed={response.changed}")
    except grpc.RpcError as error:
        print(f"        -> {error.code().name}   \"{error.details()}\"")


def main():
    parser = argparse.ArgumentParser(description="Same operation over HTTP and gRPC")
    parser.add_argument("--http-url", default="http://127.0.0.1:8000")
    parser.add_argument("--target", default="localhost:50051")
    args = parser.parse_args()
    run_id = uuid.uuid4().hex[:6]

    with grpc.insecure_channel(args.target) as channel:
        stub = quest_pb2_grpc.QuestServiceStub(channel)

        def case(number, title, prepare):
            request_id = f"cmp-{run_id}-{number}"
            print(f"\n=== {number}. {title}   (request ID: {request_id})")
            http_id, grpc_id, status = prepare(request_id)
            run_http(args.http_url, http_id, status, request_id)
            run_grpc(stub, grpc_id, status, request_id)

        # Each version gets its own fresh quest, so neither affects the other's result.
        case(1, "SUCCESS: resolve an in-progress quest", lambda rid: (
            new_quest(args.http_url, rid), new_quest(args.http_url, rid), "completed"))
        case(2, "FAILURE: quest does not exist", lambda rid: (UNKNOWN_ID, UNKNOWN_ID, "completed"))

        def already_resolved(rid):
            ids = [new_quest(args.http_url, rid), new_quest(args.http_url, rid)]
            for qid in ids:  # resolve both first, then ask for a different outcome
                http(args.http_url, "PATCH", f"/quests/{qid}", rid, {"status": "completed"})
            return ids[0], ids[1], "skipped"
        case(3, "FAILURE: quest already resolved with another outcome", already_resolved)
        case(4, "FAILURE: invalid input (ID is not a UUID)", lambda rid: ("abc", "abc", "completed"))


if __name__ == "__main__":
    main()
