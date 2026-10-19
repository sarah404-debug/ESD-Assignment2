# Quest App — HTTP APIs, gRPC, and Metrics

Enterprise Software Development, Assignment 2.

This project extends my Assignment 1 Quest App
(https://github.com/sarah404-debug/quest-app). The starting point of this
repository is a copy of the Assignment 1 code; Assignment 2 changes are made
on top of it.

Quest App gives a person a random small "side quest" (e.g. "Take a 20-minute
walk with no phone") through an HTTP API. Every quest lifecycle event and
every HTTP request is measured (Prometheus/Grafana) and logged as JSON
(Filebeat/Elasticsearch/Kibana). Quests are stored in a small SQLite
database (`quest_core/store.py`) on the Docker volume `questdata`, so they
survive restarts and can be shared with the gRPC service. A small one-page UI
is served at `/ui`.

## Start

1. Open Docker Desktop and wait until it's running.
2. From this folder, in a terminal:
   ```
   docker compose up -d
   ```
   Add `--build` if `app/main.py` changed.
3. Elasticsearch and Kibana take a minute or two to start; other services are
   fast.

## Use

| What | URL |
|---|---|
| App (web page) | http://127.0.0.1:8000/ui |
| API docs (Swagger) | http://127.0.0.1:8000/docs |
| Metrics | http://127.0.0.1:8000/metrics |
| Prometheus | http://localhost:9090 (targets: `/targets`) |
| Grafana | http://localhost:3000 (admin / admin) |
| Kibana | http://localhost:5601/app/discover |
| Elasticsearch | http://localhost:9200 |

Generate traffic by clicking "New quest" on the web page, or with curl.
Create a quest (returns `201 Created`):
```
curl -X POST http://127.0.0.1:8000/quests
```
See all quests, only some of them, or one quest:
```
curl http://127.0.0.1:8000/quests
curl "http://127.0.0.1:8000/quests?status=in_progress"
curl http://127.0.0.1:8000/quests/{instance_id}
```
Resolve a quest (`status` can be `completed`, `skipped` or `abandoned`):
```
curl -X PATCH http://127.0.0.1:8000/quests/{instance_id} -H "Content-Type: application/json" -d "{\"status\":\"completed\"}"
```
Failure examples:
```
curl -X PATCH http://127.0.0.1:8000/quests/00000000-0000-0000-0000-000000000000 -H "Content-Type: application/json" -d "{\"status\":\"completed\"}"
curl -X PATCH http://127.0.0.1:8000/quests/{instance_id} -H "Content-Type: application/json" -d "{\"status\":\"banana\"}"
```
The first returns `404` (no such quest), the second `422` (status not
allowed). Resolving a quest to a *different* status than it already has
returns `409`; sending the same status again is safe and returns `200`.

Safe retries of quest creation: send an `Idempotency-Key` header. Repeating
the request with the same key returns the same quest (with header
`Idempotent-Replayed: true`) instead of creating a second one:
```
curl -i -X POST http://127.0.0.1:8000/quests -H "Idempotency-Key: my-key-1"
curl -i -X POST http://127.0.0.1:8000/quests -H "Idempotency-Key: my-key-1"
```

For a steady stream of realistic traffic instead of manual clicking, use the
load generator (standard library only, no installs):
```
python scripts\load_test.py --duration 60 --rate 2
```
`--seed` makes runs repeatable (same seed = same sequence of outcomes).

## OpenAPI

The API is described by an OpenAPI spec, saved in `openapi/openapi.json`.
FastAPI builds it from the code, and the running app also serves it at
http://127.0.0.1:8000/openapi.json. To try endpoints from the spec, open
http://127.0.0.1:8000/docs, pick an endpoint, click **Try it out**, then
**Execute**.

After changing an endpoint, regenerate the saved file:
```
python scripts\export_openapi.py
```

## gRPC contract

The gRPC service's contract is `questpb/quest.proto` (one RPC,
`QuestService.ResolveQuest`). The Python code generated from it
(`questpb/quest_pb2.py`, `quest_pb2.pyi`, `quest_pb2_grpc.py`) is committed,
so nothing needs generating to run the project.

One-time setup of a local Python environment with the gRPC tools:
```
python -m venv venv
venv\Scripts\python -m pip install -r requirements-dev.txt
```
Regenerate the code after changing `quest.proto` (run from the project folder):
```
venv\Scripts\python -m grpc_tools.protoc -I. --python_out=. --pyi_out=. --grpc_python_out=. questpb/quest.proto
```

## Test

- **Unit tests** for the shared quest rules (`quest_core/rules.py`), using a
  throwaway database, no app or Docker needed:
  ```
  python -m unittest discover -s tests -v
  ```
- **Dashboards:** open the "Quest App Dashboard" in Grafana for metrics
  (Counters, Gauges, Histograms, a Summary, and Node Exporter machine
  metrics). Open Kibana Discover (data view `quest-app-logs`) for logs; see
  `logging/kibana-queries.md` for ready-made searches.
- **Fault injection (Part E1):** the app has an environment-variable switch,
  off by default, that delays every Nth call to `POST /quests`:
  ```
  set FAULT_DELAY_MS=500
  set FAULT_EVERY_N=5
  docker compose up -d quest-app
  ```
  Turn it off again the same way with both values set to `0` (or just
  `docker compose up -d quest-app` with the variables unset, since the
  compose file defaults them to `0`).
- **Rate limiting:** `POST /quests` accepts at most 30 calls per client IP
  address in each 10-second window. Call 31 onwards gets `429 Too Many
  Requests` with a `Retry-After` header (seconds until the window ends).
  Accepted calls show `RateLimit-Remaining`. Quick demo:
  ```
  python scripts\rate_limit_demo.py
  ```
  Change the limit with `RATE_LIMIT_REQUESTS` and
  `RATE_LIMIT_WINDOW_SECONDS` (same way as the fault-injection variables).
  The load generator's default `--rate 2` (20 calls per 10 s) stays well
  inside the limit.
- **Python caller (timeout, retry, circuit breaker):**
  `clients/quest_caller.py` calls `POST /quests` like another service would.
  Settings: timeout 2 s; up to 3 attempts with backoff 0.5 s then 1 s (only
  transient failures are retried, always with the same `Idempotency-Key`);
  circuit breaker opens after 5 failed requests in a row and lets a trial
  request through after 10 s.
  ```
  python clients\quest_caller.py
  ```
  Full demo (success, retry, breaker open, recovery):
  1. Make every 4th create slow, so some requests time out and get retried:
     ```
     set FAULT_DELAY_MS=3000
     set FAULT_EVERY_N=4
     docker compose up -d quest-app
     ```
  2. Start the caller: `python clients\quest_caller.py --calls 40 --interval 1`
  3. After about 8 s, in a second terminal: `docker compose stop quest-app`
     (the caller's requests fail, then the breaker opens and blocks calls).
  4. About 25 s later: `docker compose start quest-app` (the next trial
     request succeeds and the breaker closes).
  5. Afterwards, turn the fault off again (set both values to `0` and run
     `docker compose up -d quest-app`).

## Safely clean up

- **Pause everything** (keeps all data): `docker compose stop`
- **Resume:** `docker compose up -d`
- **Do NOT run `docker compose down`** casually — it removes containers.
  Elasticsearch's logs and the quests survive this (named volumes `esdata`
  and `questdata`), but **Grafana
  and Prometheus have no volumes, so their dashboards and metric history are
  lost.** The Grafana dashboard is exported to
  `monitoring/dashboards/*.json` as a backup for exactly this reason — if
  you do run `down`, re-import that file into a fresh Grafana instance to
  get the dashboard back.
- **`docker compose down -v`** additionally deletes the `esdata` and
  `questdata` volumes, permanently removing all stored logs and quests. If you do this and want retention
  configured again afterwards, re-run:
  ```
  curl -X PUT "http://localhost:9200/_data_stream/quest-app-logs-*/_lifecycle" -H "Content-Type: application/json" -d "{\"data_retention\":\"7d\"}"
  ```
  (This sets Elasticsearch to keep logs for at least 7 days before they
  become eligible for automatic deletion. It only needs to be set once per
  fresh Elasticsearch volume — the setting itself does not survive `down -v`
  because that command deletes the volume it's stored in.)

## Notes

- Docker images for Prometheus, Grafana and Node Exporter use `:latest`, so
  exact versions may differ between machines (built and tested here against
  Grafana 13.2.2).
- Node Exporter measures the Docker Desktop WSL2 Linux VM the containers run
  in, not Windows itself.
- AI assistance was used in this project; see the accompanying report for
  a statement of what was used and how, per the course's AI use policy.
