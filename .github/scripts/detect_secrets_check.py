"""Fail if detect-secrets finds a secret in a plugin folder that its .secrets.baseline doesn't already list.

Matches on (file, secret type, hashed_secret) so moved lines don't fail CI; only genuinely new hits do.
FILES.json release manifests (SHA-256 file hashes, regenerated every release) are skipped.
Usage: python detect_secrets_check.py <plugin_dir> [<plugin_dir> ...]
"""
import json
import os
import subprocess  # nosec B404 - fixed argv, no shell
import sys

failed = False
for d in sys.argv[1:]:
    base_path = os.path.join(d, ".secrets.baseline")
    if not os.path.exists(base_path):
        continue
    with open(base_path, encoding="utf-8") as fh:
        baseline = json.load(fh)
    known = {(f, s["type"], s["hashed_secret"]) for f, items in baseline["results"].items() for s in items}
    files = subprocess.run(["git", "ls-files", "-z", "."], cwd=d, check=True, capture_output=True).stdout.decode().split("\0")  # nosec B603 B607
    files = [f for f in files if f and not f.endswith("FILES.json") and f != ".secrets.baseline"]
    new = []
    for i in range(0, len(files), 200):
        out = subprocess.run(["detect-secrets", "scan", *files[i:i + 200]], cwd=d,
                             check=True, capture_output=True).stdout  # nosec B603 B607
        for f, items in json.loads(out)["results"].items():
            for s in items:
                if (f, s["type"], s["hashed_secret"]) not in known:
                    new.append(f"{d}/{f}:{s['line_number']} {s['type']}")
    print(f"{d}: {len(new)} new potential secret(s)")
    for n in new:
        print(f"::error::{n} (audit it, then add to {base_path} or mark '# pragma: allowlist secret')")
    failed |= bool(new)
sys.exit(1 if failed else 0)
