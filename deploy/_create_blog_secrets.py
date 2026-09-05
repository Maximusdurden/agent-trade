"""Create the blog secrets in Secret Manager (agenttrade-us). Safe/reversible."""
import subprocess

GCLOUD = r"C:\Users\17704\AppData\Local\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd"


def load(path):
    d = {}
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            s = line.strip()
            if s and not s.startswith("#") and "=" in s:
                k, v = s.split("=", 1)
                d[k.strip()] = v.strip().strip("\"'")
    return d


dex = load(r"Z:\python\projects\dexter-trader\.env")
agt = load(r"Z:\python\projects\agent-trade\.env")


def create_secret(name, val):
    if not val:
        print(f"{name}: <empty, skipped>")
        return
    r = subprocess.run(
        [GCLOUD, "secrets", "create", name, "--project=agenttrade-us",
         "--replication-policy=automatic"],
        capture_output=True, text=True)
    exists = "already exists" in (r.stderr or "") or "already exists" in (r.stdout or "")
    r2 = subprocess.run(
        [GCLOUD, "secrets", "versions", "add", name,
         "--project=agenttrade-us", "--data-file=-"],
        input=val, capture_output=True, text=True)
    print(f"  {name}: {'OK' if r2.returncode == 0 else 'ERR ' + r2.stderr[:200]}")


secrets = {
    "WP_USER": agt.get("WP_USER") or dex.get("WP_USER"),
    "WP_APP_PASSWORD": agt.get("WP_APP_PASSWORD") or dex.get("WP_APP_PASSWORD"),
    "GEMINI_API_KEY": agt.get("GEMINI_API_KEY") or dex.get("GEMINI_API_KEY"),
    "OPENROUTER_API_KEY": agt.get("OPENROUTER_API_KEY") or dex.get("OPENROUTER_API_KEY"),
}
for k, v in secrets.items():
    create_secret(k, v)
print("done")