import urllib.request
import json
import time

def test_fetch():
    url = "http://localhost:8080/api/status"
    print(f"Fetching {url}...")
    start_time = time.time()
    try:
        with urllib.request.urlopen(url, timeout=15) as res:
            elapsed = time.time() - start_time
            print(f"Success! Status: {res.status} | Time taken: {elapsed:.2f} seconds")
            data = json.loads(res.read().decode('utf-8'))
            print("is_mock:", data.get("is_mock"))
            print("account cash:", data.get("account", {}).get("cash"))
            print("account equity:", data.get("account", {}).get("equity"))
            print("positions:", list(data.get("positions", {}).keys()))
    except Exception as e:
        elapsed = time.time() - start_time
        print(f"Failed after {elapsed:.2f} seconds: {e}")

if __name__ == "__main__":
    test_fetch()
