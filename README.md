# Quest App — HTTP APIs, gRPC, and Metrics

Enterprise Software Development, Assignment 2.

This project extends my Assignment 1 Quest App
(https://github.com/sarah404-debug/quest-app). The starting point of this
repository is a copy of the Assignment 1 code; Assignment 2 changes are made
on top of it.

Quest App gives a person a random small "side quest" (e.g. "Take a 20-minute
walk with no phone") through an HTTP API. Every quest lifecycle event and
every HTTP request is measured (Prometheus/Grafana) and logged as JSON
(Filebeat/Elasticsearch/Kibana). Quests live in memory only — they are lost
when the app restarts. A small one-page UI is served at `/ui`.

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
allowed). Resolving an already-resolved quest returns `409`.

For a steady stream of realistic traffic instead of manual clicking, use the
load generator (standard library only, no installs):
```
python scripts\load_test.py --duration 60 --rate 2
```
`--seed` makes runs repeatable (same seed = same sequence of outcomes).

## Test

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

## Safely clean up

- **Pause everything** (keeps all data): `docker compose stop`
- **Resume:** `docker compose up -d`
- **Do NOT run `docker compose down`** casually — it removes containers.
  Elasticsearch's logs survive this (named volume `esdata`), but **Grafana
  and Prometheus have no volumes, so their dashboards and metric history are
  lost.** The Grafana dashboard is exported to
  `monitoring/dashboards/*.json` as a backup for exactly this reason — if
  you do run `down`, re-import that file into a fresh Grafana instance to
  get the dashboard back.
- **`docker compose down -v`** additionally deletes the `esdata` volume,
  permanently removing all stored logs. If you do this and want retention
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
