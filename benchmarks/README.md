# Top-N benchmark (2026-09-22)

The two JSON records use the same deterministic 500-paper, 80-word-per-paper
synthetic corpus (seed `20260921`, SHA-256
`4b2b3d1352b0a9a927ebfc716075a56a9f40361b344c8715bc901f31c651df0e`).
Dataproc produced 42 inverted-index terms. The worker seeded that completed
index into PostgreSQL and Redis, verified all 42 Redis sorted-set entries, and
created a completed task for the web session. A separate public web submission
of the already indexed source also went from PENDING to COMPLETE via Pub/Sub.

| Measure | Result | Scope |
| --- | ---: | --- |
| Redis Top-10 processing, public-LB run | median 0.88 ms; p95 1.05 ms; 50 samples | Web app timer around the Redis lookup and result conversion, after session validation and before HTML rendering |
| Full HTTP response, public-LB run | median 104.106 ms; p95 114.601 ms; 50 samples | Client-observed request/response through the public load balancer |
| Legacy Hadoop Top-N | median 119.320 s; range 114.533–123.273 s; 3 jobs | Local `gcloud` submission through Dataproc completion, excluding output download |

The first JSON file contains the three measured Hadoop jobs and 50 Redis
queries made through an SSH tunnel. The public-LB JSON file contains its own 50
Redis/HTTP queries and **reuses** the first file's Hadoop timings after checking
the project, cluster, bucket, corpus hash, inverted-index output ID, Top-N size,
and exact Top-10 result rows. It did not run three new Hadoop jobs. Both runs
required the `X-ScholarMiner-Query-Source: redis` response header; neither
silently fell back to PostgreSQL. A separate direct Hadoop job succeeded after
the local `gcloud` process crashed twice when called from the benchmark script
during the attempted public rerun.

The deployment reused a prebuilt worker image that predates the local Hadoop
Streaming JAR-path fix. The cached-source Pub/Sub path was verified, but a
fresh Google Scholar scrape/index through that worker image was not. The
benchmark script now sends subprocess output to a temporary file instead of a
pipe; that mitigation passed a local `gcloud version` smoke test but was not
retested against a live Dataproc job after cleanup.

These timings establish the behavior of this disposable, pre-indexed synthetic
deployment, not production Google Scholar scraping throughput. The app timer
is **not** end-to-end HTTP latency, and Hadoop job wall-clock includes batch
orchestration. Use the scoped phrasing “minute-scale Hadoop Top-N job to
sub-10-ms Redis processing” rather than claiming sub-10-ms full requests.
