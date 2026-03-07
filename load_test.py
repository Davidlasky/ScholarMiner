import requests
import concurrent.futures
import time
import sys

# Target Configuration
SEARCH_URL = "http://35.230.95.84:5000/search"
PAYLOAD = {"term": "algorithm"}

# Test Parameters
TOTAL_REQUESTS = 2000
CONCURRENCY = 200

def fire_request():
    try:
        response = requests.post(SEARCH_URL, data=PAYLOAD, timeout=5)
        return response.status_code
    except Exception:
        return 500

def main():
    print(f"Benchmarking {SEARCH_URL}")
    print(f"Concurrency Level:      {CONCURRENCY}")
    print(f"Total Requests:         {TOTAL_REQUESTS}")
    print("-" * 55)
    
    start_time = time.time()
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=CONCURRENCY) as executor:
        results = list(executor.map(lambda _: fire_request(), range(TOTAL_REQUESTS)))
        
    end_time = time.time()
    
    # Calculate metrics
    success_count = results.count(200)
    failed_count = TOTAL_REQUESTS - success_count
    duration = end_time - start_time
    throughput = TOTAL_REQUESTS / duration
    success_rate = (success_count / TOTAL_REQUESTS) * 100
    
    # Print standard benchmark report
    print("Load Test Completed.")
    print("-" * 55)
    print(f"Time taken for tests:   {duration:.3f} seconds")
    print(f"Complete requests:      {TOTAL_REQUESTS}")
    print(f"Failed requests:        {failed_count}")
    print(f"Requests per second:    {throughput:.2f} [#/sec] (mean)")
    print(f"Success rate:           {success_rate:.2f}%")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nTest interrupted by user. Exiting...")
        sys.exit(1)