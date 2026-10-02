#!/usr/bin/env python3
"""Compare ScholarMiner's historical Hadoop Top-N path with its Redis path.

The web application reports its own Top-N processing time in the result page.
This script records that value separately from client-observed HTTP latency.
The legacy path runs the repository's Top-N Hadoop Streaming mapper/reducer
against the *same* inverted-index output produced by the submitted task.
"""

import argparse
import json
import re
import statistics
import subprocess
import tempfile
import time
import uuid
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlparse
from urllib.request import HTTPCookieProcessor, Request, build_opener


class TableCells(HTMLParser):
    def __init__(self):
        super().__init__()
        self.cells = []
        self._in_cell = False
        self._parts = []

    def handle_starttag(self, tag, attrs):
        if tag == "td":
            self._in_cell = True
            self._parts = []

    def handle_data(self, data):
        if self._in_cell:
            self._parts.append(data)

    def handle_endtag(self, tag):
        if tag == "td" and self._in_cell:
            self.cells.append(unescape("".join(self._parts).strip()))
            self._in_cell = False


def request_text(opener, url, data=None, timeout=20):
    body = urlencode(data).encode("utf-8") if data is not None else None
    request = Request(url, data=body)
    started = time.perf_counter()
    with opener.open(request, timeout=timeout) as response:
        text = response.read().decode("utf-8", errors="replace")
        status = response.status
        final_url = response.geturl()
        headers = dict(response.headers)
    elapsed_ms = (time.perf_counter() - started) * 1000
    if status != 200:
        raise RuntimeError(f"HTTP {status} from {final_url}")
    return final_url, text, elapsed_ms, headers


def submit_and_wait(opener, base_url, scholar_url, timeout_seconds):
    final_url, _, _, _ = request_text(
        opener, urljoin(base_url, "/load"), {"scholar_url": scholar_url}
    )
    match = re.fullmatch(r"/tasks/([0-9a-f-]{36})", urlparse(final_url).path)
    if not match:
        raise RuntimeError(f"Index submission did not reach a task page: {final_url}")
    task_id = match.group(1)
    print(f"Index task: {task_id}", flush=True)

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        _, response, _, _ = request_text(
            opener, urljoin(base_url, f"/api/tasks/{task_id}")
        )
        task = json.loads(response)
        status = task.get("status")
        if status == "COMPLETE":
            return task_id, task
        if status == "FAILED":
            raise RuntimeError(f"Index task failed: {task.get('message', '')}")
        print(f"Index task status: {status}", flush=True)
        time.sleep(5)
    raise TimeoutError(f"Index task did not finish within {timeout_seconds}s")


def attach_completed_task(opener, base_url, task_id):
    """Initialize the web session from an already-completed benchmark task."""
    request_text(opener, urljoin(base_url, f"/tasks/{task_id}"))
    _, response, _, _ = request_text(
        opener, urljoin(base_url, f"/api/tasks/{task_id}")
    )
    task = json.loads(response)
    if task.get("status") != "COMPLETE":
        raise RuntimeError(f"Seeded task {task_id} is not complete: {task.get('status')}")
    return task


def query_topn(opener, base_url, n):
    final_url, html, http_ms, headers = request_text(
        opener, urljoin(base_url, "/topn"), {"n": n}
    )
    if urlparse(final_url).path != "/topn" or "Top-N Frequent Terms" not in html:
        raise RuntimeError(f"Top-N request was redirected or returned the wrong page: {final_url}")
    source = next(
        (value for name, value in headers.items() if name.lower() == "x-scholarminer-query-source"),
        None,
    )
    if source != "redis":
        raise RuntimeError(f"Expected a Redis Top-N hit, got query source {source!r}")

    match = re.search(r"Your search was executed in\s*<strong>([\d.]+) ms</strong>", html)
    if not match:
        raise RuntimeError("Top-N result page did not contain a numeric processing time")

    parser = TableCells()
    parser.feed(html)
    if len(parser.cells) < 2 or len(parser.cells) % 2:
        raise RuntimeError("Top-N result page did not contain parseable term-frequency rows")
    rows = [(parser.cells[i], int(parser.cells[i + 1])) for i in range(0, len(parser.cells), 2)]
    return float(match.group(1)), http_ms, rows


