#!/usr/bin/env python3
"""guru-load-env — fetch the Guru runtime configuration from AWS Secrets Manager (secret `guru-rag/env`, a JSON
object of VAR: value) using the EC2 instance role, and materialise it as /run/guru/env for systemd's EnvironmentFile.
Runs as root before guru-rag.service. Nothing is printed except the variable count. Replaces the on-disk .env."""
import json
import os
import re
import sys

import boto3

SECRET = os.environ.get("GURU_SECRET_ID", "guru-rag/env")
REGION = os.environ.get("AWS_REGION", "us-east-1")
OUT = "/run/guru/env"

client = boto3.client("secretsmanager", region_name=REGION)
raw = client.get_secret_value(SecretId=SECRET)["SecretString"]
data = json.loads(raw)
if not isinstance(data, dict) or not data:
    sys.exit("guru-load-env: secret is not a non-empty JSON object")
lines = []
for key, value in data.items():
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or value is None:
        continue
    value = str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    lines.append(f'{key}="{value}"')
os.makedirs(os.path.dirname(OUT), mode=0o750, exist_ok=True)
tmp = OUT + ".new"
with open(tmp, "w") as f:
    f.write("\n".join(lines) + "\n")
os.chmod(tmp, 0o640)
os.replace(tmp, OUT)
print(f"guru-load-env: wrote {len(lines)} variables to {OUT}")
