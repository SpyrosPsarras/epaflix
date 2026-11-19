import argparse
import json
import threading
import time
from pathlib import Path, PurePosixPath
from urllib.error import URLError
from urllib.request import Request, urlopen

HERE = Path(__file__).resolve().parent


def seed(base):
    def crud(collection, mode, doc_id, obj):
        body = json.dumps({"data": {"collection": collection, "mode": mode, "docID": doc_id, "obj": obj}}).encode()
        request = Request(base + "/api/v2/cruddb", body, {"Content-Type": "application/json"})
        with urlopen(request, timeout=10) as response:
            return response.read()

    def upsert(collection, desired, on_create):
        doc_id = desired["_id"]
        current = crud(collection, "getById", doc_id, {})
        if current:
            if json.loads(current)["_id"] != doc_id:
                raise ValueError("unexpected Tdarr document ID")
            crud(collection, "update", doc_id, desired)
        else:
            crud(collection, "insert", doc_id, {**on_create, **desired})

    flow = json.loads((HERE / "flow-lg-c1.json").read_text())
    libraries = json.loads((HERE / "library-seed.json").read_text())
    collection = "LibrarySettingsJSONDB"
    managed_folders = {PurePosixPath(lib["folder"]): lib["_id"] for lib in libraries["libraries"]}
    for existing in json.loads(crud(collection, "getAll", "", {})):
        folder = PurePosixPath(existing.get("folder") or "")
        if folder in managed_folders and existing["_id"] != managed_folders[folder]:
            raise SystemExit("Tdarr already has an unmanaged library for a seeded folder")
    upsert("FlowsJSONDB", flow, {})
    for library in libraries["libraries"]:
        upsert(collection, {**libraries["settings"], **library}, libraries["onCreate"])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("base_url")
    parser.add_argument("--ready-file", type=Path)
    args = parser.parse_args()
    if args.ready_file:
        for _ in range(60):
            try:
                with urlopen(args.base_url + "/api/v2/status", timeout=2) as response:
                    if json.load(response)["status"] == "good":
                        break
            except (URLError, TimeoutError):
                pass
            time.sleep(2)
        else:
            raise SystemExit("Tdarr did not become ready")
    try:
        seed(args.base_url)
    except (URLError, TimeoutError, ValueError, KeyError):
        raise SystemExit("Tdarr configuration seed failed") from None
    print("Tdarr flow and libraries seeded", flush=True)
    if args.ready_file:
        args.ready_file.touch()
        threading.Event().wait()


if __name__ == "__main__":
    main()
