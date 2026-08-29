#!/usr/bin/env python3
"""Validate tracked Kubernetes Secret YAML without exposing payload values.

The default mode reads staged blobs from Git's index.  ``--full-tree`` reads
all tracked YAML blobs from the index for CI.  Plaintext Secret templates are
classified by content, not path: sensitive-looking keys require an exact
placeholder and every scalar is checked for credential-like shapes.

This is deliberately a guard, not a complete secret scanner, but no scalar in a
plaintext Secret escapes analysis by being awkwardly sized.  Values from
MIN_SHORT_CREDENTIAL_LENGTH up are classified, and an opaque scalar longer than
MAX_PLAINTEXT_SCALAR_LENGTH is rejected instead of skipped, so padding cannot
buy an exemption.  Sensitive key names remain hard-gated.  Do not replace that
accepted trade-off with per-file key policies: templates must stay editable
without a second policy update.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import math
import re
import subprocess
import sys
from pathlib import PurePosixPath
from typing import Any, Iterable

import yaml


BARMAN_PATH = "2-k3s/06.postgres/operator-kustomization/barman-manifest.yaml"
BARMAN_KEY = "SIDECAR_IMAGE"

PLACEHOLDER_PATTERNS = (
    re.compile(r"<[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*>"),
    re.compile(r"REPLACE_WITH_[A-Z0-9]+(?:_[A-Z0-9]+)*"),
    re.compile(r"CHANGEME"),
)
LEGACY_PLACEHOLDERS = frozenset(
    {
        "<base64-encoded-origin-cert>",
        "<base64-encoded-origin-key>",
        "<gh_PAT_renovate-k3s>",
    }
)

SENSITIVE_KEY_RE = re.compile(
    r"pass|password|secret|token|key|crt|cert|credential|auth", re.IGNORECASE
)
REFERENCE_OBJECT_KEYS = frozenset({"name", "key", "namespace", "optional"})

KNOWN_PREFIX_RE = re.compile(
    r"(?:"
    r"github_pat_|gh[pousr]_|glpat-|xox[baprs]-|"
    r"sk_(?:live|test|proj)_|AKIA|ASIA|AIza|ya29\."
    r")"
)
PRIVATE_MATERIAL_RE = re.compile(
    r"-----BEGIN (?:[A-Z0-9 ]+ )?(?:PRIVATE KEY|CERTIFICATE)-----"
)
JWT_RE = re.compile(r"^eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}$")
HEX_RUN_RE = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{32,}(?![0-9A-Fa-f])")
BASE64_RUN_RE = re.compile(
    r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{32,}={0,2}(?![A-Za-z0-9+/=])"
)
MIN_ENTROPY_LENGTH = 16
MIN_ENTROPY_DENSITY = 4.0
MIN_UNIQUE_CHARACTER_RATIO = 0.75
MIN_SHORT_CREDENTIAL_LENGTH = 8
MIN_SHORT_CREDENTIAL_CLASSES = 2
MIN_SHORT_CREDENTIAL_UNIQUE = 6
MIN_SHORT_CREDENTIAL_DENSITY = 2.5
MAX_PLAINTEXT_SCALAR_LENGTH = 2048
SOPS_ENVELOPE_RE = re.compile(
    r"^ENC\[AES256_GCM,"
    r"data:([A-Za-z0-9+/]+={0,2}),"
    r"iv:([A-Za-z0-9+/]+={0,2}),"
    r"tag:([A-Za-z0-9+/]+={0,2}),"
    r"type:(str|int|float|bool|bytes)\]$"
)
SOPS_METADATA_KEYS = frozenset({"age", "encrypted_regex", "lastmodified", "mac", "version"})
SOPS_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
SOPS_TIMESTAMP_RE = re.compile(
    r"^\d{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])T"
    r"(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\dZ$"
)
AGE_RECIPIENT_RE = re.compile(r"^age1[023456789acdefghjklmnpqrstuvwxyz]{58}$")
AGE_BEGIN = "-----BEGIN AGE ENCRYPTED FILE-----"
AGE_END = "-----END AGE ENCRYPTED FILE-----"
OCI_IMAGE_RE = re.compile(
    r"^(?=.{1,255}$)"
    r"(?:[a-z0-9]+(?:[._-][a-z0-9]+)*(?::[0-9]+)?/)?"
    r"(?:[a-z0-9]+(?:[._-][a-z0-9]+)*/)*"
    r"[a-z0-9]+(?:[._-][a-z0-9]+)*"
    r"(?::[A-Za-z0-9_][A-Za-z0-9._-]{0,127}|@sha256:[a-f0-9]{64})$"
)


class UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects duplicate mapping keys."""


