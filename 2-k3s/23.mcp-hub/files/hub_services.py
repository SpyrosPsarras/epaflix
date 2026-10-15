"""Service names shared by hub routing and agent configuration."""

import json

PATHS = {name: "/" + name for name in
         ("gmail", "searxng", "notion", "vaultwarden", "kubernetes", "drive")}
CLIENT_NAMES = {name: name for name in PATHS}
CLIENT_NAMES["kubernetes"] = "kubernetes-epaflix"
ENVIRONMENTS = ("homelab", "work")


def load_services(path, environment=None):
    with open(path) as f:
        config = json.load(f)
    return validate_services(config, environment)


def validate_services(config, environment=None):
    if not isinstance(config, dict) or set(config) != {"environment", "services"}:
        raise ValueError("service config requires environment and services")
    if config["environment"] not in ENVIRONMENTS or (
            environment is not None and config["environment"] != environment):
        raise ValueError("service config environment does not match this instance")
    services = config["services"]
    if not isinstance(services, list) or not services or any(
            not isinstance(name, str) or name not in PATHS or name == "vaultwarden" for name in services):
        raise ValueError("services must be a nonempty list of known instance service names")
    if len(set(services)) != len(services):
        raise ValueError("services must not contain duplicates")
    return config


def client_servers(config=None):
    names = PATHS if config is None else validate_services(config)["services"]
    return {CLIENT_NAMES[name]: PATHS[name] for name in names}
