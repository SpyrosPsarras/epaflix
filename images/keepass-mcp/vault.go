package main

import (
	"context"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"sort"
	"strings"
	"sync"
	"time"

	"gitlab.com/Star95/keepass-deltasync/client/mobile"
)

// Vault is an in-memory index of one DeltaSync database. Entries and groups
// are kept as decrypted canonical JSON (map[string]any) so fields this code
// does not know about survive an edit.
type Vault struct {
	mu       sync.Mutex
	api      *API
	dbID     string
	sess     *mobile.Session
	entries  map[string]map[string]any
	groups   map[string]map[string]any
	seq      int64
	lastSync time.Time
}

// Entry is the flattened view of one KeePass entry.
type Entry struct {
	UUID, Path, Title, Username, URL, Notes, Password string
	Expired                                           bool
	Props                                             map[string]string
	Attachments                                       []Attachment
}

// Attachment is one inline binary of an entry.
type Attachment struct {
	Name string
	Data []byte
}

// String is one canonical entry string field (canonical.String on the wire).
type String struct {
	V         string `json:"v"`
	Protected bool   `json:"protected,omitempty"`
}

var standardKeys = map[string]bool{"Title": true, "UserName": true, "Password": true, "URL": true, "Notes": true}

// Open derives the session key and loads the whole database from since=0.
func Open(ctx context.Context, api *API, dbID string, password []byte) (*Vault, error) {
	sess, err := mobile.NewSession(password, dbID)
	if err != nil {
		return nil, err
	}
	v := &Vault{api: api, dbID: dbID, sess: sess, entries: map[string]map[string]any{}, groups: map[string]map[string]any{}}
	if err := v.Refresh(ctx); err != nil {
		return nil, err
	}
	return v, nil
}

// Refresh pulls every change since the last seen seq and applies it.
func (v *Vault) Refresh(ctx context.Context) error {
	v.mu.Lock()
	defer v.mu.Unlock()
	return v.refresh(ctx)
}

func (v *Vault) refresh(ctx context.Context) error {
	ch, err := v.api.Changes(ctx, v.dbID, v.seq)
	if err != nil {
		return err
	}
	for _, c := range ch.Objects {
		index, decrypt := v.entries, v.sess.DecryptEntry
		if c.Kind == kindGroup {
			index, decrypt = v.groups, v.sess.DecryptGroup
		}
		if c.Deleted {
			delete(index, c.UUID)
			continue
		}
		blob, err := base64.StdEncoding.DecodeString(c.Blob)
		if err != nil {
			return fmt.Errorf("object %s: decode blob: %w", c.UUID, err)
		}
		plain, err := decrypt(blob)
		if err != nil {
			return fmt.Errorf("object %s: %w", c.UUID, err)
		}
		var raw map[string]any
		if err := json.Unmarshal(plain, &raw); err != nil {
			return fmt.Errorf("object %s: %w", c.UUID, err)
		}
		index[c.UUID] = raw
	}
	v.seq = ch.CurrentSeq
	v.lastSync = time.Now()
	return nil
}

// LastSync is the time of the last successful /changes call.
func (v *Vault) LastSync() time.Time {
	v.mu.Lock()
	defer v.mu.Unlock()
	return v.lastSync
}

// Entries returns every entry, sorted by path.
func (v *Vault) Entries() []Entry {
	v.mu.Lock()
	defer v.mu.Unlock()
	out := make([]Entry, 0, len(v.entries))
	for id, raw := range v.entries {
		out = append(out, v.entry(id, raw))
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].Path != out[j].Path {
			return out[i].Path < out[j].Path
		}
		return out[i].UUID < out[j].UUID
	})
	return out
}

