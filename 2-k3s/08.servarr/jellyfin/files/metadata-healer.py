import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ["JELLYFIN_URL"].rstrip("/")
API_KEY = os.environ["JELLYFIN_API_KEY"]
PAGE_SIZE = 1000
RECHECK_SECONDS = 180


def jellyfin(path, method="GET", body=None, **params):
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"X-Emby-Token": API_KEY, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = response.read()
        return json.loads(payload) if payload else {}


def unmatched_items():
    found = []
    for item_type in ("Episode", "Movie"):
        start = 0
        while True:
            page = jellyfin(
                "/Items",
                IncludeItemTypes=item_type,
                Recursive="true",
                Fields="ProviderIds,SeriesName",
                Limit=PAGE_SIZE,
                StartIndex=start,
            )
            items = page.get("Items", [])
            found += [
                item
                for item in items
                if item.get("Type") == item_type and not item.get("ProviderIds")
            ]
            start += PAGE_SIZE
            if start >= page.get("TotalRecordCount", 0) or not items:
                break
    return found


def item_label(item):
    return item.get("SeriesName") or item.get("Name") or item.get("Id", "unknown")


def report(items):
    grouped = {}
    for item in items:
        grouped[item_label(item)] = grouped.get(item_label(item), 0) + 1
    for label, count in sorted(grouped.items(), key=lambda kv: -kv[1]):
        print(f"  {count:3d}  {label}")


def refresh(item_id, name):
    try:
        jellyfin(
            f"/Items/{item_id}/Refresh",
            method="POST",
            MetadataRefreshMode="FullRefresh",
            ImageRefreshMode="FullRefresh",
            ReplaceAllMetadata="true",
            ReplaceAllImages="false",
        )
        print(f"refreshed: {name}")
    except urllib.error.HTTPError as error:
        print(f"refresh failed ({error.code}): {name}")
    except urllib.error.URLError as error:
        print(f"refresh failed ({error.reason}): {name}")


def main():
    broken = unmatched_items()
    print(f"unmatched items: {len(broken)}")
    report(broken)
    if not broken:
        print("library is clean, nothing to do")
        return

    for item in broken:
        refresh(item["Id"], item_label(item))
        time.sleep(0.3)

    print(f"waiting {RECHECK_SECONDS}s for refreshes to settle")
    time.sleep(RECHECK_SECONDS)
    remaining = unmatched_items()
    print(f"still unmatched after refresh: {len(remaining)}")
    report(remaining)
    if remaining:
        print(
            "items that stay unmatched across runs have no provider entry yet or hit a "
            "stale TVDB plugin cache (restart the Jellyfin app to clear the cache)"
        )


if __name__ == "__main__":
    main()
