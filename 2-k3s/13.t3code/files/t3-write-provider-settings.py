#!/usr/bin/env python3
"""Seeds /home/spyros/.t3/userdata/settings.json with the server-authoritative
provider layout:

- claudeAgent ("Claude (via cliproxy)"): anthropic models only, no pinned
  default model. Base URL and token are inherited from the t3 service
  environment (Q3: cliproxy for everything), so nothing sensitive lands in
  settings.json for this one.
- codex ("Codex (via cliproxy)"): the ChatGPT/Codex subscription account
  added to cliproxy via CPAMP. Unlike claudeAgent, Codex's own config format
  needs an explicit base_url (it doesn't pick up ANTHROPIC_BASE_URL the way
  the Anthropic SDK convention does), so it's set via launchArgs -c
  overrides. wire_api="responses" is load-bearing: cliproxy only answers
  this account's models (gpt-5.3-codex, codex-auto-review) on /v1/responses
  — /v1/chat/completions returns an empty message for them (verified). Only
  the env var *name* (ANTHROPIC_AUTH_TOKEN) is embedded, never the secret
  value — codex reads the actual key from its own process environment,
  inherited from t3code.service's EnvironmentFile.
- All non-anthropic, non-codex models come from the OpenCode driver — run
  setup-opencode.sh / oc-config.py for that; do not add per-model claude or
  codex instances here (T3's drivers only list known model families for
  their own account, so extra instances just duplicate the picker)."""
import json
import sys

ANTHROPIC_BASE_URL = ""
for line in open("/etc/t3code/t3code.env"):
    line = line.strip()
    if line.startswith("ANTHROPIC_BASE_URL="):
        ANTHROPIC_BASE_URL = line.split("=", 1)[1].strip("'\"")
if not ANTHROPIC_BASE_URL:
    sys.exit("ANTHROPIC_BASE_URL missing from /etc/t3code/t3code.env - refusing to write a broken codex base_url")

instances = {
    "claudeAgent": {
        "driver": "claudeAgent",
        "displayName": "Claude (via cliproxy)",
        "enabled": True,
    },
    "codex": {
        "driver": "codex",
        "displayName": "Codex (via cliproxy)",
        "enabled": True,
        "config": {
            "launchArgs": (
                '-c model_providers.cliproxy.name="cliproxy" '
                f'-c model_providers.cliproxy.base_url="{ANTHROPIC_BASE_URL}/v1" '
                '-c model_providers.cliproxy.env_key="ANTHROPIC_AUTH_TOKEN" '
                '-c model_providers.cliproxy.wire_api="responses" '
                '-c model_provider="cliproxy"'
            ),
            "customModels": ["gpt-5.3-codex", "codex-auto-review"],
        },
    },
}

out = {"providerInstances": instances}
print(json.dumps(out, indent=2)[:400])
open("/home/spyros/.t3/userdata/settings.json", "w").write(json.dumps(out, indent=2) + "\n")
print("written")