// Find returns the entry with the given uuid, or else the one at path.
func (v *Vault) Find(path, uuid string) (Entry, error) {
	v.mu.Lock()
	defer v.mu.Unlock()
	if uuid != "" {
		raw, ok := v.entries[uuid]
		if !ok {
			return Entry{}, fmt.Errorf("no entry with uuid %s", uuid)
		}
		return v.entry(uuid, raw), nil
	}
	needle := "/" + strings.Trim(path, "/")
	var hits []Entry
	for id, raw := range v.entries {
		if e := v.entry(id, raw); e.Path == needle {
			hits = append(hits, e)
		}
	}
	switch len(hits) {
	case 0:
		return Entry{}, fmt.Errorf("no entry at path '%s' (list entries to see valid paths)", path)
	case 1:
		return hits[0], nil
	}
	ids := make([]string, len(hits))
	for i, e := range hits {
		ids[i] = e.UUID
	}
	sort.Strings(ids)
	return Entry{}, fmt.Errorf("path '%s' matches %d entries (%s); pass a uuid", needle, len(ids), strings.Join(ids, ", "))
}

// Write refreshes, applies edit to a copy of the raw entry, stamps
// times.modified, then encrypts and PUTs it. The index changes only on success.
func (v *Vault) Write(ctx context.Context, uuid string, edit func(raw map[string]any) error) error {
	v.mu.Lock()
	defer v.mu.Unlock()
	if err := v.refresh(ctx); err != nil {
		return err
	}
	cur, ok := v.entries[uuid]
	if !ok {
		return fmt.Errorf("no entry with uuid %s", uuid)
	}
	raw, err := clone(cur)
	if err != nil {
		return err
	}
	if err := edit(raw); err != nil {
		return err
	}
	now := time.Now().UTC()
	times, _ := raw["times"].(map[string]any)
	if times == nil {
		times = map[string]any{}
		raw["times"] = times
	}
	times["modified"] = now.Format(time.RFC3339Nano)
	return v.put(ctx, "entries", uuid, raw, now)
}

// Add creates the entry at path ("/Group/Sub/Title"), creating missing groups.
// Existing groups are reused by name under the same parent.
func (v *Vault) Add(ctx context.Context, path string, fields map[string]String) (Entry, error) {
	parts := strings.FieldsFunc(path, func(r rune) bool { return r == '/' })
	if len(parts) == 0 {
		return Entry{}, errors.New("path must name an entry, e.g. /Group/Title")
	}
	for _, p := range parts {
		if p == "Recycle Bin" {
			return Entry{}, errors.New("entries inside the Recycle Bin are managed by vault_trash and KeePassXC")
		}
	}
	v.mu.Lock()
	defer v.mu.Unlock()
	if err := v.refresh(ctx); err != nil {
		return Entry{}, err
	}
	needle := "/" + strings.Join(parts, "/")
	for id, raw := range v.entries {
		if v.entry(id, raw).Path == needle {
			return Entry{}, fmt.Errorf("an entry already exists at '%s' (uuid %s)", needle, id)
		}
	}

	now := time.Now().UTC()
	parent := ""
	for _, name := range parts[:len(parts)-1] {
		var ids []string
		for id, g := range v.groups {
			if g["name"] == name && v.parentOf(g) == parent {
				ids = append(ids, id)
			}
		}
		switch len(ids) {
		case 1:
			parent = ids[0]
			continue
		case 0:
		default:
			sort.Strings(ids)
			return Entry{}, fmt.Errorf("group '%s' is ambiguous: %d sibling groups share the name (%s)", name, len(ids), strings.Join(ids, ", "))
		}
		id, err := newUUID()
		if err != nil {
			return Entry{}, err
		}
		g := map[string]any{"v": 1, "uuid": id, "name": name, "parent_group": parent, "icon_id": 0, "times": newTimes(now)}
		if err := v.put(ctx, "groups", id, g, now); err != nil {
			return Entry{}, err
		}
		parent = id
	}

	id, err := newUUID()
	if err != nil {
		return Entry{}, err
	}
	strs := map[string]any{}
	for k, s := range fields {
		if k == "Password" {
			s.Protected = true
		}
		strs[k] = map[string]any{"v": s.V, "protected": s.Protected}
	}
	strs["Title"] = map[string]any{"v": parts[len(parts)-1], "protected": false}
	raw := map[string]any{"v": 1, "uuid": id, "parent_group": parent, "icon_id": 0, "times": newTimes(now), "strings": strs}
	if err := v.put(ctx, "entries", id, raw, now); err != nil {
		return Entry{}, err
	}
	return v.entry(id, v.entries[id]), nil
}

