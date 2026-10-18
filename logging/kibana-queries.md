# Kibana queries for the Quest App logs

## How to use these

1. Open Kibana Discover: http://localhost:5601/app/discover
2. Pick the data view **quest-app-logs** (index pattern `quest-app-logs-*`, time field `@timestamp`).
3. Set the time range at the top right (for example **Last 7 days**).
4. Type a query into the KQL search bar and press Enter.

The app writes one JSON object per log line. Filebeat parses it and stores the
fields under `app.*` (for example `app.severity`, `app.message`, `app.request_id`,
`app.event`, `app.endpoint`, `app.status`, `app.duration_ms`).

Kibana shows times in the browser's timezone. The raw logs and Elasticsearch use UTC
(for example 19:58 UTC shows as 00:58 in Pakistan, UTC+5).

## Queries tested and working (22 Sep 2026)

| Query | What it finds | Result when tested |
|---|---|---|
| `app.severity : "WARNING"` | Every warning line (client errors and app warnings) | 2 documents |
| `app.request_id : "08d08ced29b7"` | Every log line written while handling one request | 2 documents ("quest not found" and "request completed") |
| `app.event : "quest_generated"` | Each time a quest was handed out by `/quest/new` | 16 documents |
| `app.status >= 400` | HTTP requests that failed with a 4xx or 5xx status | 1 document |
| `app.severity : "ERROR"` | Server errors (unhandled exceptions, 5xx responses) | 0 documents (no server errors so far) |

Counts grow as more traffic is generated. They are the results at the time of testing.

`app.status` only exists on the "request completed" lines, so `app.status >= 400`
finds fewer documents than `app.severity : "WARNING"`, which also matches the
"quest not found" line written inside the app.

## How to find one request or one error

- Find every log line of one request: copy its `request_id` (the app also returns it in
  the `X-Request-ID` response header) and search `app.request_id : "<id>"`.
- Find one kind of event: `app.event : "quest_not_found"` (also `quest_generated`,
  `quest_resolved`, `quest_already_resolved`, `request_completed`). The
  `quest_not_found` search was verified through the Elasticsearch search API.

## Planned for Part E1 (not tested yet)

- `app.duration_ms >= 400` to find slow requests during the fault experiment.

## Log retention setting (Elasticsearch, applied once)

Logs are kept for at least 7 days using a data stream lifecycle. This setting is stored
inside Elasticsearch, so re-run it if the Elasticsearch data volume is ever deleted:

```
curl -X PUT "http://localhost:9200/_data_stream/quest-app-logs-*/_lifecycle" -H "Content-Type: application/json" -d "{\"data_retention\":\"7d\"}"
```

Check it with:

```
curl "http://localhost:9200/_data_stream/quest-app-logs-*?pretty"
```