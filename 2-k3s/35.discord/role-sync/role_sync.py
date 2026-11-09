#!/usr/bin/env python3
import json
import os
import sys
import time
import urllib.request
from urllib.error import HTTPError
from urllib.parse import urlencode

DISCORD_API = "https://discord.com/api/v10"
DISCORD_PAGE = 1000


class NoLinks(Exception):
    pass


def plan(group_members, links, members, role_ids):
    if not links:
        raise NoLinks("Authentik returned no Discord links; refusing to remove every mapped role")
    allowed = {
        group: {links[pk] for pk in group_members.get(group, ()) if pk in links}
        for group in role_ids
    }
    adds = sorted(
        (user, role)
        for group, role in role_ids.items()
        for user in allowed[group]
        if user in members and role not in members[user]
    )
    removes = sorted(
        (user, role)
        for user, roles in members.items()
        for group, role in role_ids.items()
        if role in roles and user not in allowed[group]
    )
    linked = set(links.values())
    unlinked = sorted(user for user in members if user not in linked)
    return adds, removes, unlinked


def request(method, url, headers):
    data = None if method == "GET" else b""
    while True:
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data, headers, method=method), timeout=30) as response:
                body = response.read()
                return json.loads(body) if body else None
        except HTTPError as error:
            if error.code != 429:
                raise
            time.sleep(retry_after(error))


def retry_after(error):
    try:
        return float(json.loads(error.read() or b"{}").get("retry_after", 1))
    except ValueError:
        return float(error.headers.get("Retry-After", 1))


def authentik_list(base, headers, path, params):
    results, page = [], 1
    while page:
        data = request("GET", f"{base}/api/v3/{path}?{urlencode({**params, 'page': page})}", headers)
        results += data["results"]
        page = data["pagination"]["next"]
    return results


def group_member_pks(base, headers, group):
    matches = [
        found
        for found in authentik_list(base, headers, "core/groups/", {"name": group, "include_users": "true"})
        if found["name"] == group
    ]
    if len(matches) != 1:
        raise SystemExit(json.dumps({"event": "group_not_found", "group": group}))
    return active_member_pks(matches[0])


def active_member_pks(group):
    return {str(user["pk"]) for user in group["users_obj"] if user["is_active"]}


def guild_members(guild, headers):
    members, after = {}, "0"
    while True:
        page = request("GET", f"{DISCORD_API}/guilds/{guild}/members?limit={DISCORD_PAGE}&after={after}", headers)
        for member in page:
            if not member["user"].get("bot"):
                members[member["user"]["id"]] = set(member["roles"])
        if len(page) < DISCORD_PAGE:
            return members
        after = max((member["user"]["id"] for member in page), key=int)


def main():
    base = os.environ["AUTHENTIK_URL"].rstrip("/")
    guild = os.environ["DISCORD_GUILD_ID"]
    role_ids = json.loads(os.environ["ROLE_MAP"])
    ignored = {user.strip() for user in os.environ.get("IGNORE_USER_IDS", "").split(",") if user.strip()}
    authentik_headers = {"Authorization": f"Bearer {os.environ['AUTHENTIK_TOKEN']}", "Accept": "application/json"}
    discord_headers = {
        "Authorization": f"Bot {os.environ['DISCORD_TOKEN']}",
        "User-Agent": "DiscordBot (https://github.com/SpyrosPsarras/epaflix, 1)",
    }

    group_members = {group: group_member_pks(base, authentik_headers, group) for group in role_ids}
    links = {
        str(connection["user"]): connection["identifier"]
        for connection in authentik_list(base, authentik_headers, "sources/user_connections/oauth/", {"source__slug": "discord"})
    }
    members = {user: roles for user, roles in guild_members(guild, discord_headers).items() if user not in ignored}

    adds, removes, unlinked = plan(group_members, links, members, role_ids)
    for event, method, changes in [("role_added", "PUT", adds), ("role_removed", "DELETE", removes)]:
        for user, role in changes:
            request(method, f"{DISCORD_API}/guilds/{guild}/members/{user}/roles/{role}", discord_headers)
            print(json.dumps({"event": event, "discord_user_id": user, "role_id": role}), flush=True)
    for user in unlinked:
        print(json.dumps({"event": "unlinked_member", "discord_user_id": user}), flush=True)


if __name__ == "__main__":
    try:
        main()
    except HTTPError as error:
        print(json.dumps({"event": "http_error", "status": error.code, "url": error.url}), flush=True)
        sys.exit(1)
    except NoLinks as error:
        print(json.dumps({"event": "aborted", "reason": str(error)}), flush=True)
        sys.exit(1)
    except OSError as error:
        print(json.dumps({"event": "network_error", "reason": str(getattr(error, "reason", error))}), flush=True)
        sys.exit(1)
