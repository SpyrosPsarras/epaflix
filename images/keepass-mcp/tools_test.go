package main

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"reflect"
	"sort"
	"strings"
	"testing"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// connect wires an in-memory MCP client to the tool server over v.
func connect(t *testing.T, v *Vault, log *slog.Logger) *mcp.ClientSession {
	t.Helper()
	ct, st := mcp.NewInMemoryTransports()
	ctx := context.Background()
	if _, err := newServer(v, log).Connect(ctx, st, nil); err != nil {
		t.Fatal(err)
	}
	cs, err := mcp.NewClient(&mcp.Implementation{Name: "test", Version: "0"}, nil).Connect(ctx, ct, nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { cs.Close() })
	return cs
}

// call returns the text of the first content block and whether the tool errored.
func call(t *testing.T, cs *mcp.ClientSession, name string, args map[string]any) (string, bool) {
	t.Helper()
	res, err := cs.CallTool(context.Background(), &mcp.CallToolParams{Name: name, Arguments: args})
	if err != nil {
		t.Fatalf("%s: %v", name, err)
	}
	if len(res.Content) == 0 {
		t.Fatalf("%s: no content", name)
	}
	return res.Content[0].(*mcp.TextContent).Text, res.IsError
}

func callJSON(t *testing.T, cs *mcp.ClientSession, name string, args map[string]any, out any) {
	t.Helper()
	text, isErr := call(t, cs, name, args)
	if isErr {
		t.Fatalf("%s failed: %s", name, text)
	}
	if err := json.Unmarshal([]byte(text), out); err != nil {
		t.Fatalf("%s: %v in %q", name, err, text)
	}
}

func keysOf(m map[string]any) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

func TestHubSecret(t *testing.T) {
	f := newFake(t)
	v := f.open()
	srv := httptest.NewServer(newHandler(v, "hub-secret", slog.New(slog.NewTextHandler(io.Discard, nil))))
	defer srv.Close()

	const initialize = `{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"t","version":"0"}}}`
	post := func(secret string) int {
		req, _ := http.NewRequest(http.MethodPost, srv.URL+"/keepass", strings.NewReader(initialize))
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("Accept", "application/json, text/event-stream")
		if secret != "" {
			req.Header.Set("X-Hub-Secret", secret)
		}
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		defer resp.Body.Close()
		return resp.StatusCode
	}
	if got := post(""); got != http.StatusUnauthorized {
		t.Errorf("no header: %d", got)
	}
	if got := post("wrong"); got != http.StatusUnauthorized {
		t.Errorf("wrong header: %d", got)
	}
	if got := post("hub-secret"); got != http.StatusOK {
		t.Errorf("right header: %d", got)
	}
}

func TestHealthzStale(t *testing.T) {
	f := newFake(t)
	v := f.open()
	srv := httptest.NewServer(newHandler(v, "s", slog.New(slog.NewTextHandler(io.Discard, nil))))
	defer srv.Close()
	status := func() int {
		resp, err := http.Get(srv.URL + "/healthz")
		if err != nil {
			t.Fatal(err)
		}
		resp.Body.Close()
		return resp.StatusCode
	}
	if got := status(); got != http.StatusOK {
		t.Fatalf("fresh: %d", got)
	}
	v.mu.Lock()
	v.lastSync = time.Now().Add(-6 * time.Minute)
	v.mu.Unlock()
	if got := status(); got != http.StatusServiceUnavailable {
		t.Fatalf("stale: %d", got)
	}
}

