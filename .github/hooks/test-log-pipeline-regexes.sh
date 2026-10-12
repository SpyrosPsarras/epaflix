#!/usr/bin/env bash
# Synthetic fixtures for the committed log regexes: alloy's credential
# self-mask/count/redact stages and the ServarrErrorSpike LogQL pipeline.
# Runs them with Go's regexp, the RE2 engine Alloy and Loki use. Go ships on
# the ubuntu-latest runner image; nothing is downloaded. Not a full Alloy or
# Loki run: stage.replace is a copy of Alloy v1.20.1 getReplacedEntry, and
# stage.match is reduced to its |~ line filter.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
command -v go >/dev/null || { echo "go not found on PATH; this fixture needs it" >&2; exit 1; }
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

cat >"$tmp/main.go" <<'GO'
package main

import (
	"fmt"
	"os"
	"regexp"
	"strconv"
	"strings"
)

var fails int

func check(name string, ok bool, detail string) {
	if ok {
		fmt.Println("ok   " + name)
		return
	}
	fails++
	fmt.Printf("FAIL %s: %q\n", name, detail)
}

// Alloy v1.20.1 stages/replace.go getReplacedEntry: only captured groups are
// replaced; uncaptured parts of a match (a terminator) stay in the line.
func replace(re *regexp.Regexp, line, with string) string {
	out, prev := "", 0
	for _, m := range re.FindAllStringSubmatchIndex(line, -1) {
		for i := 2; i < len(m); i += 2 {
			if m[i] != -1 && (prev == 0 || prev <= m[i]) {
				out += line[prev:m[i]] + with
				prev = m[i+1]
			}
		}
	}
	return out + line[prev:]
}

type stage struct {
	re   *regexp.Regexp
	with string // "" means stage.match: count the line
}