// Trash writes a tombstone for the entry and drops it from the index.
func (v *Vault) Trash(ctx context.Context, uuid string) error {
	v.mu.Lock()
	defer v.mu.Unlock()
	if _, ok := v.entries[uuid]; !ok {
		return fmt.Errorf("no entry with uuid %s", uuid)
	}
	if _, err := v.api.Delete(ctx, v.dbID, "entries", uuid, time.Now()); err != nil {
		return err
	}
	delete(v.entries, uuid)
	return nil
}

// put encrypts raw, uploads it and stores it in the index. v.seq is left
// alone so the next refresh still sees other devices' changes in between.
func (v *Vault) put(ctx context.Context, kind, uuid string, raw map[string]any, now time.Time) error {
	plain, err := json.Marshal(raw)
	if err != nil {
		return err
	}
	encrypt, index := v.sess.EncryptEntry, v.entries
	if kind == "groups" {
		encrypt, index = v.sess.EncryptGroup, v.groups
	}
	blob, err := encrypt(plain)
	if err != nil {
		return err
	}
	if _, err := v.api.Put(ctx, v.dbID, kind, uuid, blob, now); err != nil {
		return err
	}
	index[uuid] = raw
	return nil
}

// parentOf returns the effective parent group: "" (root) when unset or unknown.
func (v *Vault) parentOf(obj map[string]any) string {
	p, _ := obj["parent_group"].(string)
	if _, ok := v.groups[p]; !ok {
		return ""
	}
	return p
}

func (v *Vault) entry(id string, raw map[string]any) Entry {
	strs, _ := raw["strings"].(map[string]any)
	get := func(k string) string {
		s, _ := strs[k].(map[string]any)
		val, _ := s["v"].(string)
		return val
	}
	e := Entry{UUID: id, Title: get("Title"), Username: get("UserName"), Password: get("Password"),
		URL: get("URL"), Notes: get("Notes"), Props: map[string]string{}}
	for k := range strs {
		if !standardKeys[k] {
			e.Props[k] = get(k)
		}
	}

	names := []string{e.Title}
	seen := map[string]bool{}
	for g := v.parentOf(raw); g != "" && !seen[g]; g = v.parentOf(v.groups[g]) {
		seen[g] = true // ponytail: a parent cycle just stops the walk; the partial chain hangs off root
		name, _ := v.groups[g]["name"].(string)
		names = append(names, name)
	}
	for i, j := 0, len(names)-1; i < j; i, j = i+1, j-1 {
		names[i], names[j] = names[j], names[i]
	}
	e.Path = "/" + strings.Join(names, "/")

	if t, _ := raw["times"].(map[string]any); t["expires"] == true {
		at, _ := t["expires_at"].(string)
		if ts, err := time.Parse(time.RFC3339Nano, at); err == nil && ts.Before(time.Now()) {
			e.Expired = true
		}
	}

	bins, _ := raw["binaries"].([]any)
	for _, b := range bins {
		m, _ := b.(map[string]any)
		name, _ := m["name"].(string)
		data, _ := m["data"].(string)
		dec, err := base64.StdEncoding.DecodeString(data)
		if err != nil {
			continue
		}
		e.Attachments = append(e.Attachments, Attachment{Name: name, Data: dec})
	}
	return e
}

func newTimes(now time.Time) map[string]any {
	ts := now.Format(time.RFC3339Nano)
	return map[string]any{"created": ts, "modified": ts, "accessed": ts, "location_changed": ts, "expires": false, "usage_count": 0}
}

func clone(m map[string]any) (map[string]any, error) {
	b, err := json.Marshal(m)
	if err != nil {
		return nil, err
	}
	var out map[string]any
	return out, json.Unmarshal(b, &out)
}

func newUUID() (string, error) {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		return "", err
	}
	b[6] = b[6]&0x0f | 0x40
	b[8] = b[8]&0x3f | 0x80
	return fmt.Sprintf("%x-%x-%x-%x-%x", b[0:4], b[4:6], b[6:8], b[8:10], b[10:16]), nil
}
