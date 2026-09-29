package main

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"sort"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"gitlab.com/Star95/keepass-deltasync/client/mobile"
)

const (
	testDB    = "11111111-2222-4333-8444-555555555555"
	testToken = "test-token"
)

var testPassword = []byte("test-master")

// fakeServer implements the four DeltaSync endpoints the vault uses.
type fakeServer struct {
	t    *testing.T
	sess *mobile.Session
	srv  *httptest.Server

	mu      sync.Mutex
	seq     int64
	objs    map[string]Change
	writes  []fakeWrite
	changes []int64 // since values seen on /changes
}

type fakeWrite struct {
	Method, Segment, UUID string
	Blob                  []byte
}

func newFake(t *testing.T) *fakeServer {
	t.Helper()
	sess, err := mobile.NewSession(testPassword, testDB)
	if err != nil {
		t.Fatal(err)
	}
	f := &fakeServer{t: t, sess: sess, objs: map[string]Change{}}
	f.srv = httptest.NewServer(http.HandlerFunc(f.handle))
	t.Cleanup(f.srv.Close)
	return f
}

func (f *fakeServer) handle(w http.ResponseWriter, r *http.Request) {
	if r.Header.Get("Authorization") != "Bearer "+testToken {
		http.Error(w, `{"error":"unauthorized"}`, http.StatusUnauthorized)
		return
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	prefix := "/api/v1/databases/" + testDB + "/"
	switch {
	case r.Method == http.MethodGet && r.URL.Path == "/api/v1/databases":
		json.NewEncoder(w).Encode(map[string]any{"databases": []map[string]string{
			{"id": "other", "name": "work"}, {"id": testDB, "name": "personal"},
		}})
	case r.Method == http.MethodGet && r.URL.Path == prefix+"changes":
		if r.URL.Query().Get("include") != "groups" {
			http.Error(w, "include=groups missing", http.StatusBadRequest)
			return
		}
		since, _ := strconv.ParseInt(r.URL.Query().Get("since"), 10, 64)
		f.changes = append(f.changes, since)
		out := Changes{CurrentSeq: f.seq, Objects: []Change{}}
		for _, c := range f.objs {
			if c.Seq > since {
				out.Objects = append(out.Objects, c)
			}
		}
		sort.Slice(out.Objects, func(i, j int) bool { return out.Objects[i].Seq < out.Objects[j].Seq })
		json.NewEncoder(w).Encode(out)
	case (r.Method == http.MethodPut || r.Method == http.MethodDelete) && strings.HasPrefix(r.URL.Path, prefix):
		parts := strings.Split(strings.TrimPrefix(r.URL.Path, prefix), "/")
		if len(parts) != 2 || (parts[0] != "entries" && parts[0] != "groups") {
			http.NotFound(w, r)
			return
		}
		var body struct {
			ModifiedAt string `json:"modified_at"`
			Blob       string `json:"blob"`
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
		if _, err := time.Parse("2006-01-02T15:04:05Z", body.ModifiedAt); err != nil {
			http.Error(w, "bad modified_at", http.StatusBadRequest)
			return
		}
		blob, err := base64.StdEncoding.DecodeString(body.Blob)
		if err != nil || (r.Method == http.MethodPut && len(blob) == 0) {
			http.Error(w, "bad blob", http.StatusBadRequest)
			return
		}
		f.writes = append(f.writes, fakeWrite{r.Method, parts[0], parts[1], blob})
		kind := 1
		if parts[0] == "groups" {
			kind = 2
		}
		f.seq++
		f.objs[parts[1]] = Change{UUID: parts[1], Blob: body.Blob, ModifiedAt: body.ModifiedAt,
			Deleted: r.Method == http.MethodDelete, Seq: f.seq, Kind: kind}
		key := "entry"
		if kind == 2 {
			key = "group"
		}
		json.NewEncoder(w).Encode(map[string]any{key: map[string]any{
			"uuid": parts[1], "modified_at": body.ModifiedAt, "deleted": r.Method == http.MethodDelete, "seq": f.seq,
		}})
	default:
		http.NotFound(w, r)
	}
}

func (f *fakeServer) store(uuid string, kind int, blob []byte, deleted bool) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.seq++
	f.objs[uuid] = Change{UUID: uuid, Blob: base64.StdEncoding.EncodeToString(blob),
		ModifiedAt: "2026-01-01T00:00:00Z", Deleted: deleted, Seq: f.seq, Kind: kind}
}

func (f *fakeServer) seedGroup(uuid, name, parent string) {
	f.t.Helper()
	g := map[string]any{"v": 1, "uuid": uuid, "name": name, "parent_group": parent, "icon_id": 0,
		"times": testTimes(false, nil)}
	raw, _ := json.Marshal(g)
	blob, err := f.sess.EncryptGroup(raw)
	if err != nil {
		f.t.Fatal(err)
	}
	f.store(uuid, 2, blob, false)
}

func (f *fakeServer) seedEntry(e map[string]any) {
	f.t.Helper()
	raw, _ := json.Marshal(e)
	blob, err := f.sess.EncryptEntry(raw)
	if err != nil {
		f.t.Fatal(err)
	}
	f.store(e["uuid"].(string), 1, blob, false)
}

func (f *fakeServer) lastWrite() fakeWrite {
	f.mu.Lock()
	defer f.mu.Unlock()
	if len(f.writes) == 0 {
		f.t.Fatal("no writes recorded")
	}
	return f.writes[len(f.writes)-1]
}

func (f *fakeServer) decryptEntry(blob []byte) map[string]any {
	f.t.Helper()
	plain, err := f.sess.DecryptEntry(blob)
	if err != nil {
		f.t.Fatal(err)
	}
	var m map[string]any
	if err := json.Unmarshal(plain, &m); err != nil {
		f.t.Fatal(err)
	}
	return m
}

func (f *fakeServer) open() *Vault {
	f.t.Helper()
	api := &API{Base: f.srv.URL, Token: testToken, HTTP: f.srv.Client()}
	v, err := Open(context.Background(), api, testDB, testPassword)
	if err != nil {
		f.t.Fatal(err)
	}
	return v
}

func testTimes(expires bool, expiresAt *time.Time) map[string]any {
	t := map[string]any{
		"created": "2025-01-01T00:00:00Z", "modified": "2025-01-01T00:00:00Z",
		"accessed": "2025-01-01T00:00:00Z", "location_changed": "2025-01-01T00:00:00Z",
		"expires": expires, "usage_count": 0,
	}
	if expiresAt != nil {
		t["expires_at"] = expiresAt.UTC().Format(time.RFC3339)
	}
	return t
}

func testEntry(uuid, title, parent string) map[string]any {
	return map[string]any{
		"v": 1, "uuid": uuid, "parent_group": parent, "icon_id": 0, "times": testTimes(false, nil),
		"strings": map[string]any{
			"Title":    map[string]any{"v": title},
			"UserName": map[string]any{"v": "alice"},
			"Password": map[string]any{"v": "pw-value", "protected": true},
			"URL":      map[string]any{"v": "https://example.com"},
			"Notes":    map[string]any{"v": "note"},
			"Env":      map[string]any{"v": "prod"},
		},
	}
}

func uid(n int) string { return fmt.Sprintf("00000000-0000-4000-8000-%012d", n) }

func TestPathsFromGroups(t *testing.T) {
	f := newFake(t)
	f.seedGroup(uid(1), "SSH", "")
	f.seedGroup(uid(2), "keys", uid(1))
	f.seedEntry(testEntry(uid(3), "k", uid(2)))
	v := f.open()

	got := v.Entries()
	if len(got) != 1 || got[0].Path != "/SSH/keys/k" {
		t.Fatalf("entries = %+v", got)
	}
	e := got[0]
	if e.Username != "alice" || e.Password != "pw-value" || e.URL != "https://example.com" || e.Notes != "note" ||
		e.Title != "k" || e.UUID != uid(3) || e.Expired {
		t.Fatalf("fields = %+v", e)
	}
	if len(e.Props) != 1 || e.Props["Env"] != "prod" {
		t.Fatalf("props = %v", e.Props)
	}
	if byPath, err := v.Find("SSH/keys/k/", ""); err != nil || byPath.UUID != uid(3) {
		t.Fatalf("Find by path = %+v, %v", byPath, err)
	}
	if byID, err := v.Find("", uid(3)); err != nil || byID.Path != "/SSH/keys/k" {
		t.Fatalf("Find by uuid = %+v, %v", byID, err)
	}
	if _, err := v.Find("/nope", ""); err == nil {
		t.Fatal("missing path must error")
	}
}

func TestUpdateKeepsUnknownFields(t *testing.T) {
	f := newFake(t)
	e := testEntry(uid(1), "web", "")
	e["autotype"] = map[string]any{"enabled": true, "default_sequence": "{USERNAME}{TAB}{PASSWORD}{ENTER}",
		"associations": []any{map[string]any{"window": "Firefox*", "sequence": "{PASSWORD}"}}}
	old := testEntry(uid(1), "web", "")
	delete(old, "parent_group")
	e["history"] = []any{old}
	e["custom_data"] = map[string]any{"KPXC_x": map[string]any{"v": "1", "modified": "2025-02-01T00:00:00Z"}}
	e["tags"] = []any{"a", "b"}
	f.seedEntry(e)
	before := f.decryptEntry(mustDecodeBlob(t, f.objs[uid(1)].Blob))

	v := f.open()
	err := v.Write(context.Background(), uid(1), func(raw map[string]any) error {
		raw["strings"].(map[string]any)["UserName"] = map[string]any{"v": "bob"}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	w := f.lastWrite()
	if w.Method != http.MethodPut || w.Segment != "entries" || w.UUID != uid(1) {
		t.Fatalf("write = %+v", w)
	}
	after := f.decryptEntry(w.Blob)
	for _, k := range []string{"autotype", "history", "custom_data", "tags"} {
		a, _ := json.Marshal(before[k])
		b, _ := json.Marshal(after[k])
		if before[k] == nil || !bytes.Equal(a, b) {
			t.Errorf("%s changed:\n before %s\n after  %s", k, a, b)
		}
	}
	if got := after["strings"].(map[string]any)["UserName"].(map[string]any)["v"]; got != "bob" {
		t.Errorf("UserName = %v", got)
	}
	oldMod, _ := time.Parse(time.RFC3339Nano, before["times"].(map[string]any)["modified"].(string))
	newMod, err := time.Parse(time.RFC3339Nano, after["times"].(map[string]any)["modified"].(string))
	if err != nil || !newMod.After(oldMod) {
		t.Errorf("times.modified %v -> %v (%v)", oldMod, newMod, err)
	}
	if e, _ := v.Find("", uid(1)); e.Username != "bob" {
		t.Errorf("index not updated: %q", e.Username)
	}
}

func mustDecodeBlob(t *testing.T, s string) []byte {
	t.Helper()
	b, err := base64.StdEncoding.DecodeString(s)
	if err != nil {
		t.Fatal(err)
	}
	return b
}

func TestDuplicatePathIsAmbiguous(t *testing.T) {
	f := newFake(t)
	f.seedEntry(testEntry(uid(1), "dup", ""))
	f.seedEntry(testEntry(uid(2), "dup", ""))
	v := f.open()
	_, err := v.Find("/dup", "")
	if err == nil || !strings.Contains(err.Error(), uid(1)) || !strings.Contains(err.Error(), uid(2)) {
		t.Fatalf("err = %v", err)
	}
	if e, err := v.Find("/dup", uid(2)); err != nil || e.UUID != uid(2) {
		t.Fatalf("uuid disambiguates: %+v %v", e, err)
	}
}

func TestWriteRefreshesFirst(t *testing.T) {
	f := newFake(t)
	f.seedEntry(testEntry(uid(1), "web", ""))
	v := f.open()

	bumped := testEntry(uid(1), "web", "")
	bumped["strings"].(map[string]any)["Notes"] = map[string]any{"v": "edited on phone"}
	f.seedEntry(bumped)

	err := v.Write(context.Background(), uid(1), func(raw map[string]any) error {
		raw["strings"].(map[string]any)["UserName"] = map[string]any{"v": "bob"}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	s := f.decryptEntry(f.lastWrite().Blob)["strings"].(map[string]any)
	if s["Notes"].(map[string]any)["v"] != "edited on phone" || s["UserName"].(map[string]any)["v"] != "bob" {
		t.Fatalf("strings = %v", s)
	}
}

func TestOrphanFallsBackToRoot(t *testing.T) {
	f := newFake(t)
	f.seedEntry(testEntry(uid(1), "o", uid(99)))
	f.seedGroup(uid(2), "child", uid(98)) // orphan group also hangs off root
	f.seedEntry(testEntry(uid(3), "c", uid(2)))
	v := f.open()
	paths := map[string]bool{}
	for _, e := range v.Entries() {
		paths[e.Path] = true
	}
	if !paths["/o"] || !paths["/child/c"] {
		t.Fatalf("paths = %v", paths)
	}
}

func TestAttachmentRoundTrip(t *testing.T) {
	f := newFake(t)
	data := []byte("fake attachment\n\x00\x01\xff\n")
	e := testEntry(uid(1), "k", "")
	e["binaries"] = []any{map[string]any{"name": "id_test", "data": base64.StdEncoding.EncodeToString(data)}}
	f.seedEntry(e)
	v := f.open()

	got, err := v.Find("/k", "")
	if err != nil {
		t.Fatal(err)
	}
	if len(got.Attachments) != 1 || got.Attachments[0].Name != "id_test" || !bytes.Equal(got.Attachments[0].Data, data) {
		t.Fatalf("attachments = %+v", got.Attachments)
	}
	if err := v.Write(context.Background(), uid(1), func(map[string]any) error { return nil }); err != nil {
		t.Fatal(err)
	}
	b := f.decryptEntry(f.lastWrite().Blob)["binaries"].([]any)[0].(map[string]any)["data"].(string)
	if dec, _ := base64.StdEncoding.DecodeString(b); !bytes.Equal(dec, data) {
		t.Fatalf("written attachment = %q", dec)
	}
}

func TestDeletedDropsOut(t *testing.T) {
	f := newFake(t)
	f.seedGroup(uid(1), "G", "")
	f.seedEntry(testEntry(uid(2), "a", uid(1)))
	f.seedEntry(testEntry(uid(3), "b", ""))
	v := f.open()
	if n := len(v.Entries()); n != 2 {
		t.Fatalf("want 2 entries, got %d", n)
	}
	f.store(uid(2), 1, nil, true)
	f.store(uid(1), 2, nil, true)
	if err := v.Refresh(context.Background()); err != nil {
		t.Fatal(err)
	}
	got := v.Entries()
	if len(got) != 1 || got[0].Path != "/b" {
		t.Fatalf("entries = %+v", got)
	}
	if last := f.changes[len(f.changes)-1]; last != 3 {
		t.Fatalf("refresh asked since=%d, want 3", last)
	}
	if v.lastSync.IsZero() {
		t.Fatal("lastSync not set")
	}

	if err := v.Trash(context.Background(), uid(3)); err != nil {
		t.Fatal(err)
	}
	if w := f.lastWrite(); w.Method != http.MethodDelete || w.Segment != "entries" || w.UUID != uid(3) {
		t.Fatalf("trash write = %+v", w)
	}
	if n := len(v.Entries()); n != 0 {
		t.Fatalf("trashed entry still listed")
	}
}

func TestExpired(t *testing.T) {
	f := newFake(t)
	past, future := time.Now().Add(-time.Hour), time.Now().Add(time.Hour)
	for i, tc := range []struct {
		expires bool
		at      *time.Time
	}{{true, &past}, {true, &future}, {false, &past}} {
		e := testEntry(uid(i+1), fmt.Sprint(i), "")
		e["times"] = testTimes(tc.expires, tc.at)
		f.seedEntry(e)
	}
	v := f.open()
	want := map[string]bool{"/0": true, "/1": false, "/2": false}
	for _, e := range v.Entries() {
		if e.Expired != want[e.Path] {
			t.Errorf("%s expired = %v", e.Path, e.Expired)
		}
	}
}

func TestAddCreatesMissingGroups(t *testing.T) {
	f := newFake(t)
	f.seedGroup(uid(1), "SSH", "")
	v := f.open()
	e, err := v.Add(context.Background(), "/SSH/hosts/web1", map[string]String{
		"UserName": {V: "root"}, "Password": {V: "pw"}, "Env": {V: "prod"},
	})
	if err != nil {
		t.Fatal(err)
	}
	if e.Path != "/SSH/hosts/web1" || e.Username != "root" || e.Props["Env"] != "prod" {
		t.Fatalf("entry = %+v", e)
	}
	f.mu.Lock()
	writes := append([]fakeWrite(nil), f.writes...)
	f.mu.Unlock()
	if len(writes) != 2 || writes[0].Segment != "groups" || writes[1].Segment != "entries" {
		t.Fatalf("writes = %+v", writes)
	}
	plain, err := f.sess.DecryptGroup(writes[0].Blob)
	if err != nil {
		t.Fatal(err)
	}
	var g map[string]any
	json.Unmarshal(plain, &g)
	if g["name"] != "hosts" || g["parent_group"] != uid(1) || g["uuid"] != writes[0].UUID {
		t.Fatalf("group = %v", g)
	}
	ent := f.decryptEntry(writes[1].Blob)
	s := ent["strings"].(map[string]any)
	if ent["parent_group"] != writes[0].UUID || ent["v"] != float64(1) ||
		s["Title"].(map[string]any)["v"] != "web1" || s["Password"].(map[string]any)["protected"] != true ||
		s["Env"].(map[string]any)["protected"] != nil {
		t.Fatalf("entry blob = %v", ent)
	}
	tm := ent["times"].(map[string]any)
	for _, k := range []string{"created", "modified", "accessed", "location_changed"} {
		ts, err := time.Parse(time.RFC3339Nano, tm[k].(string))
		if err != nil || time.Since(ts) > time.Minute {
			t.Errorf("times.%s = %v", k, tm[k])
		}
	}

	if _, err := v.Add(context.Background(), "/SSH/hosts/web1", nil); err == nil {
		t.Error("adding an existing path must error")
	}
	f.seedGroup(uid(5), "SSH", "")
	if _, err := v.Add(context.Background(), "/SSH/x", nil); err == nil || !strings.Contains(err.Error(), uid(5)) {
		t.Errorf("sibling groups with the same name must be ambiguous, got %v", err)
	}
}

func TestDatabaseID(t *testing.T) {
	f := newFake(t)
	api := &API{Base: f.srv.URL, Token: testToken, HTTP: f.srv.Client()}
	id, err := api.DatabaseID(context.Background(), "personal")
	if err != nil || id != testDB {
		t.Fatalf("id = %q, %v", id, err)
	}
	if _, err := api.DatabaseID(context.Background(), "missing"); err == nil {
		t.Fatal("unknown name must error")
	}
	bad := &API{Base: f.srv.URL, Token: "wrong", HTTP: f.srv.Client()}
	if _, err := bad.DatabaseID(context.Background(), "personal"); err == nil || !strings.Contains(err.Error(), "401") {
		t.Fatalf("bad token err = %v", err)
	}
}
