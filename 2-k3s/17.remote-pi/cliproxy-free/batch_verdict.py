"""Verdict for one lingarr batch response, plus the fixed synthetic batch.

Pure, no network. Run `python3 batch_verdict.py --selftest` offline; batch-repro.sh
pipes this file into the probe pod and calls verdict() on each live response.
"""
import json, re, sys

LINES = ["Where were you last night?", "I told you, I was at work.", "Don't lie to me.", "Nanahoshi",
         "We have to leave before sunrise.", "Is that blood on your sleeve?", "It's paint. Relax.", "You never could lie well.",
         "Get in the car.", "I'm not going anywhere with you.", "Then stay and explain it to the police.", "Fine. Drive.",
         "Where are we going?", "Somewhere they won't look.", "You're scaring me.", "Good. You should be scared.",
         "What did you do?", "What I had to.", "That's not an answer.", "It's the only one you're getting.",
         "Pull over.", "Not yet.", "I said pull over!", "We're almost there.", "Almost where?"]
BATCH = [{"position": i + 50, "line": l} for i, l in enumerate(LINES)]
WANT = [b["position"] for b in BATCH]
# The one bare proper noun: it may legitimately come back untranslated, but only as itself.
NAME_ONLY = {53: "Nanahoshi"}

GREEK = re.compile(r"[\u0370-\u03FF\u1F00-\u1FFF]")


def verdict(content):
    try:
        j = json.loads(content)
    except Exception:
        return "not-json"
    if not isinstance(j, dict) or not isinstance(j.get("translations"), list):
        return "no-translations-wrapper"
    items = j["translations"]
    pos = [t.get("position") for t in items if isinstance(t, dict)]
    if sorted(pos) != WANT:
        return (f"positions-mismatch(unique={len(set(pos))}, dup={len(pos) - len(set(pos))}, "
                f"missing={sorted(set(WANT) - set(pos))[:5]}, extra={sorted(set(pos) - set(WANT))[:5]})")
    bad = []
    for t in items:
        line, p = t.get("line"), t["position"]
        if not isinstance(line, str) or not line.strip():
            bad.append(f"{p}:empty"); continue
        if p in NAME_ONLY and line.strip() == NAME_ONLY[p]:
            continue
        alpha = [c for c in line if c.isalpha()]
        greek = sum(1 for c in alpha if GREEK.match(c))  # letters only; U+037E etc. are punctuation
        # Greek must dominate every other script, not just Latin.
        if greek == 0 or greek * 2 <= len(alpha):
            bad.append(f"{p}:not-greek({greek}/{len(alpha)}):{line[:25]!r}")
    return "OK" if not bad else "bad-lines:" + ",".join(bad[:4])


def _selftest():
    def wrap(items): return json.dumps({"translations": items})
    good = [{"position": p, "line": "Πού ήσουν χθες;"} for p in WANT]
    cases = [
        ("all greek", wrap(good), "OK"),
        ("name kept as itself", wrap([{"position": p, "line": "Nanahoshi" if p == 53 else "Καλά."} for p in WANT]), "OK"),
        ("name replaced by other english", wrap([{"position": p, "line": "Hello there" if p == 53 else "Καλά."} for p in WANT]), "bad-lines:53:"),
        ("russian with token greek", wrap([{"position": p, "line": "Жизнь прекрасна α"} for p in WANT]), "bad-lines:50:not-greek(1/"),
        ("arabic with token greek", wrap([{"position": p, "line": "أين كنت ليلة أمس؟ α"} for p in WANT]), "bad-lines:50:not-greek(1/"),
        ("english", wrap([{"position": p, "line": "Where were you?"} for p in WANT]), "bad-lines:50:not-greek(0/"),
        ("latin padded with greek punctuation", wrap([{"position": p, "line": "No\u037e\u037e"} for p in WANT]), "bad-lines:50:not-greek(0/2)"),
        ("empty line", wrap([{"position": p, "line": "" if p == 60 else "Καλά."} for p in WANT]), "bad-lines:60:empty"),
        ("missing position", wrap(good[:-1]), "positions-mismatch(unique=24, dup=0, missing=[74]"),
        ("duplicate position", wrap(good[:-1] + [good[0]]), "positions-mismatch(unique=24, dup=1, missing=[74]"),
        ("extra position", wrap(good + [{"position": 99, "line": "Καλά."}]), "positions-mismatch(unique=26, dup=0, missing=[], extra=[99]"),
        ("bare array", json.dumps(good), "no-translations-wrapper"),
        ("prose", "Καλημέρα, πώς είστε;", "not-json"),
        ("safety classifier", "User Safety: unsafe", "not-json"),
    ]
    fails = 0
    for name, content, want_prefix in cases:
        got = verdict(content)
        ok = got.startswith(want_prefix)
        fails += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {name:32s} -> {got[:90]}")
    print(f"selftest: {len(cases) - fails}/{len(cases)} passed")
    sys.exit(1 if fails else 0)


# No usage/exit here on purpose: batch-repro.sh appends its runner to this file
# and pipes the whole thing to python3 -, so the module must fall through.
if __name__ == "__main__" and "--selftest" in sys.argv:
    _selftest()
