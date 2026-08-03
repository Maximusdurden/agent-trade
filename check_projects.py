import subprocess
import json

projects_regions = [
    ("agenttrade-us", "us-central1"),
    ("treatmotivated-capital", "us-central1"),
]

for proj, region in projects_regions:
    print(f"\n=== Runner Job: {proj} / {region} ===")
    try:
        result = subprocess.run(
            ["gcloud", "run", "jobs", "describe", "agent-trade-job",
             "--project", proj, "--region", region, "--format=json"],
            capture_output=True, text=True
        )
        data = json.loads(result.stdout)
        containers = data["spec"]["template"]["spec"]["template"]["spec"]["containers"]
        for c in containers:
            for e in c.get("env", []):
                val = e.get("value", "(not set)")
                print(f"  {e['name']}={val}")
    except Exception as ex:
        print(f"  Error: {ex}")

print("\n\n=== Dashboard Services ===")
for proj in ["agenttrade-us", "treatmotivated-capital"]:
    print(f"\n--- Dashboard in {proj} ---")
    try:
        result = subprocess.run(
            ["gcloud", "run", "services", "describe", "agenttrade-dashboard",
             "--project", proj, "--region", "us-east1", "--format=json"],
            capture_output=True, text=True
        )
        data = json.loads(result.stdout)
        containers = data["spec"]["template"]["spec"]["containers"]
        for c in containers:
            for e in c.get("env", []):
                print(f"  ENV: {e['name']}={e.get('value','')}")
            print(f"  Image: {c['image']}")
    except Exception as ex:
        print(f"  Error: {ex}")