func TestToolContract(t *testing.T) {
	f := newFake(t)
	f.seedGroup(uid(1), "SSH", "")
	e := testEntry(uid(2), "key", uid(1))
	e["binaries"] = []any{
		map[string]any{"name": "id_z", "data": base64.StdEncoding.EncodeToString([]byte("z"))},
		map[string]any{"name": "id_a", "data": base64.StdEncoding.EncodeToString([]byte("a"))},
	}
	f.seedEntry(e)
	f.seedEntry(testEntry(uid(3), "other", ""))
	cs := connect(t, f.open(), slog.New(slog.NewTextHandler(io.Discard, nil)))

	tools, err := cs.ListTools(context.Background(), nil)
	if err != nil {
		t.Fatal(err)
	}
	var names []string
	for _, tl := range tools.Tools {
		names = append(names, tl.Name)
		if tl.Description == "" {
			t.Errorf("%s has no description", tl.Name)
		}
	}
	sort.Strings(names)
	want := []string{"vault_add", "vault_attach", "vault_attachment", "vault_get", "vault_list", "vault_trash", "vault_update"}
	if !reflect.DeepEqual(names, want) {
		t.Fatalf("tools = %v", names)
	}

	text, _ := call(t, cs, "vault_list", map[string]any{"prefix": "SSH"})
	if strings.Contains(text, "pw-value") {
		t.Fatalf("vault_list leaks the password: %s", text)
	}
	var list []map[string]any
	callJSON(t, cs, "vault_list", map[string]any{"prefix": "/SSH"}, &list)
	if len(list) != 1 {
		t.Fatalf("list = %v", list)
	}
	wantKeys := []string{"attachments", "expired", "notes", "path", "title", "url", "username"}
	if got := keysOf(list[0]); !reflect.DeepEqual(got, wantKeys) {
		t.Fatalf("summary keys = %v", got)
	}
	if list[0]["path"] != "/SSH/key" || !reflect.DeepEqual(list[0]["attachments"], []any{"id_a", "id_z"}) {
		t.Fatalf("summary = %v", list[0])
	}
	var all []map[string]any
	callJSON(t, cs, "vault_list", nil, &all)
	if len(all) != 2 {
		t.Fatalf("empty prefix lists all, got %v", all)
	}

	var got map[string]any
	callJSON(t, cs, "vault_get", map[string]any{"path": "/SSH/key"}, &got)
	wantGet := []string{"attachments", "custom_properties", "expired", "notes", "password", "path", "title", "url", "username"}
	if g := keysOf(got); !reflect.DeepEqual(g, wantGet) {
		t.Fatalf("get keys = %v", g)
	}
	if got["password"] != "pw-value" || got["custom_properties"].(map[string]any)["Env"] != "prod" {
		t.Fatalf("get = %v", got)
	}
	var noPw map[string]any
	callJSON(t, cs, "vault_get", map[string]any{"path": "/SSH/key", "include_password": false}, &noPw)
	if _, ok := noPw["password"]; ok {
		t.Fatalf("include_password=false still returns a password: %v", noPw)
	}
	if text, isErr := call(t, cs, "vault_get", map[string]any{"path": "/nope"}); !isErr || !strings.Contains(text, "no entry at path") {
		t.Fatalf("missing path: %q %v", text, isErr)
	}
}