def run_command(args):
    with tempfile.TemporaryFile(mode="w+t") as output:
        completed = subprocess.run(
            args, text=True, stdout=output, stderr=subprocess.STDOUT, check=False
        )
        output.seek(0)
        result = output.read()
    if completed.returncode:
        raise RuntimeError(
            f"Command failed ({completed.returncode}): {' '.join(args)}\n{result[-2500:]}"
        )
    return result


def run_hadoop_baseline(project, region, cluster, bucket, index_output_task_id, run_number):
    root = f"gs://{bucket}"
    output = f"{root}/benchmark/topn/{uuid.uuid4().hex}"
    files = ",".join(
        [f"{root}/mapreduce/topn_mapper.py", f"{root}/mapreduce/topn_reducer.py"]
    )
    command = [
        "gcloud", "dataproc", "jobs", "submit", "hadoop",
        f"--project={project}", f"--region={region}", f"--cluster={cluster}",
        "--jar=file:///usr/lib/hadoop/hadoop-streaming.jar", "--",
        "-files", files,
        "-mapper", "python3 topn_mapper.py",
        "-reducer", "python3 topn_reducer.py",
        "-input", f"{root}/output/inverted_index/{index_output_task_id}/part-*",
        "-output", output,
    ]
    print(f"Legacy Hadoop Top-N run {run_number}...", flush=True)
    started = time.perf_counter()
    run_command(command)
    elapsed_ms = (time.perf_counter() - started) * 1000
    result = run_command(["gcloud", "storage", "cat", f"{output}/part-*"])
    frequencies = {}
    for line in result.splitlines():
        if not line.strip():
            continue
        term, frequency = line.rsplit("\t", 1)
        frequencies[term] = int(frequency)
    if not frequencies:
        raise RuntimeError("Hadoop Top-N job produced no term frequencies")
    return elapsed_ms, frequencies


def percentile(values, fraction):
    ordered = sorted(values)
    rank = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * fraction + 0.999999)))
    return ordered[rank]


