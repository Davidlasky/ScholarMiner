#!/usr/bin/env python3
"""
High-Performance Load Testing Utility for ScholarMiner
Measures throughput, error distributions, and latency percentiles (P50, P90, P99).
"""

import argparse
import requests
import concurrent.futures
import time
import sys
import statistics
from collections import Counter

def create_arg_parser():
    parser = argparse.ArgumentParser(description="ScholarMiner Concurrency Load Tester")
    parser.add_argument("--url", required=True, help="Target Search URL (e.g., http://IP:5000/search)")
    parser.add_argument("--term", default="algorithm", help="Search term payload")
    parser.add_argument("--requests", type=int, default=2000, help="Total number of requests")
    parser.add_argument("--concurrency", type=int, default=200, help="Number of concurrent workers")
    parser.add_argument("--timeout", type=int, default=5, help="Request timeout in seconds")
    return parser

def fire_request(session, url, payload, timeout):
    """Executes a single HTTP request using a pooled connection."""
    start_time = time.time()
    try:
        # Use session to reuse TCP connections, dramatically improving client throughput
        response = session.post(url, data=payload, timeout=timeout)
        duration = time.time() - start_time
        return response.status_code, duration
    except requests.exceptions.Timeout:
        return "TIMEOUT", time.time() - start_time
    except requests.exceptions.ConnectionError:
        return "CONN_ERROR", time.time() - start_time
    except Exception as e:
        return "ERROR", time.time() - start_time

def print_statistics(results, duration, total_requests):
    """Aggregates and prints professional telemetry metrics."""
    status_codes = [res[0] for res in results]
    latencies = [res[1] for res in results if isinstance(res[0], int) and res[0] == 200]

    status_counts = Counter(status_codes)
    success_count = status_counts.get(200, 0)
    throughput = total_requests / duration
    success_rate = (success_count / total_requests) * 100

    print("\n" + "=" * 55)
    print("BENCHMARK REPORT")
    print("=" * 55)
    print(f"Total Time Taken:        {duration:.3f} seconds")
    print(f"Total Requests:          {total_requests}")
    print(f"Throughput:              {throughput:.2f} req/sec")
    print(f"Success Rate:            {success_rate:.2f}%")
    
    print("\n--- Latency Distribution (Successful Requests) ---")
    if latencies:
        # Convert to milliseconds
        latencies_ms = [l * 1000 for l in latencies]
        print(f"Min Latency:             {min(latencies_ms):.2f} ms")
        print(f"Mean Latency:            {statistics.mean(latencies_ms):.2f} ms")
        print(f"Median (P50):            {statistics.median(latencies_ms):.2f} ms")
        print(f"P90 Latency:             {statistics.quantiles(latencies_ms, n=10)[8]:.2f} ms")
        print(f"P99 Latency:             {statistics.quantiles(latencies_ms, n=100)[98]:.2f} ms")
        print(f"Max Latency:             {max(latencies_ms):.2f} ms")
    else:
        print("No successful requests to measure latency.")

    print("\n--- Error Distribution ---")
    for code, count in status_counts.items():
        if code != 200:
            print(f"Status {code}: {count} occurrences")
    if success_count == total_requests:
        print("Zero errors detected.")
    print("=" * 55)

def main():
    parser = create_arg_parser()
    args = parser.parse_args()

    print(f"Initiating benchmark against: {args.url}")
    print(f"Concurrency: {args.concurrency} | Total Requests: {args.requests}")
    print("Warming up connections...")

    payload = {"search_term": args.term} # Updated to match your Flask app form variable name
    
    # Thread-safe connection pooling
    session = requests.Session()
    
    # Optional: Configure HTTP adapter for higher pool maxsize to prevent connection blocking
    adapter = requests.adapters.HTTPAdapter(pool_connections=args.concurrency, pool_maxsize=args.concurrency)
    session.mount('http://', adapter)
    session.mount('https://', adapter)

    start_time = time.time()
    
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as executor:
            # Submit all tasks and gather results
            futures = [executor.submit(fire_request, session, args.url, payload, args.timeout) for _ in range(args.requests)]
            results = [future.result() for future in concurrent.futures.as_completed(futures)]
    except KeyboardInterrupt:
        print("\n[!] Load test aborted by user. Exiting gracefully...")
        sys.exit(1)

    end_time = time.time()
    total_duration = end_time - start_time

    print_statistics(results, total_duration, args.requests)

if __name__ == "__main__":
    main()