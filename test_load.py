import time
import json
import urllib.request
import urllib.parse
import urllib.error
import concurrent.futures

BASE_URL = "http://127.0.0.1:8000/api/v1/time"

LOCATIONS = [
    "Finland",
    "Malta",
    "New York",
    "Berlin",
    "finlan", # fuzzy match test
    "berl", # fuzzy match test
    "Turkey",
    "brazil",
    "India",
    "Japan",
    "A" * 5000  # Malicious heavy CPU constraint payload
]

def map_location(loc: str):
    url = f"{BASE_URL}?location={urllib.parse.quote(loc)}"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req) as response:
            status = response.status
            body = json.loads(response.read().decode('utf-8'))
            return loc, status, body
    except urllib.error.HTTPError as e:
        body = json.loads(e.read().decode('utf-8'))
        return loc, e.code, body

def run_tests():
    print("--- 1. Testing Correctness ---")
    for loc in LOCATIONS:
        original, status, response = map_location(loc)
        print(f"[{status}] {original} -> {response.get('resolved_timezone', response) if isinstance(response, dict) else response}")
        
    print("\n--- 2. Testing Concurrency (100 req/sec load) ---")
    start_time = time.time()
    
    # 10s of requests, let's do 100 requests concurrently
    payloads = LOCATIONS * 10 
    
    success_count = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=50) as executor:
        results = list(executor.map(map_location, payloads))
        
    for res in results:
        if res[1] == 200:
            success_count += 1
            
    end_time = time.time()
    elapsed = end_time - start_time
    
    print(f"Executed {len(payloads)} requests in {elapsed:.4f} seconds")
    print(f"Throughput: {len(payloads) / elapsed:.2f} requests/sec")
    print(f"Successful responses: {success_count}/{len(payloads)}")

if __name__ == "__main__":
    run_tests()
