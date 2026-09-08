"""真实 Fleet MCP 探活；不将 relay 存活当作 candidate 就绪。"""

import json
import urllib.request

with urllib.request.urlopen("http://127.0.0.1:15611/health", timeout=6) as response:
    result = json.load(response)
    if not result.get("ready"):
        raise SystemExit(1)