func main() {
	read := func(p string) string {
		b, err := os.ReadFile(os.Args[1] + "/2-k3s/10.observability/" + p)
		if err != nil {
			panic(err)
		}
		return string(b)
	}
	alloy, rules := read("alloy-values.yaml"), read("alertmanager-config/loki-log-alerts.yaml")

	// Stages of loki.process "pods" in file order.
	var stages []stage
	expr := regexp.MustCompile("^\\s*expression\\s*=\\s*`(.*)`$")
	repl := regexp.MustCompile(`^\s*replace\s*=\s*"(.*)"$`)
	sel := regexp.MustCompile("^\\s*selector\\s*=\\s*`\\{namespace=~\"\\.\\+\"\\} \\|~ (\".*\")`$")
	keys := map[string]bool{}
	keyAlt := regexp.MustCompile(`\((?:\?:)?(password\|[^)]*)\)`)
	for _, l := range strings.Split(alloy, "\n") {
		if m := expr.FindStringSubmatch(l); m != nil {
			stages = append(stages, stage{re: regexp.MustCompile(m[1])})
			keys[keyAlt.FindStringSubmatch(m[1])[1]] = true
		} else if m := repl.FindStringSubmatch(l); m != nil {
			stages[len(stages)-1].with = m[1]
		} else if m := sel.FindStringSubmatch(l); m != nil {
			s, err := strconv.Unquote(m[1])
			if err != nil {
				panic(err)
			}
			stages = append(stages, stage{re: regexp.MustCompile(s)})
			keys[keyAlt.FindStringSubmatch(s)[1]] = true
		}
	}
	check("credential key alternation identical in every stage", len(keys) == 1, fmt.Sprint(keys))

	// CredentialRedactedFromLogs treats a live series as recent activity, so
	// the counter after the selector must exist once and expire after 5m idle.
	var counters []map[string]string
	afterSel := alloy[regexp.MustCompile("(?m)"+sel.String()).FindStringIndex(alloy)[1]:]
	for _, b := range regexp.MustCompile(`(?s)metric\.counter \{(.*?)\n\s*\}`).FindAllStringSubmatch(alloy, -1) {
		f := map[string]string{}
		for _, kv := range regexp.MustCompile(`(?m)^\s*(\w+)\s*=\s*(.*?)\s*$`).FindAllStringSubmatch(b[1], -1) {
			f[kv[1]] = kv[2]
		}
		if f["name"] == `"credential_lines_redacted_total"` {
			f["in selector's stage.match"] = fmt.Sprint(strings.Index(afterSel, b[0]) >= 0 && strings.Index(afterSel, b[0]) < strings.Index(afterSel, "stage.replace"))
			counters = append(counters, f)
		}
	}
	check("credential counter: once, under the selector, inc on match, 5m idle expiry", len(counters) == 1 &&
		counters[0]["in selector's stage.match"] == "true" && counters[0]["action"] == `"inc"` &&
		counters[0]["match_all"] == "true" && counters[0]["max_idle_duration"] == `"5m"`, fmt.Sprint(counters))
	pipeline := func(line string) (string, bool) {
		counted := false
		for _, s := range stages {
			if s.with == "" {
				counted = s.re.MatchString(line)
			} else {
				line = replace(s.re, line, s.with)
			}
		}
		return line, counted
	}

	for _, c := range []struct {
		name, in     string
		counted      bool
		want, absent string
	}{
		{"grafana starred env var", `logger=settings level=info msg="Config overridden from Environment variable" var="GF_SECURITY_ADMIN_PASSWORD=*********"`, false, `var="GF_SECURITY_ADMIN_<self-masked>"`, "*"},
		{"stars at end of line", `password=***`, false, `<self-masked>`, "*"},
		{"two starred keys, space kept", `password=*** api_key=****'`, false, `<self-masked> <self-masked>'`, "*"},
		{"single-quote terminator", `'passwd=****'`, false, `'<self-masked>'`, "*"},
		{"two stars is not a mask", `password=**"`, true, `<REDACTED>"`, "*"},
		{"stars then text is not a mask", `password=***Synthetic1"`, true, `<REDACTED>"`, "Synthetic1"},
		{"star inside a value", `password=Synth*etic2 x`, true, `<REDACTED> x`, "etic2"},
		{"plain synthetic password", `password=hunter2-synthetic&x=1`, true, `<REDACTED>&x=1`, "hunter2"},
		{"grafana real value still counted", `var="GF_SECURITY_ADMIN_PASSWORD=synthetic-admin-pass"`, true, `GF_SECURITY_ADMIN_<REDACTED>"`, "synthetic-admin-pass"},
		{"multiline mask then secret", "var=\"PASSWORD=*****\"\n    at password=synthsecret3", true, "<self-masked>\"\n    at <REDACTED>", "synthsecret3"},
		{"multiline mask then plain text", "PASSWORD=*****\nnext line", false, "<self-masked>\nnext line", "*"},
		{"servarr (removed)", `Password=(removed);Host=db`, false, `<self-masked>;Host=db`, "(removed)"},
		{"cliproxy first4...last4", `api_key=abcd...wxyz, cooling down`, false, `<self-masked>, cooling down`, "wxyz"},
		{"cliproxy shape glued to more text", `api_key=abcd...wxyz*SECRETX`, true, `<REDACTED>`, "SECRETX"},
	} {
		out, counted := pipeline(c.in)
		check("credential: "+c.name, counted == c.counted && strings.Contains(out, c.want) && !strings.Contains(out, c.absent),
			fmt.Sprintf("counted=%v out=%s", counted, out))
	}

	// The real Alloy/Loki fixture suite checks severity matching. Here verify
	// that the Explore example is identical and every shipped regexp is RE2.
	severityPipeline := func(block string) string {
		var steps []string
		for _, l := range strings.Split(block, "\n") {
			l = strings.TrimSpace(l)
			if strings.HasPrefix(l, "| ") {
				steps = append(steps, l)
			}
		}
		return strings.Join(steps, "\n")
	}
	serv := strings.SplitN(strings.SplitN(rules, "- alert: ServarrErrorSpike", 2)[1], "- name: cluster-fatal", 2)[0]
	parts := strings.SplitN(serv, "Inspect in Grafana → Explore → Loki:", 2)
	check("servarr Explore example exists", len(parts) == 2, serv)
	if len(parts) == 2 {
		alert, example := severityPipeline(parts[0]), severityPipeline(parts[1])
		check("servarr Explore example uses the alert's severity pipeline", alert != "" && alert == example, example)
		regexes := regexp.MustCompile("\\| regexp \x60([^\x60]*)\x60").FindAllStringSubmatch(alert, -1)
		check("servarr severity pipeline has three RE2 extractors", len(regexes) == 3, alert)
		for _, r := range regexes {
			_, err := regexp.Compile(r[1])
			check("servarr severity extractor compiles as RE2", err == nil, r[1])
		}
	}

	if fails > 0 {
		fmt.Printf("%d fixture(s) failed\n", fails)
		os.Exit(1)
	}
	fmt.Println("all log pipeline fixtures passed")
}
GO

go version
go run "$tmp/main.go" "$repo_root"