func TestWriteTools(t *testing.T) {
	f := newFake(t)
	f.seedEntry(testEntry(uid(1), "k", ""))
	var logs bytes.Buffer
	cs := connect(t, f.open(), slog.New(slog.NewTextHandler(&logs, nil)))

	var added map[string]any
	callJSON(t, cs, "vault_add", map[string]any{"path": "/New/Thing", "username": "u", "password": "brand-new-secret", "props": map[string]any{"k1": "v1"}}, &added)
	if added["path"] != "/New/Thing" || added["username"] != "u" {
		t.Fatalf("add = %v", added)
	}
	if _, ok := added["password"]; ok {
		t.Fatalf("add summary leaks the password: %v", added)
	}
	s := f.decryptEntry(f.lastWrite().Blob)["strings"].(map[string]any)
	if s["Password"].(map[string]any)["v"] != "brand-new-secret" || s["k1"].(map[string]any)["v"] != "v1" {
		t.Fatalf("stored strings = %v", s)
	}
	if text, isErr := call(t, cs, "vault_add", map[string]any{"path": "/New/Thing2", "props": map[string]any{"Password": "x"}}); !isErr {
		t.Fatalf("reserved prop key must fail: %s", text)
	}

	payload := []byte("private key\x00\xff")
	b64 := base64.StdEncoding.EncodeToString(payload)
	var att map[string]any
	callJSON(t, cs, "vault_attach", map[string]any{"path": "/k", "filename": "id", "content_b64": b64}, &att)
	if att["attached"] != "id" || att["entry"] != "k" || att["bytes"] != float64(len(payload)) {
		t.Fatalf("attach = %v", att)
	}
	callJSON(t, cs, "vault_attach", map[string]any{"path": "/k", "filename": "id", "content_b64": base64.StdEncoding.EncodeToString([]byte("v2"))}, &att)
	bins := f.decryptEntry(f.lastWrite().Blob)["binaries"].([]any)
	if len(bins) != 1 {
		t.Fatalf("same-named attachment must be replaced, got %v", bins)
	}
	if _, isErr := call(t, cs, "vault_attach", map[string]any{"path": "/k", "filename": "e", "content_b64": ""}); !isErr {
		t.Fatal("empty content must fail")
	}
	var fetched map[string]any
	callJSON(t, cs, "vault_attachment", map[string]any{"path": "/k", "filename": "id"}, &fetched)
	if dec, _ := base64.StdEncoding.DecodeString(fetched["content_b64"].(string)); string(dec) != "v2" ||
		fetched["filename"] != "id" || fetched["entry"] != "k" {
		t.Fatalf("attachment = %v", fetched)
	}
	if _, isErr := call(t, cs, "vault_attachment", map[string]any{"path": "/k", "filename": "nope"}); !isErr {
		t.Fatal("missing attachment must fail")
	}

	var trashed map[string]any
	callJSON(t, cs, "vault_trash", map[string]any{"path": "/k"}, &trashed)
	if trashed["trashed"] != "k" {
		t.Fatalf("trash = %v", trashed)
	}
	if w := f.lastWrite(); w.Method != http.MethodDelete || w.UUID != uid(1) {
		t.Fatalf("trash write = %+v", w)
	}

	out := logs.String()
	for _, secret := range []string{"brand-new-secret", "pw-value", "private key", b64} {
		if strings.Contains(out, secret) {
			t.Fatalf("log leaks %q:\n%s", secret, out)
		}
	}
	if !strings.Contains(out, "tool=vault_trash") || !strings.Contains(out, uid(1)) || !strings.Contains(out, "result=ok") {
		t.Fatalf("write log line missing tool, uuid or result:\n%s", out)
	}
}

func TestExpiredWithholdsPassword(t *testing.T) {
	f := newFake(t)
	past := time.Now().Add(-time.Hour)
	e := testEntry(uid(1), "old", "")
	e["times"] = testTimes(true, &past)
	f.seedEntry(e)
	cs := connect(t, f.open(), slog.New(slog.NewTextHandler(io.Discard, nil)))

	text, isErr := call(t, cs, "vault_get", map[string]any{"path": "/old"})
	if isErr || strings.Contains(text, "pw-value") {
		t.Fatalf("expired get leaks: %q", text)
	}
	var got map[string]any
	callJSON(t, cs, "vault_get", map[string]any{"path": "/old"}, &got)
	pw, ok := got["password"]
	if !ok || pw != nil || got["expired"] != true {
		t.Fatalf("want password:null and expired:true, got %v", got)
	}
	if got["note"] != "entry is expired; password withheld. Update the entry's expiry in KeePassXC, then retry." {
		t.Fatalf("note = %v", got["note"])
	}
}