def _construct_mapping(loader: UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping", node.start_mark,
                "found an unhashable key", key_node.start_mark,
            ) from exc
        if duplicate:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping", node.start_mark,
                "found a duplicate key", key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping
)


def approved_placeholder(value: Any) -> bool:
    return isinstance(value, str) and (
        value in LEGACY_PLACEHOLDERS
        or any(pattern.fullmatch(value) for pattern in PLACEHOLDER_PATTERNS)
    )


def sensitive_key(key: Any) -> bool:
    return isinstance(key, str) and SENSITIVE_KEY_RE.search(key) is not None


def structural_reference_object(key: Any, value: Any) -> bool:
    """Recognize a mapping that names another credential-bearing object.

    A scalar such as ``password-ref: ...`` is deliberately not a reference
    object and remains hard-gated.  The constrained mapping shape covers
    Kubernetes-style secretKeyRef/secretRef objects without exempting their
    surrounding document or arbitrary descendants.
    """
    if not isinstance(key, str) or not sensitive_key(key) or not isinstance(value, dict):
        return False
    compact_key = re.sub(r"[^a-z0-9]", "", key.lower())
    if not compact_key.endswith(("ref", "reference")):
        return False
    if "name" not in value or not value:
        return False
    return all(isinstance(field, str) and field in REFERENCE_OBJECT_KEYS for field in value)


def paired_reference_identifier(mapping: dict[Any, Any], key: Any) -> bool:
    """Recognize pve.yml's non-secret token identifier/value structure.

    ``token_name`` identifies which Proxmox token the separately hard-gated
    ``token_value`` authenticates.  Requiring this exact sibling pair and an
    approved value placeholder preserves the readable structured template
    without reviving a generic name/ref/reference/id suffix exemption.
    """
    return (
        key == "token_name"
        and "token_value" in mapping
        and approved_placeholder(mapping["token_value"])
    )


def shannon_entropy_density(value: str) -> float:
    """Return Shannon bits per character for a scalar value."""
    if not value:
        return 0.0
    return -sum(
        (count / len(value)) * math.log2(count / len(value))
        for count in (value.count(char) for char in set(value))
    )


def letter_digit_classes(value: str) -> int:
    """Count how many of lowercase, uppercase, and digit appear in the value."""
    return sum(
        (
            any(char.islower() for char in value),
            any(char.isupper() for char in value),
            any(char.isdigit() for char in value),
        )
    )


def short_mixed_class_credential(value: Any) -> bool:
    """Classify a short unbroken token that mixes character classes (#822).

    Deliberately narrow: a value containing a separator is a template
    identifier, a single character class is an ordinary word, and fewer than
    MIN_SHORT_CREDENTIAL_UNIQUE distinct characters is not usable credential
    material.  The documented residual is a short all-lowercase identifier that
    gains a digit inside the same unbroken token, such as an unseparated
    per-instance database user; give that a separator or a placeholder.
    """
    if not isinstance(value, str) or approved_placeholder(value):
        return False
    candidate = value.strip()
    if not MIN_SHORT_CREDENTIAL_LENGTH <= len(candidate) < MIN_ENTROPY_LENGTH:
        return False
    if not candidate.isalnum():
        return False
    return (
        letter_digit_classes(candidate) >= MIN_SHORT_CREDENTIAL_CLASSES
        and len(set(candidate)) >= MIN_SHORT_CREDENTIAL_UNIQUE
        and shannon_entropy_density(candidate) >= MIN_SHORT_CREDENTIAL_DENSITY
    )


