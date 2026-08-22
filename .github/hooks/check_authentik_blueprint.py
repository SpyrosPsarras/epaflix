#!/usr/bin/env python3
"""Validate an Authentik blueprint payload nested inside a Secret's stringData.

Reads a DECRYPTED Secret document on stdin (``--path`` is diagnostics only) and
checks every ``stringData`` key that ends ``.yaml``/``.yml``, in two layers:

Layer 1, syntax (#876).  The blueprint file is valid YAML; the thing that broke
was a second YAML document nested inside ``stringData``, which nothing parsed.
So load the payload with the ten Authentik tags registered and fail on any
YAML error.  A tag outside those ten also fails, by design: a new Authentik tag
has to be added here deliberately rather than being silently ignored.

Layer 2, semantic references (#940).  The payload that failed every apply for
two days parsed CLEANLY - a ``!KeyOf`` in a ``present`` entry pointed at an
entry declared ``state: absent``, which ``KeyOf.resolve`` cannot resolve, so the
importer aborted the whole run.  Layer 1 provably cannot see that.  So resolve
``!KeyOf``/``!Find`` against the entries declared in the same payload, and
reject the #940 corollary shape: ``state: absent`` plus ``attrs`` (attrs are
ignored on a delete, and a tag inside them silently skips the delete).

NOT covered, stated plainly: every other semantic error - wrong model paths,
bad attr names, permissions Authentik would reject at apply.  Only Authentik's
own importer knows those.

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
import sys
from typing import Any

import yaml


# The ten tags #883 lists.  !KeyOf and !Find become marker objects because
# layer 2 has to resolve them; the rest are no-ops, exactly as the manual check
# used to verify #880 did.
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
        # (model, attr, value) -> entry indices whose `identifiers` declare it.
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
                # `state: created` is resolvable, not absent: a skipped
                # `created` entry still populates `entry._state`, so !KeyOf
                # resolves off it (#1040). Only `absent` is unresolvable.
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
        # Never str(error): it embeds the offending source line, which is
        # decrypted Secret content.
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--path",
        default="<stdin>",
        help="path of the encrypted file, for diagnostics only",
    )
    args = parser.parse_args()

    raw = sys.stdin.buffer.read().decode("utf-8")
    try:
        documents = list(yaml.safe_load_all(raw))
    except yaml.YAMLError as error:
        # The decrypted Secret itself, so same rule: problem and mark only.
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
        string_data = document.get("stringData")
        if not isinstance(string_data, dict):
            continue
        for key, payload in sorted(string_data.items()):
            if not key.endswith((".yaml", ".yml")) or not isinstance(payload, str):
                continue
            payloads += 1
            where = f"{args.path}[doc {doc_index}].stringData[{key}]"
            found, summary = check_payload(payload, where)
            violations.extend(found)
            if summary:
                summaries.append(summary)

    if payloads == 0:
        print(
            f"ERROR: {args.path}: no stringData key ending .yaml/.yml, so there "
            "is no blueprint payload to validate. The check would pass "
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
