#!/usr/bin/env python3
"""Validate an Authentik blueprint payload nested inside a Secret's stringData.

ONE COPY, TWO CONSUMERS.  This file is the single on-disk source of truth for
the check.  ``2-k3s/07.authentik-deployment/kustomization.yaml`` builds it into
the ``authentik-blueprint-check-script`` ConfigMap that the in-cluster CronJob
mounts, and ``.github/hooks/test-check-authentik-blueprint.sh`` runs the same
file against synthetic fixtures in CI.  Do not copy it anywhere.

WHERE IT RUNS AND WHY (owner decision, 2026-08-22).  It runs in the cluster, on
a schedule, NOT in the commit path.  A pre-commit version would have to decrypt
``authentik-iac-blueprint.enc.yaml``, which means sops, which means the age key,
which on this workstation lives behind KeePassXC - a personal password manager
on the owner's own machines.  A repo-wide commit hook must not depend on it.
The live Secret ``app-authentik/authentik-iac-blueprint`` is already decrypted
in the cluster, so the CronJob needs no age key and no sops: only RBAC to *get*
that one Secret.  The trade-off is real and is not hidden: the hook would have
refused a broken blueprint BEFORE it reached main, the CronJob only detects it
AFTER.  It is still far earlier than the status quo, where the sole signal is
``failed to parse blueprint`` in a worker log nothing reads (#883, #876, #940).

Reads a Secret document on stdin (``--path`` is diagnostics only) and checks
every payload key that ends ``.yaml``/``.yml``, in three layers.  Both Secret
shapes are accepted: ``stringData`` (plaintext, what the fixtures and the
decrypted ``.enc.yaml`` look like) and ``data`` (base64, what
``kubectl get secret -o json`` returns, which is the CronJob's input).

Layer 1, syntax (#876).  The blueprint file is valid YAML; the thing that broke
was a second YAML document nested inside ``stringData``, which nothing parsed.
So load the payload with the ten Authentik tags registered and fail on any
YAML error.  A tag outside those ten also fails, by design: a new Authentik tag
has to be added here deliberately rather than being silently ignored.

Layer 2, semantic references (#940).  The payload that failed every apply for
two days parsed CLEANLY - a ``!KeyOf`` in a ``present`` entry pointed at an
entry declared ``state: absent``, which ``KeyOf.resolve`` cannot resolve, so
the importer aborted the whole run.  Layer 1 provably cannot see that.  So resolve
``!KeyOf``/``!Find`` against the entries declared in the same payload, and
reject the #940 corollary shape: ``state: absent`` plus ``attrs`` (attrs are
ignored on a delete, and a tag inside them silently skips the delete).

Layer 3, the importer dry-run (#1103, decision 2026-09-15).  Only the
importer knows the model registry and the per-model schemas, and it has a
supported dry-run: ``Importer.validate()`` runs the full apply inside a
transaction that always rolls back.  Authentik exposes that as
``POST /api/v3/managed/blueprints/validate/`` (upstream merged 2026-09-14,
first in the release AFTER 2026.8.2).  When ``--validate-url`` is set, every
payload that survives layers 1-2 is POSTed there with a bearer token;
``success: false`` fails the run.  The response also carries
``imported``, asserted false: this endpoint must never apply.

Why the running server and not an offline importer (the literal ask of
#1103): this payload resolves ~132 of its 151 ``!Find`` lookups against
objects that exist only in the live database (the sibling index matches 19).
A dry-run against a scratch database would fail on nearly all of them and
cry wolf on valid blueprints; a nightly dump/restore variant is a restore
pipeline wrapped around unsupported internals.  The server already runs the
right image, the right database and the right context - zero new
infrastructure.

Why not what 2026.8.2 ships: ``POST .../blueprints/import/`` VALIDATES AND
THEN APPLIES on success, the opposite of a dry-run, and padding a disabled
BlueprintInstance through the create API would churn instance rows nightly
in throwaway code.  Both rejected.  Until the image carries the validate
endpoint it answers 405 (the router falls through to the instance detail
route, which takes no POST), which this layer reports as INACTIVE in the
summary and exits 0 - a stated gap, not a silent one - and it flips itself
on at the next image MINOR bump (renovate bumps chart+image in lockstep).
A 404, by contrast, is a violation: an endpointless authentik says 405, so
404 means the URL itself is wrong.

The bearer token is the payload's own ``authentik_core.token`` entry
``ak-blueprint-check-token`` - a service account this same blueprint
provisions, holding add/change/delete on every model the blueprint manages,
which is exactly what the endpoint's ``check_blueprint_perms`` demands of any
caller.  It is extracted from the Secret the CronJob already reads: no second
Secret, no extra k8s RBAC.  COROLLARY: a new model added to this blueprint
must also add that model's three permissions to the ``ak-blueprint-check``
role, or layer 3 fails with 403 naming the missing model.  A 403
``Token invalid/expired`` means the payload's declared credential is not
applied yet or has drifted from live authentik - the run fails until the
worker applies the payload, which is the desync signal layers 1-2 cannot
produce.

LEAK TRAP, layer 3 edition.  The response's ``logs`` carry full
``attributes`` (entry dicts, serializer state), which can embed Secret
values.  Only the ``event`` text is read, and it passes through a redactor
built from the payload's own scalars: ids, model paths and desired states
stay printable so violations stay locatable, everything else of length >= 8
is replaced with ``<redacted>``.  The raw response body is never printed.
Connection failures, 403 and unexpected statuses fail the run: the server
being down at check time is signal, and a check that silently skips itself
is a false green.

DISCOVERY IS CONVENTION, ON PURPOSE (#1104, decision 2026-09-15).  This
checker finds its work twice by convention: the CI suite runs against
``*blueprint*.enc.yaml`` files, and the CronJob reads the one Secret name
``authentik-iac-blueprint``.  A second blueprint Secret under a different
name would be unchecked by both.  Mechanical enforcement was evaluated and
rejected.  Git-side detection means parsing decrypted payloads in a commit
hook, which means the age key in the commit path - the coupling the owner
rejected in #1098.  Cluster-side detection means ``list`` on Secrets, and
``resourceNames`` does not constrain ``list``: the least-privilege Role would
widen from one named Secret to every Secret in the namespace, handed to a
checker deliberately built never to echo Secret values.  Accepted: exactly
one blueprint exists and both paths cover it by name.  Revisit when a second
blueprint Secret appears (a new ``*blueprint*.enc.yaml`` or a second entry
under helm ``blueprints.secrets``), when the existing Secret is renamed or
moved, or when someone other than this repo authors blueprints - then wiring
is two lines (ksops generator + the CronJob's Role resourceNames) and a
filename-convention lint in ``check_sops_encrypted.py`` is the cheap
enforcement to add.

NOT covered, stated plainly: the apply-time behaviour of authentik's
instance management - a disabled or errored BlueprintInstance, a drifted
mount path, entries silently skipped by ``!If`` conditions.  Validation sees
the same skips apply would; instance status needs the worker's own view.

LEAK TRAP, measured: ``str(yaml.YAMLError)`` embeds the offending SOURCE LINE,
and here that line is decrypted Secret content, so printing it would echo
Secret material into the terminal and the retained transcript (the #602 class).
This module prints ``e.problem`` plus ``problem_mark.line``/``.column`` only -
never ``str(e)``, never a scalar value.  Violations are located by
``entries[i]:<id or model>`` plus the structural path, which is key names and
indices.  Keep it that way.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import sys
import urllib.error
import urllib.request
import uuid
from typing import Any

import yaml


# The authentik_core.token entry this checker presents to the validate
# endpoint. The blueprint provisions it; see the layer 3 paragraph.
CHECK_TOKEN_IDENTIFIER = "ak-blueprint-check-token"


INERT_TAGS = (
    "!Context",
    "!Format",
    "!If",
    "!Env",
    "!Enumerate",
    "!Value",
    "!Index",
    "!Condition",
)


class KeyOf:
    """A ``!KeyOf <id>`` reference to another entry in the same payload."""

    def __init__(self, target: Any) -> None:
        self.target = target


class Find:
    """A ``!Find [model, [attr, value]]`` lookup."""

    def __init__(self, value: Any) -> None:
        self.value = value


class BlueprintLoader(yaml.SafeLoader):
    """SafeLoader that knows the Authentik blueprint tags and nothing else."""


def _construct_key_of(loader: yaml.Loader, node: yaml.Node) -> KeyOf:
    if isinstance(node, yaml.ScalarNode):
        return KeyOf(node.value)
    return KeyOf(None)  # wrong shape; layer 2 reports it as unresolvable


def _construct_find(loader: yaml.Loader, node: yaml.Node) -> Find:
    if isinstance(node, yaml.SequenceNode):
        return Find(loader.construct_sequence(node, deep=True))
    return Find(None)  # wrong shape; layer 2 reports it


def _construct_inert(loader: yaml.Loader, node: yaml.Node) -> None:
    return None


BlueprintLoader.add_constructor("!KeyOf", _construct_key_of)
BlueprintLoader.add_constructor("!Find", _construct_find)
for _tag in INERT_TAGS:
    BlueprintLoader.add_constructor(_tag, _construct_inert)


def yaml_error_summary(error: yaml.YAMLError) -> str:
    """Describe a YAML error without echoing the offending source line.

    ``str(error)`` includes the source line, which here is decrypted Secret
    content.  ``problem`` is parser vocabulary ("expected <block end>, but
    found '-'"), and the mark gives the operator the coordinates to look at.
    """
    problem = getattr(error, "problem", None) or "unparseable YAML"
    mark = getattr(error, "problem_mark", None)
    if mark is not None:
        return f"{problem} (line {mark.line + 1}, column {mark.column + 1})"
    return str(problem)


def identifier_key(model: Any, attr: Any, value: Any) -> str:
    """Stable comparison key for a (model, attr, value) triple.

    ``repr`` keeps unhashable values (a nested list or mapping) comparable
    without putting the value anywhere near the output.
    """
    return f"{model!r}|{attr!r}|{value!r}"


def entry_label(index: int, entry: Any) -> str:
    if isinstance(entry, dict):
        name = entry.get("id") or entry.get("model") or "?"
    else:
        name = "?"
    return f"entries[{index}]:{name}"


class PayloadCheck:
    """Layer 2 over one parsed payload."""

    def __init__(self, blueprint: dict) -> None:
        self.entries = blueprint["entries"]
        self.violations: list[str] = []
        self.key_of_checked = 0
        self.find_checked = 0
        self.find_sibling_matched = 0

        self.ids: set[str] = set()
        self.absent_ids: set[str] = set()
        self.identifier_index: dict[str, list[int]] = {}

        for index, entry in enumerate(self.entries):
            if not isinstance(entry, dict):
                self.violations.append(
                    f"{entry_label(index, entry)}: entry is not a mapping"
                )
                continue
            entry_id = entry.get("id")
            if isinstance(entry_id, str):
                self.ids.add(entry_id)
                if entry.get("state") == "absent":
                    self.absent_ids.add(entry_id)
            identifiers = entry.get("identifiers")
            if isinstance(identifiers, dict):
                for attr, value in identifiers.items():
                    key = identifier_key(entry.get("model"), attr, value)
                    self.identifier_index.setdefault(key, []).append(index)

    def run(self) -> None:
        for index, entry in enumerate(self.entries):
            if not isinstance(entry, dict):
                continue
            label = entry_label(index, entry)
            if entry.get("state") == "absent" and "attrs" in entry:
                self.violations.append(
                    f"{label}: state:absent entry also carries attrs. Attrs are "
                    "ignored on a delete and a tag inside them silently skips "
                    "the delete (#940 - the objects stayed live for 2 days with "
                    "no error anywhere). An absent entry carries identifiers only."
                )
            self.walk(entry, label, "")

    def walk(self, node: Any, label: str, path: str) -> None:
        if isinstance(node, KeyOf):
            self.check_key_of(node, label, path)
        elif isinstance(node, Find):
            self.check_find(node, label, path)
        elif isinstance(node, dict):
            for key, value in node.items():
                self.walk(value, label, f"{path}.{key}")
        elif isinstance(node, list):
            for i, value in enumerate(node):
                self.walk(value, label, f"{path}[{i}]")

    def check_key_of(self, node: KeyOf, label: str, path: str) -> None:
        self.key_of_checked += 1
        target = node.target
        if not isinstance(target, str) or not target:
            self.violations.append(f"{label}{path}: !KeyOf is not a scalar id")
            return
        if target not in self.ids:
            self.violations.append(
                f"{label}{path}: !KeyOf matches no entry id declared in this "
                "payload (target withheld: it is Secret content)"
            )
            return
        if target in self.absent_ids:
            self.violations.append(
                f"{label}{path}: !KeyOf points at a state:absent entry, which "
                "KeyOf.resolve cannot resolve, so the importer aborts the whole "
                "apply (#940)"
            )

    def check_find(self, node: Find, label: str, path: str) -> None:
        self.find_checked += 1
        value = node.value
        if (
            not isinstance(value, list)
            or len(value) != 2
            or not isinstance(value[1], list)
            or len(value[1]) != 2
        ):
            self.violations.append(
                f"{label}{path}: !Find is not shaped [model, [attr, value]]"
            )
            return
        model, (attr, wanted) = value[0], value[1]
        key = identifier_key(model, attr, wanted)
        matches = self.identifier_index.get(key, [])
        if not matches:
            return  # a lookup of something Authentik ships or a human created
        self.find_sibling_matched += 1
        for sibling in matches:
            entry = self.entries[sibling]
            if isinstance(entry, dict) and entry.get("state") == "absent":
                self.violations.append(
                    f"{label}{path}: !Find matches sibling "
                    f"{entry_label(sibling, entry)}, which is declared "
                    "state:absent, so the lookup cannot resolve at apply (#940)"
                )

    def summary(self) -> str:
        return (
            f"entries={len(self.entries)} ids={len(self.ids)} "
            f"absent={len(self.absent_ids)} "
            f"!KeyOf refs checked={self.key_of_checked} "
            f"!Find refs checked={self.find_checked} "
            f"(sibling-matched={self.find_sibling_matched})"
        )


def check_payload(payload: str, where: str) -> tuple[list[str], str | None]:
    """Return (violations, summary) for one stringData payload."""
    try:
        blueprint = yaml.load(payload, Loader=BlueprintLoader)
    except yaml.YAMLError as error:
        return [f"{where}: {yaml_error_summary(error)}"], None

    if not isinstance(blueprint, dict):
        return [f"{where}: payload is not a mapping"], None
    if "version" not in blueprint:
        return [f"{where}: payload has no `version` key"], None
    entries = blueprint.get("entries")
    if not isinstance(entries, list) or not entries:
        return [f"{where}: payload has no non-empty `entries` list"], None

    check = PayloadCheck(blueprint)
    check.run()
    return [f"{where}: {v}" for v in check.violations], f"{where}: {check.summary()}"


def extract_check_token(blueprint: dict) -> str | None:
    """The bearer token this checker presents to the validate endpoint.

    The payload provisions its own validator credential: an
    ``authentik_core.token`` entry identified ``ak-blueprint-check-token``
    (see the layer 3 paragraph).  Reading it from the Secret the CronJob
    already fetched means no second Secret and no extra RBAC.  Missing is
    not an error: layer 3 reports itself inactive instead.
    """
    for entry in blueprint.get("entries", []):
        if not isinstance(entry, dict) or entry.get("model") != "authentik_core.token":
            continue
        identifiers = entry.get("identifiers")
        if not isinstance(identifiers, dict):
            continue
        if identifiers.get("identifier") != CHECK_TOKEN_IDENTIFIER:
            continue
        attrs = entry.get("attrs")
        if isinstance(attrs, dict) and isinstance(attrs.get("key"), str) and attrs["key"]:
            return attrs["key"]
    return None


def redaction_strings(blueprint: dict) -> list[str]:
    """Payload scalars that must never reach checker output, longest first.

    Ids, model paths and desired states are what the existing layers already
    print, so they are excluded and server-side log lines stay locatable.
    Every other string of length >= 8 - attr values, identifier values, the
    check token itself - is replaced with ``<redacted>``.
    """
    structural: set[str] = set()
    values: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
        elif isinstance(node, str) and len(node) >= 8:
            values.add(node)

    for entry in blueprint.get("entries", []):
        if not isinstance(entry, dict):
            continue
        for field in ("id", "model", "state"):
            if isinstance(entry.get(field), str):
                structural.add(entry[field])
        walk(entry.get("attrs"))
        walk(entry.get("identifiers"))
    walk(blueprint.get("metadata"))
    return sorted(values - structural, key=len, reverse=True)


def redact(text: str, needles: list[str]) -> str:
    for needle in needles:
        if needle in text:
            text = text.replace(needle, "<redacted>")
    return text


def post_blueprint(url: str, token: str, payload: str, filename: str) -> tuple[int, str]:
    """POST one payload to the validate endpoint. Returns (status, body)."""
    boundary = f"blueprint-check-{uuid.uuid4().hex}"
    part = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        "Content-Type: application/x-yaml\r\n"
        "\r\n"
    ).encode("utf-8")
    body = part + payload.encode("utf-8") + f"\r\n--{boundary}--\r\n".encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", "replace")


def server_dry_run(
    payload: str, url: str, where: str, filename: str
) -> tuple[list[str], str | None]:
    """Layer 3: the importer invoked without applying, inside authentik.

    Returns (violations, note); the note is a summary line for the clean and
    inactive paths, None when there is nothing to report.
    """
    try:
        blueprint = yaml.load(payload, Loader=BlueprintLoader)
    except yaml.YAMLError:
        return [], None  # layers 1-2 already rejected this payload; never POSTed
    if not isinstance(blueprint, dict):
        return [], None

    token = extract_check_token(blueprint)
    if token is None:
        return [], (
            f"{where}: server dry-run inactive: this payload declares no "
            f"{CHECK_TOKEN_IDENTIFIER} entry, so there is no credential to "
            "present (the blueprint creates it on its first apply)"
        )

    try:
        status, body = post_blueprint(url, token, payload, filename)
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        reason = getattr(error, "reason", None) or error
        return [f"{where}: server dry-run: cannot reach the authentik API "
                f"at {url}: {reason}"], None

    if status == 405:
        return [], (
            f"{where}: server dry-run inactive: this authentik version has no "
            "/managed/blueprints/validate endpoint (upstream merged it "
            "2026-09-14, first in the release after 2026.8.2); it flips on at "
            "the next image MINOR bump"
        )

    needles = redaction_strings(blueprint)
    excerpt = redact(body, needles)[:400].replace("\n", " ")

    if status == 403:
        return [f"{where}: server dry-run: authentik refused the check "
                f"credential (403): {excerpt}"], None
    if status != 200:
        return [f"{where}: server dry-run: unexpected HTTP {status} from the "
                f"authentik API: {excerpt}"], None

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return [f"{where}: server dry-run: the authentik API answered 200 "
                f"with a non-JSON body: {excerpt}"], None
    if not isinstance(data, dict):
        return [f"{where}: server dry-run: the authentik API answered 200 "
                f"with a non-object body: {excerpt}"], None

    if data.get("imported") is True:
        # Would mean the endpoint applied the blueprint: the one thing this
        # layer must never do. Asserted, not assumed.
        return [f"{where}: server dry-run: response says the blueprint was "
                "imported; a validate endpoint must never apply"], None

    logs = data.get("logs")
    if data.get("success") is not True:
        if not isinstance(logs, list) or not logs:
            return [f"{where}: server dry-run: the authentik API rejected the "
                    f"payload without usable detail: {excerpt}"], None
        violations = []
        for log in logs:
            if isinstance(log, dict) and isinstance(log.get("event"), str):
                text = redact(log["event"], needles)
            else:
                text = excerpt
            violations.append(
                f"{where}: server dry-run: the importer rejected this payload "
                f"against the live server: {text}"
            )
        return violations, None

    count = len(logs) if isinstance(logs, list) else 0
    return [], f"{where}: server dry-run: passed ({count} log events)"


def secret_payloads(document: dict) -> list[tuple[str, str, str]]:
    """Yield (field, key, payload) for every YAML-suffixed key of a Secret.

    ``stringData`` is plaintext; ``data`` is base64, which is the only shape the
    kube API ever returns, so the CronJob depends on this branch.  A ``data``
    value that is not valid base64 or not UTF-8 is skipped here and surfaces as
    the "no payload to validate" refusal below rather than as a traceback that
    could print bytes.
    """
    found: list[tuple[str, str, str]] = []
    string_data = document.get("stringData")
    if isinstance(string_data, dict):
        for key, payload in sorted(string_data.items()):
            if key.endswith((".yaml", ".yml")) and isinstance(payload, str):
                found.append(("stringData", key, payload))
    data = document.get("data")
    if isinstance(data, dict):
        for key, encoded in sorted(data.items()):
            if not key.endswith((".yaml", ".yml")) or not isinstance(encoded, str):
                continue
            try:
                payload = base64.b64decode(encoded, validate=True).decode("utf-8")
            except (binascii.Error, UnicodeDecodeError, ValueError):
                continue
            found.append(("data", key, payload))
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--path",
        default="<stdin>",
        help="path of the encrypted file, for diagnostics only",
    )
    parser.add_argument(
        "--validate-url",
        default="",
        help="authentik blueprints validate endpoint for layer 3, the importer "
        "dry-run (e.g. http://authentik-server.app-authentik.svc.cluster.local"
        "/api/v3/managed/blueprints/validate/). Empty disables layer 3, which "
        "is what the CI fixtures want.",
    )
    args = parser.parse_args()

    raw = sys.stdin.buffer.read().decode("utf-8")
    try:
        documents = list(yaml.safe_load_all(raw))
    except yaml.YAMLError as error:
        print(
            f"ERROR: {args.path}: decrypted document does not parse: "
            f"{yaml_error_summary(error)}",
            file=sys.stderr,
        )
        return 1

    violations: list[str] = []
    summaries: list[str] = []
    payloads = 0
    for doc_index, document in enumerate(documents):
        if not isinstance(document, dict):
            continue
        for field, key, payload in secret_payloads(document):
            payloads += 1
            where = f"{args.path}[doc {doc_index}].{field}[{key}]"
            found, summary = check_payload(payload, where)
            violations.extend(found)
            if summary:
                summaries.append(summary)
            # Layer 3 runs only on payloads layers 1-2 accepted: a payload
            # that fails locally is rejected without asking the server.
            if not found and args.validate_url:
                found, note = server_dry_run(payload, args.validate_url, where, key)
                violations.extend(found)
                if note:
                    summaries.append(note)

    if payloads == 0:
        print(
            f"ERROR: {args.path}: no stringData/data key ending .yaml/.yml, so "
            "there is no blueprint payload to validate. The check would pass "
            "vacuously; refusing instead.",
            file=sys.stderr,
        )
        return 1

    if violations:
        print(f"ERROR: {args.path}: blueprint payload rejected:", file=sys.stderr)
        for violation in violations:
            print(f"  {violation}", file=sys.stderr)
        return 1

    for summary in summaries:
        print(f"blueprint OK: {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
