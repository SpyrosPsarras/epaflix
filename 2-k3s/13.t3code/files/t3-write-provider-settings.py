#!/usr/bin/env python3
"""Builds /home/spyros/.t3/userdata/settings.json with server-authoritative
provider instances: one per non-claude model in Spyros' Zed set. Each instance
differs only by ANTHROPIC_MODEL; base URL and token are inherited from the
t3 service environment (Q3: cliproxy for everything)."""
import json

MODELS = [
    ("glm-flash", "GLM Flash", "or-glm-5.3-flash"),
    ("glm-5-3", "GLM 5.3", "or-glm-5.3"),
    ("gpt-terra", "GPT Terra", "gpt-5.6-terra"),
    ("gpt-sol", "GPT Sol", "gpt-5.6-sol"),
    ("gpt-luna", "GPT Luna", "gpt-5.6-luna"),
    ("gemini-flash", "Gemini Flash", "or-gemini-3.7-flash"),
    ("deepseek-flash", "DeepSeek Flash", "or-deepseek-v4-flash"),
    ("deepseek-pro", "DeepSeek Pro", "or-deepseek-v4-pro"),
    ("qwen-max", "Qwen Max", "or-qwen3.8-max"),
    ("qwen-27b", "Qwen 3.8 27B", "or-qwen3.8-27b"),
    ("minimax-m3", "MiniMax M3", "or-minimax-m3"),
]

instances = {
    "claudeAgent": {
        "driver": "claudeAgent",
        "displayName": "Claude (via cliproxy)",
        "enabled": True,
    }
}
for slug, label, model in MODELS:
    instances[slug] = {
        "driver": "claudeAgent",
        "displayName": label,
        "enabled": True,
        "environment": [
            {"name": "ANTHROPIC_MODEL", "value": model, "sensitive": False},
        ],
    }

out = {"providerInstances": instances}
print(json.dumps(out, indent=2)[:400])
open("/home/spyros/.t3/userdata/settings.json", "w").write(json.dumps(out, indent=2) + "\n")
print("written")