def looks_like_credential(value: Any) -> bool:
    if not isinstance(value, str) or approved_placeholder(value):
        return False
    candidate = value.strip()
    if not candidate:
        return False
    if KNOWN_PREFIX_RE.search(candidate):
        return True
    if PRIVATE_MATERIAL_RE.search(candidate) or JWT_RE.fullmatch(candidate):
        return True
    if HEX_RUN_RE.search(candidate) or BASE64_RUN_RE.search(candidate):
        return True
    if (
        MIN_ENTROPY_LENGTH <= len(candidate) <= MAX_PLAINTEXT_SCALAR_LENGTH
        and all(char.isprintable() or char.isspace() for char in candidate)
        and len(set(candidate)) >= 10
        and len(set(candidate)) / len(candidate) >= MIN_UNIQUE_CHARACTER_RATIO
        and shannon_entropy_density(candidate) >= MIN_ENTROPY_DENSITY
    ):
        return True
    return False


EMBEDDED_PARSE_ERROR = object()


def embedded_structure(value: str) -> Any | None:
    """Safely try every string as YAML/JSON and return structured results."""
    structurally_plausible = "\n" in value or value.lstrip().startswith(("{", "["))
    try:
        parsed = yaml.load(value, Loader=UniqueKeyLoader)
    except yaml.YAMLError:
        return EMBEDDED_PARSE_ERROR if structurally_plausible else None
    return parsed if isinstance(parsed, (dict, list)) else None


def display_key(path: tuple[str, ...]) -> str:
    return ".".join(path) if path else "<document>"


def report(path: str, document: int, key_path: tuple[str, ...], reason: str) -> None:
    print(
        f"ERROR: {path}: document {document}, key {display_key(key_path)}: {reason}",
        file=sys.stderr,
    )


def scalar_leaves(value: Any) -> Iterable[Any]:
    if isinstance(value, dict):
        for child in value.values():
            yield from scalar_leaves(child)
    elif isinstance(value, list):
        for child in value:
            yield from scalar_leaves(child)
    else:
        yield value


def canonical_base64(value: str) -> bytes | None:
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return None
    if base64.b64encode(decoded).decode("ascii") != value:
        return None
    return decoded


def sops_envelope(value: Any) -> tuple[bytes, bytes, bytes, str] | None:
    """Return canonical SOPS AES-GCM envelope fields, or None if malformed."""
    if not isinstance(value, str):
        return None
    match = SOPS_ENVELOPE_RE.fullmatch(value)
    if match is None:
        return None
    data = canonical_base64(match.group(1))
    iv = canonical_base64(match.group(2))
    tag = canonical_base64(match.group(3))
    if data is None or iv is None or tag is None:
        return None
    if not data or len(iv) != 32 or len(tag) != 16:
        return None
    return data, iv, tag, match.group(4)


def valid_age_entry(entry: Any) -> bool:
    if not isinstance(entry, dict) or set(entry) != {"recipient", "enc"}:
        return False
    recipient = entry.get("recipient")
    armor = entry.get("enc")
    if not isinstance(recipient, str) or not AGE_RECIPIENT_RE.fullmatch(recipient):
        return False
    if not isinstance(armor, str):
        return False
    lines = armor.rstrip("\n").splitlines()
    if len(lines) < 3 or lines[0] != AGE_BEGIN or lines[-1] != AGE_END:
        return False
    body = "".join(lines[1:-1])
    decoded = canonical_base64(body)
    return bool(
        decoded
        and decoded.startswith(b"age-encryption.org/v1\n")
        and b"\n-> X25519 " in decoded
    )