func TestUpdateNullPropDeletes(t *testing.T) {
	f := newFake(t)
	e := testEntry(uid(1), "web", "")
	e["strings"].(map[string]any)["Old"] = map[string]any{"v": "x"}
	f.seedEntry(e)
	cs := connect(t, f.open(), slog.New(slog.NewTextHandler(io.Discard, nil)))

	var got map[string]any
	callJSON(t, cs, "vault_update", map[string]any{
		"path": "/web", "notes": "", "password": "rotated",
		"props": map[string]any{"Old": nil, "New": "n", "Absent": nil},
	}, &got)
	s := f.decryptEntry(f.lastWrite().Blob)["strings"].(map[string]any)
	if _, ok := s["Old"]; ok {
		t.Fatalf("null prop must delete Old: %v", s)
	}
	if s["New"].(map[string]any)["v"] != "n" || s["Env"].(map[string]any)["v"] != "prod" {
		t.Fatalf("props = %v", s)
	}
	if s["Notes"].(map[string]any)["v"] != "" {
		t.Fatalf("empty string must clear notes: %v", s["Notes"])
	}
	if s["UserName"].(map[string]any)["v"] != "alice" || s["URL"].(map[string]any)["v"] != "https://example.com" {
		t.Fatalf("omitted fields must stay: %v", s)
	}
	pw := s["Password"].(map[string]any)
	if pw["v"] != "rotated" || pw["protected"] != true {
		t.Fatalf("password = %v", pw)
	}
	if got["path"] != "/web" || got["notes"] != "" {
		t.Fatalf("summary = %v", got)
	}

	// Explicit JSON nulls mean "unchanged", as with the Python tools.
	var same map[string]any
	callJSON(t, cs, "vault_update", map[string]any{"path": "/web", "title": nil, "notes": nil, "props": nil}, &same)
	if same["path"] != "/web" || same["username"] != "alice" {
		t.Fatalf("null args changed the entry: %v", same)
	}
}

func TestGetByUUIDForUntitled(t *testing.T) {
	f := newFake(t)
	f.seedGroup(uid(1), "G", "")
	f.seedEntry(testEntry(uid(0xabcdef), "", uid(1)))
	cs := connect(t, f.open(), slog.New(slog.NewTextHandler(io.Discard, nil)))

	upper := strings.ToUpper(uid(0xabcdef))
	var got map[string]any
	callJSON(t, cs, "vault_get", map[string]any{"path": "", "uuid": upper}, &got)
	if got["password"] != "pw-value" || got["title"] != "" {
		t.Fatalf("get by uuid = %v", got)
	}
	var updated map[string]any
	callJSON(t, cs, "vault_update", map[string]any{"path": "", "uuid": upper, "title": "named"}, &updated)
	if updated["title"] != "named" || updated["path"] != "/G/named" {
		t.Fatalf("update by uuid = %v", updated)
	}
	var trashed map[string]any
	callJSON(t, cs, "vault_trash", map[string]any{"path": "", "uuid": upper}, &trashed)
	if trashed["trashed"] != "G/named" {
		t.Fatalf("trash by uuid = %v", trashed)
	}
}

func TestEnroll(t *testing.T) {
	var body struct {
		DeviceName string `json:"device_name"`
		PublicKey  string `json:"public_key"`
	}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost || r.URL.Path != "/api/v1/devices/enroll" || r.Header.Get("Authorization") != "Bearer enroll-tok" {
			http.Error(w, "bad request", http.StatusBadRequest)
			return
		}
		json.NewDecoder(r.Body).Decode(&body)
		w.WriteHeader(http.StatusCreated)
		w.Write([]byte(`{"device":{"id":"d1"},"token":"device-tok"}`))
	}))
	defer srv.Close()

	var out bytes.Buffer
	if err := runEnroll(context.Background(), &out, []string{"--server", srv.URL, "--name", "t3code", "enroll-tok"}); err != nil {
		t.Fatal(err)
	}
	var got map[string]string
	if err := json.Unmarshal(out.Bytes(), &got); err != nil {
		t.Fatalf("%v in %q", err, out.String())
	}
	if len(got) != 2 || got["device_token"] != "device-tok" {
		t.Fatalf("output = %v", got)
	}
	priv, err := base64.StdEncoding.DecodeString(got["private_key_b64"])
	if err != nil || len(priv) != 32 {
		t.Fatalf("private key: %v, %d bytes", err, len(priv))
	}
	pub, err := base64.StdEncoding.DecodeString(body.PublicKey)
	if body.DeviceName != "t3code" || err != nil || len(pub) != 32 {
		t.Fatalf("request body = %+v (%v)", body, err)
	}
}