def summary(values):
    return {
        "count": len(values),
        "min_ms": round(min(values), 3),
        "median_ms": round(statistics.median(values), 3),
        "p95_ms": round(percentile(values, 0.95), 3),
        "max_ms": round(max(values), 3),
        "samples_ms": [round(value, 3) for value in values],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--scholar-url", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--region", default="us-west1")
    parser.add_argument("--cluster", default="scholarminer-dataproc")
    parser.add_argument("--bucket", required=True)
    parser.add_argument(
        "--index-output-task-id",
        help="Index-output ID to use when the corpus was seeded independently of the web task",
    )
    parser.add_argument(
        "--seeded", action="store_true",
        help="Benchmark a pre-seeded index without submitting a Google Scholar scrape task",
    )
    parser.add_argument("--completed-task-id", help="Completed web task UUID for seeded mode")
    parser.add_argument("--indexed-terms", type=int)
    parser.add_argument("--dataset-label", default="live-indexed")
    parser.add_argument("--corpus-papers", type=int)
    parser.add_argument("--corpus-sha256")
    parser.add_argument("--n", type=int, default=10)
    parser.add_argument("--warmups", type=int, default=5)
    parser.add_argument("--samples", type=int, default=50)
    parser.add_argument("--baseline-runs", type=int, default=3)
    parser.add_argument(
        "--baseline-reference", type=Path,
        help="Reuse a prior Hadoop baseline for the exact same index (requires --baseline-runs 0)",
    )
    parser.add_argument("--task-timeout", type=int, default=1800)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if min(args.n, args.samples) < 1 or args.warmups < 0 or args.baseline_runs < 0:
        parser.error("n and samples must be positive; warmups and baseline-runs cannot be negative")
    if (args.baseline_runs == 0) != bool(args.baseline_reference):
        parser.error("use --baseline-runs 0 together with --baseline-reference")
    if args.seeded and (not args.index_output_task_id or not args.completed_task_id):
        parser.error("--seeded requires --index-output-task-id and --completed-task-id")

    base_url = args.base_url.rstrip("/") + "/"
    opener = build_opener(HTTPCookieProcessor(CookieJar()))
    if args.seeded:
        task_id = args.completed_task_id
        task = attach_completed_task(opener, base_url, task_id)
    else:
        task_id, task = submit_and_wait(opener, base_url, args.scholar_url, args.task_timeout)
    index_output_task_id = args.index_output_task_id or task_id
    for _ in range(args.warmups):
        query_topn(opener, base_url, args.n)

    processing_times = []
    http_times = []
    expected_rows = None
    for index in range(args.samples):
        processing_ms, http_ms, rows = query_topn(opener, base_url, args.n)
        if expected_rows is not None and rows != expected_rows:
            raise RuntimeError("Top-N results changed during the benchmark")
        expected_rows = rows
        processing_times.append(processing_ms)
        http_times.append(http_ms)
        if (index + 1) % 10 == 0:
            print(f"Redis Top-N samples: {index + 1}/{args.samples}", flush=True)

    if args.baseline_reference:
        reference = json.loads(args.baseline_reference.read_text())
        for name, value in (
            ("project", args.project), ("region", args.region),
            ("cluster", args.cluster), ("bucket", args.bucket),
            ("index_output_task_id", index_output_task_id),
            ("corpus_sha256", args.corpus_sha256), ("top_n", args.n),
        ):
            if reference.get(name) != value:
                raise RuntimeError(f"Hadoop baseline reference mismatch: {name}")
        if [list(row) for row in expected_rows] != reference.get("top_n_results"):
            raise RuntimeError("Redis Top-N results differ from the baseline reference")
        baseline_summary = reference["hadoop_job_wall_clock"]
    else:
        baseline_times = []
        for index in range(args.baseline_runs):
            elapsed_ms, frequencies = run_hadoop_baseline(
                args.project, args.region, args.cluster, args.bucket, index_output_task_id, index + 1
            )
            for term, frequency in expected_rows:
                if frequencies.get(term) != frequency:
                    raise RuntimeError(
                        f"Result mismatch for {term}: Redis={frequency}, Hadoop={frequencies.get(term)}"
                    )
            expected_scores = sorted(frequencies.values(), reverse=True)[: len(expected_rows)]
            observed_scores = sorted((frequency for _, frequency in expected_rows), reverse=True)
            if observed_scores != expected_scores:
                raise RuntimeError("Redis Top-N scores differ from the Hadoop Top-N scores")
            baseline_times.append(elapsed_ms)
        baseline_summary = summary(baseline_times)

    record = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "project": args.project,
        "region": args.region,
        "cluster": args.cluster,
        "bucket": args.bucket,
        "task_id": task_id,
        "index_output_task_id": index_output_task_id,
        "scholar_url": args.scholar_url,
        "dataset_label": args.dataset_label,
        "corpus_sha256": args.corpus_sha256,
        "papers_indexed": args.corpus_papers or (task.get("result") or {}).get("papers_indexed"),
        "indexed_terms": args.indexed_terms or (task.get("result") or {}).get("num_terms"),
        "top_n": args.n,
        "warmups": args.warmups,
        "redis_processing": summary(processing_times),
        "redis_http": summary(http_times),
        "hadoop_job_wall_clock": baseline_summary,
        "hadoop_baseline_reference": str(args.baseline_reference) if args.baseline_reference else None,
        "top_n_results": expected_rows,
        "methodology": (
            "Redis processing time is the web application's displayed pre-render Top-N timer; "
            "HTTP time is client-observed full response time; Hadoop time includes gcloud job "
            "submission and completion but excludes result download. All paths use the same index output. "
            "Seeded mode uses a deterministic pre-indexed corpus and does not test web task submission or scraping. "
            "A baseline reference reuses earlier Hadoop timings for the verified identical index."
        ),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({key: record[key] for key in (
        "papers_indexed", "indexed_terms", "redis_processing", "redis_http", "hadoop_job_wall_clock"
    )}, indent=2))
    print(f"Saved benchmark record to {args.output}")


if __name__ == "__main__":
    main()