def valid_sops_metadata(metadata: Any) -> bool:
    if not isinstance(metadata, dict) or set(metadata) != SOPS_METADATA_KEYS:
        return False
    age = metadata.get("age")
    if not isinstance(age, list) or len(age) != 1 or not valid_age_entry(age[0]):
        return False
    if metadata.get("encrypted_regex") != "^(data|stringData)$":
        return False
    lastmodified = metadata.get("lastmodified")
    version = metadata.get("version")
    if not isinstance(lastmodified, str) or not SOPS_TIMESTAMP_RE.fullmatch(lastmodified):
        return False
    if not isinstance(version, str) or not SOPS_VERSION_RE.fullmatch(version):
        return False
    mac = sops_envelope(metadata.get("mac"))
    return mac is not None and mac[3] == "str" and len(mac[0]) >= 32


def valid_sops_document(path: str, document: int, doc: dict[Any, Any]) -> bool:
    ok = True
    if not path.endswith(".enc.yaml"):
        report(path, document, ("sops",), "SOPS Secret does not use the encrypted-file suffix")
        ok = False
    if not valid_sops_metadata(doc.get("sops")):
        report(path, document, ("sops",), "encrypted Secret has malformed SOPS metadata")
        ok = False

    found_leaf = False
    for section in ("data", "stringData"):
        if section not in doc:
            continue
        payload = doc.get(section)
        if not isinstance(payload, dict) or not payload:
            report(path, document, (section,), "encrypted Secret payload is not a non-empty mapping")
            ok = False
            continue
        for leaf in scalar_leaves(payload):
            if leaf in (None, ""):
                continue
            found_leaf = True
            envelope = sops_envelope(leaf)
            if envelope is None or envelope[3] != "str":
                report(path, document, (section,), "SOPS metadata does not protect every non-empty payload leaf")
                ok = False
                break
    if not found_leaf:
        report(path, document, ("data",), "encrypted Secret has no encrypted payload leaves")
        ok = False
    return ok


