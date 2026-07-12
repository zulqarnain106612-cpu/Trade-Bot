import json
import sys


try:
    with open("resolved.json") as f:
        data = json.load(f)
except Exception as e:
    print(f"Warning: could not read resolved.json: {e}", file=sys.stderr)
    open("requirements-resolved.txt", "w").close()
    sys.exit(0)

pkgs = [f"{p['metadata']['name']}=={p['metadata']['version']}" for p in data.get("install", [])]

with open("requirements-resolved.txt", "w") as f:
    f.write("\n".join(pkgs))

print(f"Resolved {len(pkgs)} packages into requirements-resolved.txt")
