from __future__ import annotations

import json
import sys

payload = json.load(sys.stdin)
print(json.dumps({"ok": True, "output": "FAKE_RUNTIME:" + str(payload.get("message", ""))}))