def valid_barman_exception(path: str, document: int, doc: dict[Any, Any]) -> bool | None:
    """Return None when this document is not the one narrow path exception."""
    if path != BARMAN_PATH:
        return None
    data = doc.get("data")
    if not isinstance(data, dict) or BARMAN_KEY not in data:
        return None
    string_data = doc.get("stringData")
    if set(data) != {BARMAN_KEY} or string_data not in (None, {}):
        report(path, document, ("data",), "non-sensitive exception has an unapproved payload shape")
        return False

    encoded = data.get(BARMAN_KEY)
    if not isinstance(encoded, str):
        report(path, document, ("data", BARMAN_KEY), "non-sensitive exception value is malformed")
        return False
    try:
        decoded = base64.b64decode(re.sub(r"\s+", "", encoded), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        report(path, document, ("data", BARMAN_KEY), "non-sensitive exception value is malformed")
        return False
    if not OCI_IMAGE_RE.fullmatch(decoded):
        report(path, document, ("data", BARMAN_KEY), "non-sensitive exception value has an unapproved shape")
        return False
    return True


def validate_plaintext(
    path: str,
    document: int,
    value: Any,
    key_path: tuple[str, ...] = (),
    inherited_sensitive: bool = False,
    reference_fields: bool = False,
    exempt_value_paths: frozenset[tuple[str, ...]] = frozenset(),
) -> bool:
    if key_path in exempt_value_paths:
        return True

    ok = True
    if isinstance(value, dict):
        for key, child in value.items():
            key_name = str(key) if isinstance(key, (str, int, float, bool)) else "<non-string-key>"
            child_path = key_path + (key_name,)
            child_is_reference = structural_reference_object(key, child)
            identifier_is_reference = paired_reference_identifier(value, key)
            if child_is_reference or reference_fields or identifier_is_reference:
                child_sensitive = False
            else:
                child_sensitive = inherited_sensitive or sensitive_key(key)
            if not validate_plaintext(
                path,
                document,
                child,
                child_path,
                child_sensitive,
                child_is_reference,
                exempt_value_paths,
            ):
                ok = False
        return ok
    if isinstance(value, list):
        for index, child in enumerate(value):
            if not validate_plaintext(
                path,
                document,
                child,
                key_path + (f"[{index}]",),
                inherited_sensitive,
                exempt_value_paths=exempt_value_paths,
            ):
                ok = False
        return ok

    if inherited_sensitive and not approved_placeholder(value):
        report(path, document, key_path, "sensitive field is not an approved exact placeholder")
        ok = False

    if isinstance(value, str):
        nested = embedded_structure(value)
        if nested is EMBEDDED_PARSE_ERROR:
            report(path, document, key_path, "embedded structured value could not be safely parsed")
            ok = False
        elif nested is not None:
            if not validate_plaintext(
                path,
                document,
                nested,
                key_path + ("<embedded>",),
                exempt_value_paths=exempt_value_paths,
            ):
                ok = False
        elif len(value) > MAX_PLAINTEXT_SCALAR_LENGTH:
            report(path, document, key_path, "plaintext scalar exceeds the analysed length limit")
            ok = False
        elif looks_like_credential(value):
            report(path, document, key_path, "plaintext value has a credential-like shape")
            ok = False
        elif short_mixed_class_credential(value):
            report(path, document, key_path, "plaintext value has a short credential-like shape")
            ok = False
    return ok


def parse_documents(path: str, raw: bytes) -> list[Any] | None:
    try:
        text = raw.decode("utf-8")
        return list(yaml.load_all(text, Loader=UniqueKeyLoader))
    except (UnicodeDecodeError, yaml.YAMLError):
        if "charts" in PurePosixPath(path).parts:
            print(
                f"ERROR: {path}: tracked chart YAML is not exempt and could not be safely parsed",
                file=sys.stderr,
            )
        else:
            print(f"ERROR: {path}: YAML could not be safely parsed", file=sys.stderr)
        return None


def validate_file(path: str, raw: bytes) -> bool:
    documents = parse_documents(path, raw)
    if documents is None:
        return False

    ok = True
    for number, doc in enumerate(documents, start=1):
        if not isinstance(doc, dict):
            continue
        if doc.get("kind") in {"List", "SecretList"}:
            report(path, number, ("items",), "Secret container kinds are not accepted")
            ok = False
            continue
        if doc.get("kind") != "Secret":
            continue

        has_sops = "sops" in doc
        encrypted_suffix = path.endswith(".enc.yaml")
        if has_sops or encrypted_suffix:
            if not valid_sops_document(path, number, doc):
                ok = False
            continue

        exception_result = valid_barman_exception(path, number, doc)
        if exception_result is not None:
            if not exception_result:
                ok = False
            elif not validate_plaintext(
                path,
                number,
                doc,
                exempt_value_paths=frozenset({("data", BARMAN_KEY)}),
            ):
                ok = False
            continue

        if not validate_plaintext(path, number, doc):
            ok = False
    return ok


def git_paths(full_tree: bool) -> list[str] | None:
    command = ["git", "ls-files", "-z"] if full_tree else [
        "git", "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"
    ]
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
    if result.returncode != 0:
        print("ERROR: unable to enumerate tracked YAML files", file=sys.stderr)
        return None
    return [
        entry.decode("utf-8", errors="surrogateescape")
        for entry in result.stdout.split(b"\0")
        if entry and entry.lower().endswith((b".yaml", b".yml"))
    ]


def index_blob(path: str) -> bytes | None:
    result = subprocess.run(
        ["git", "show", f":{path}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode != 0:
        print(f"ERROR: {path}: unable to read tracked YAML from the Git index", file=sys.stderr)
        return None
    return result.stdout


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--full-tree",
        action="store_true",
        help="validate every tracked YAML blob instead of only staged changes",
    )
    args = parser.parse_args()

    paths = git_paths(args.full_tree)
    if paths is None:
        return 1

    ok = True
    for path in paths:
        raw = index_blob(path)
        if raw is None or not validate_file(path, raw):
            ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
