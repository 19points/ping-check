# Ping Test Utility

Containerized IP uptime checker with a simple web dashboard, response-time graph, and outage history. Python standard library + SQLite; no frontend dependencies.

## Run

```sh
docker compose up -d --build
```

Open **http://localhost:8088**, then add IPv4 or IPv6 addresses. Data persists in the `ping-data` Docker volume across container restarts and rebuilds.

```sh
docker compose logs -f
docker compose down
```

The port binds to localhost by default. There is **no authentication**: use an authenticated reverse proxy before exposing it remotely. Only let trusted users access it, since they can probe addresses reachable from the container and delete monitoring history.

## Behavior

- Healthy/pending targets: one ICMP echo probe every 10 seconds.
- Failed probe: open an outage and retry every second until a reply closes it.
- Probe timeout: 1 second (2-second process safety timeout). Scheduling is best-effort, with up to 32 concurrent probes; a large target list can delay checks.
- Outage timestamps represent probe start times. A single missed reply counts as downtime; ICMP filtering can therefore appear as an outage.
- Response times and failures are stored in SQLite. Samples are retained for 30 days; outage records are kept until the target is removed.
- Dashboard refreshes every second. The graph defaults to the last **1h**, with **1h, 3h, 8h, 1d, 2d, 7d** options. Samples are averaged into up to 1,000 time buckets; buckets containing failures appear as gaps. Outages overlapping the selected window are shown in red and listed below the graph.
- Removing a target permanently deletes its samples and outages.
- Monitoring only runs while the service is running. No checks are inferred during service downtime. An already-open outage remains open across restarts until a successful probe.

Docker networking must support the target address family. IPv6 targets require working IPv6 connectivity in the container. The image includes `iputils-ping`; Compose grants `NET_RAW` for ICMP support.

## Local development

Requires Python 3.10+ and Linux `ping` (`iputils-ping`).

```sh
python app.py
python -m unittest -v
```

Configuration through environment variables:

| Variable | Default | Description |
| --- | --- | --- |
| `PORT` | `8088` | HTTP listen port (all interfaces); Docker uses internal port `8080`, published locally as `8088` |
| `DB_PATH` | `data/ping.db` | SQLite database path (`/data/ping.db` in Docker) |
| `RETENTION_DAYS` | `30` | Raw sample retention; cleanup runs hourly |

Run only one application instance per database. Back up SQLite using its backup API or stop the service before copying the database and its WAL files.

## API

- `GET /api/targets` — target status and most recent latency
- `POST /api/targets` with `{"address":"1.1.1.1"}` — add a target
- `GET /api/targets/{id}/history?range=1h` — aggregated samples and overlapping outages for `1h` (default), `3h`, `8h`, `1d`, `2d`, or `7d`; includes window `start`/`end` (Unix seconds; null latency means a bucket contains failures)
- `DELETE /api/targets/{id}` — remove a target and its history
