#!/usr/bin/env bash
# Synthetic fixtures for the committed log regexes: alloy's credential
# self-mask/count/redact stages and the ServarrErrorSpike LogQL line filters.
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

	// ServarrErrorSpike: every |~ must match and no !~ may match. The
	// description's Explore example must use the same filters.
	filt := regexp.MustCompile("(\\|~|!~) `([^`]*)`")
	var expr5m, example []string
	for _, l := range strings.Split(rules, "\n") {
		if strings.Contains(l, `count_over_time({namespace="servarr"}`) {
			expr5m = filt.FindAllString(l, -1)
		} else if strings.Contains(l, `{namespace="servarr", container=`) {
			example = filt.FindAllString(l, -1)
		}
	}
	check("servarr Explore example uses the alert's filters", strings.Join(expr5m, " ") == strings.Join(example, " "), strings.Join(example, " "))
	counts := func(line string) bool {
		for _, f := range filt.FindAllStringSubmatch(strings.Join(expr5m, " "), -1) {
			if regexp.MustCompile(f[2]).MatchString(line) != (f[1] == "|~") {
				return false
			}
		}
		return true
	}
	const ts = "2026-01-01T00:00:00.000Z "
	for _, c := range []struct {
		name, in string
		counted  bool
	}{
		{"seerr plain debug with error field", ts + `[debug][DnsCache]: Lookup retry {"error":"synthetic"}`, false},
		{"seerr ANSI debug with error field", ts + "[\x1b[34mdebug\x1b[39m][DnsCache]: Lookup retry {\"error\":\"synthetic\"}", false},
		{"seerr ANSI trace with error word", ts + "[\x1b[90mtrace\x1b[39m][Api]: error path taken", false},
		{"radarr plain debug", `[Debug] Api: request error, retrying`, false},
		{"radarr plain trace", `[Trace] Http: error body follows`, false},
		{"seerr ANSI error with error word", ts + "[\x1b[31merror\x1b[39m][Api]: Request error", true},
		{"seerr ANSI error level only", ts + "[\x1b[31merror\x1b[39m][Api]: Request failed", true},
		{"seerr ANSI fatal level only", ts + "[\x1b[35mfatal\x1b[39m][Server]: shutting down", true},
		{"ANSI panic level only, two SGRs", "[\x1b[1m\x1b[31mPANIC\x1b[39m\x1b[22m][Main]: aborting", true},
		{"ANSI error level mentioning [debug]", ts + "[\x1b[31merror\x1b[39m][Settings]: cannot parse [debug] section", true},
		{"radarr plain fatal level only", `[Fatal] Host: startup aborted`, true},
		{"plain panic level only", `[panic][Main]: aborting`, true},
		{"seerr two-SGR debug with error field", ts + "[\x1b[1m\x1b[34mdebug\x1b[39m\x1b[22m][DnsCache]: Lookup retry {\"error\":\"synthetic\"}", false},
		{"two-SGR trace with error word", "[\x1b[2m\x1b[90mTRACE\x1b[39m\x1b[22m][Api]: error path taken", false},
		{"terror is not error", ts + `[info][Jobs]: added terror movie`, false},
		{"ANSI terror is not error", ts + "[\x1b[32minfo\x1b[39m][Jobs]: \x1b[31mterror\x1b[39m added", false},
		{"ANSI errors/panicked are not words", ts + "[\x1b[32minfo\x1b[39m][Jobs]: \x1b[31merrors\x1b[39m \x1b[31mpanicked\x1b[39m", false},
		{"seerr plain error", ts + `[error][Api]: Request failed`, true},
		{"radarr fatal", `[Fatal] Host: fatal startup failure`, true},
		{"go panic", `panic: synthetic nil map write`, true},
		{"error mentioning [debug]", ts + `[error][Settings]: cannot parse [debug] section`, true},
		{"error mentioning trace", `[Error] Api: trace id 42 failed with error`, true},
		{"untimestamped error prefix then [debug]", `ERROR [debug] section unreadable`, true},
		{"info with error word kept", `[Info] Api: 0 error(s) in last sync`, true},
		{"seerr ANSI info with error word kept", ts + "[\x1b[32minfo\x1b[39m][Jobs]: finished with error count 1", true},
		{"info without error word", ts + `[info][Jobs]: finished`, false},
	} {
		check("servarr: "+c.name, counts(c.in) == c.counted, c.in)
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
