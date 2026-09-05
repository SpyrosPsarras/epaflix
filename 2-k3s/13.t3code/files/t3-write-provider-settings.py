#!/usr/bin/env python3
"""Seeds /home/spyros/.t3/userdata/settings.json with the server-authoritative
provider layout:

- claudeAgent ("Claude (via cliproxy)"): anthropic models only, no pinned
  default model.
- All non-anthropic models come from the OpenCode driver — run
  setup-opencode.sh for that; do not add per-model claude instances here
  (T3's claude driver only lists known Anthropic families, so extra
  instances just duplicate the picker).

Base URL and token are inherited from the t3 service environment (Q3:
cliproxy for everything), so nothing sensitive lands in settings.json."""
import json

instances = {
    "claudeAgent": {
        "driver": "claudeAgent",
        "displayName": "Claude (via cliproxy)",
        "enabled": True,
    }
}

out = {"providerInstances": instances}
print(json.dumps(out, indent=2)[:400])
open("/home/spyros/.t3/userdata/settings.json", "w").write(json.dumps(out, indent=2) + "\n")
print("written")