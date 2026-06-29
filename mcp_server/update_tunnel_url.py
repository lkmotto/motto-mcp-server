"""
Startup script: reads cloudflared log for latest tunnel URL
and updates Cloudflare KV namespace so the Worker proxy stays current.
Run this after cloudflared starts (service dependency or scheduled task).
"""

import re
import sys
import time
import urllib.request

TOKEN = "cfut_wyodQESvqVnRo0XHStJkCm9MvRf2Z7Sd62SpAgTy0a43d8ab"
ACCOUNT_ID = "69c676f35299031461fcc0f4b52aa102"
NAMESPACE_ID = "d28ec54bb1c44fad9e7d038490c9d2d1"
LOG_PATH = r"C:\Users\lkmot\ona-mcp-server\tunnel-service-err.log"
MAX_RETRIES = 30
KV_PROPAGATION_WAIT_SECONDS = 8


def extract_tunnel_url(log_content):
    match = re.search(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com", log_content)
    return match.group(0) if match else None


def get_tunnel_url_from_metrics(ports=(20241, 20242, 20243)):
    """Read live tunnel URL from cloudflared metrics endpoint."""
    for port in ports:
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/metrics")
            resp = urllib.request.urlopen(req, timeout=2)
            body = resp.read().decode()
            match = re.search(
                r'cloudflared_tunnel_user_hostnames_counts\{userHostname="(https://[^"]+trycloudflare\.com)"\}',
                body,
            )
            if match:
                return match.group(1)
        except Exception:
            continue
    return None


def update_kv(url):
    body = url.encode()
    req = urllib.request.Request(
        f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_ID}/storage/kv/namespaces/{NAMESPACE_ID}/values/tunnel-url",
        data=body,
        headers={"Authorization": f"Bearer {TOKEN}"},
        method="PUT",
    )
    resp = urllib.request.urlopen(req)
    return resp.getcode() == 200


def main():
    # Prefer live metrics endpoint (reflects currently running tunnel)
    metrics_url = get_tunnel_url_from_metrics()
    if metrics_url:
        print(f"Found tunnel URL via metrics: {metrics_url}")
        if update_kv(metrics_url):
            print(f"KV updated successfully: {metrics_url}")
            print(f"Waiting {KV_PROPAGATION_WAIT_SECONDS}s for KV propagation...")
            time.sleep(KV_PROPAGATION_WAIT_SECONDS)
            return
        else:
            print("KV update via metrics URL failed, falling back to log file...")

    for i in range(MAX_RETRIES):
        try:
            with open(LOG_PATH) as f:
                content = f.read()
            url = extract_tunnel_url(content)
            if url:
                print(f"Found tunnel URL in log: {url}")
                if update_kv(url):
                    print(f"KV updated successfully: {url}")
                    print(f"Waiting {KV_PROPAGATION_WAIT_SECONDS}s for KV propagation...")
                    time.sleep(KV_PROPAGATION_WAIT_SECONDS)
                    return
                else:
                    print("KV update failed")
            else:
                print(f"Attempt {i+1}/{MAX_RETRIES}: Tunnel URL not found in logs yet...")
        except FileNotFoundError:
            print(f"Attempt {i+1}/{MAX_RETRIES}: Log file not found yet...")
        except Exception as e:
            print(f"Attempt {i+1}/{MAX_RETRIES}: Error: {e}")
        time.sleep(2)
    print("Failed to update tunnel URL after max retries")
    sys.exit(1)


if __name__ == "__main__":
    main()
