#!/usr/bin/env python3
"""Writes the OpenCode config for the t3code guest: cliproxy as an
openai-compatible provider with Spyros' Zed model set. The API key comes
from the T3 env file (never printed)."""
import json

env = {}
for line in open("/etc/t3code/t3code.env"):
    line = line.strip()
    if line.startswith("ANTHROPIC_AUTH_TOKEN="):
        v = line.split("=", 1)[1]
        env["token"] = v[1:-1] if v.startswith("'") and v.endswith("'") else v
    if line.startswith("ANTHROPIC_BASE_URL="):
        env["base"] = line.split("=", 1)[1].strip("'\"")

models = {
    "or-glm-5.3-flash": "GLM 5.3 Flash",
    "or-glm-5.3": "GLM 5.3",
    "gpt-5.6-terra": "GPT Terra",
    "gpt-5.6-sol": "GPT Sol",
    "gpt-5.6-luna": "GPT Luna",
    "or-gemini-3.7-flash": "Gemini 3.7 Flash",
    "or-deepseek-v4-flash": "DeepSeek V4 Flash",
    "or-deepseek-v4-pro": "DeepSeek V4 Pro",
    "or-qwen3.8-max": "Qwen 3.8 Max",
    "or-qwen3.8-27b": "Qwen 3.8 27B",
    "or-minimax-m3": "MiniMax M3",
    "claude-opus-5": "Claude Opus 5",
    "claude-sonnet-5": "Claude Sonnet 5",
    "claude-fable-5": "Claude Fable 5",
    "claude-fable-5-1": "Claude Fable 5.1",
}
cfg = {
    "$schema": "https://opencode.ai/config.json",
    "provider": {
        "cliproxy": {
            "npm": "@ai-sdk/openai-compatible",
            "name": "Cliproxy",
            "options": {
                "baseURL": "https://cliproxy.epaflix.com/v1",
                "apiKey": env["token"],
            },
            "models": {mid: {"name": name} for mid, name in models.items()},
        }
    },
    "model": "cliproxy/or-glm-5.3-flash",
}
import os
os.makedirs("/home/spyros/.config/opencode", exist_ok=True)
json.dump(cfg, open("/home/spyros/.config/opencode/opencode.json", "w"), indent=2)
print("opencode.json written with", len(models), "models")