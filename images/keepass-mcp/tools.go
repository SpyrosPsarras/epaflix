package main

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"sort"
	"strings"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// readFreshness is how old the index may be before a tool call pulls /changes.
const readFreshness = 3 * time.Second

// device names this client in the write log; DeltaSync enrolls it as t3code.
const device = "t3code"

const expiredNote = "entry is expired; password withheld. Update the entry's expiry in KeePassXC, then retry."

type tools struct {
	v   *Vault
	log *slog.Logger
}

type listIn struct {
	Prefix string `json:"prefix,omitempty"`
}

type getIn struct {
	Path            string `json:"path"`
	IncludePassword *bool  `json:"include_password,omitempty"`
	UUID            string `json:"uuid,omitempty"`
}

type addIn struct {
	Path     string             `json:"path"`
	Username string             `json:"username,omitempty"`
	Password string             `json:"password,omitempty"`
	URL      string             `json:"url,omitempty"`
	Notes    string             `json:"notes,omitempty"`
	Props    *map[string]string `json:"props,omitempty"`
}

type updateIn struct {
	Path     string              `json:"path"`
	Title    *string             `json:"title,omitempty"`
	Username *string             `json:"username,omitempty"`
	Password *string             `json:"password,omitempty"`
	URL      *string             `json:"url,omitempty"`
	Notes    *string             `json:"notes,omitempty"`
	Props    *map[string]*string `json:"props,omitempty"`
	UUID     string              `json:"uuid,omitempty"`
}

type trashIn struct {
	Path string `json:"path"`
	UUID string `json:"uuid,omitempty"`
}

type attachIn struct {
	Path       string `json:"path"`
	Filename   string `json:"filename"`
	ContentB64 string `json:"content_b64"`
}

type attachmentIn struct {
	Path     string `json:"path"`
	Filename string `json:"filename"`
}

const uuidHint = " Optional uuid overrides path (use it for untitled entries or paths shared by several entries)."

// newServer registers the seven vault tools. Names, argument shapes and
// descriptions follow 2-k3s/15.syncthing/files/keepass_mcp.py.
func newServer(v *Vault, log *slog.Logger) *mcp.Server {
	t := &tools{v: v, log: log}
	s := mcp.NewServer(&mcp.Implementation{Name: "keepass", Version: "1.0.0"}, &mcp.ServerOptions{
		Instructions: "Read-write access to the personal KeePass vault, synced through DeltaSync. Writes are serialized in the keepass pod and refresh from the server first, so edits from other devices are kept.",
	})
	mcp.AddTool(s, &mcp.Tool{Name: "vault_list", Description: "List vault entries below the given group path prefix (no passwords). Empty prefix lists all. Returns a JSON array."},
		func(ctx context.Context, _ *mcp.CallToolRequest, in listIn) (*mcp.CallToolResult, any, error) {
			return reply(t.list(ctx, in))
		})
	mcp.AddTool(s, &mcp.Tool{Name: "vault_get", Description: "Fetch one entry by its full vault path (as returned by vault_list, e.g. /Group/Title). Returns a JSON object." + uuidHint},
		func(ctx context.Context, _ *mcp.CallToolRequest, in getIn) (*mcp.CallToolResult, any, error) {
			return reply(t.get(ctx, in))
		})
	mcp.AddTool(s, &mcp.Tool{Name: "vault_add", Description: "Create an entry at the full vault path (e.g. /Group/Title); missing groups are created. props sets custom string properties. Returns the new entry summary."},
		func(ctx context.Context, _ *mcp.CallToolRequest, in addIn) (*mcp.CallToolResult, any, error) {
			return reply(t.add(ctx, in))
		})
	mcp.AddTool(s, &mcp.Tool{Name: "vault_update", Description: "Update fields of the entry at the full vault path. Fields left as null are unchanged; empty string clears. props sets custom properties (null value deletes one). Returns the entry summary." + uuidHint},
		func(ctx context.Context, _ *mcp.CallToolRequest, in updateIn) (*mcp.CallToolResult, any, error) {
			return reply(t.update(ctx, in))
		})
	// Deliberately not the Python text: trash here is a sync tombstone, there is no recycle bin to recover from.
	mcp.AddTool(s, &mcp.Tool{Name: "vault_trash", Description: "Delete the entry at the full vault path. The deletion syncs to every device; there is no recycle bin. Returns a confirmation object." + uuidHint},
		func(ctx context.Context, _ *mcp.CallToolRequest, in trashIn) (*mcp.CallToolResult, any, error) {
			return reply(t.trash(ctx, in))
		})
	mcp.AddTool(s, &mcp.Tool{Name: "vault_attach", Description: "Store a base64-encoded file (e.g. an SSH private key) as an attachment on the entry at the full vault path. Replaces an existing attachment with the same name. Returns a confirmation object."},
		func(ctx context.Context, _ *mcp.CallToolRequest, in attachIn) (*mcp.CallToolResult, any, error) {
			return reply(t.attach(ctx, in))
		})
	mcp.AddTool(s, &mcp.Tool{Name: "vault_attachment", Description: "Fetch an attachment by name from the entry at the full vault path. Returns {filename, content_b64}. Use vault_get to list attachment names."},
		func(ctx context.Context, _ *mcp.CallToolRequest, in attachmentIn) (*mcp.CallToolResult, any, error) {
			return reply(t.attachment(ctx, in))
		})
	return s
}

// reply JSON-encodes a result as the tool's text content, like the Python server.
func reply(v any, err error) (*mcp.CallToolResult, any, error) {
	if err != nil {
		return nil, nil, err
	}
	b, err := json.Marshal(v)
	if err != nil {
		return nil, nil, err
	}
	return &mcp.CallToolResult{Content: []mcp.Content{&mcp.TextContent{Text: string(b)}}}, nil, nil
}

// fresh pulls /changes unless the index is younger than readFreshness.
func (t *tools) fresh(ctx context.Context) error {
	if time.Since(t.v.LastSync()) > readFreshness {
		return t.v.Refresh(ctx)
	}
	return nil
}

func (t *tools) find(ctx context.Context, path, uuid string) (Entry, error) {
	if err := t.fresh(ctx); err != nil {
		return Entry{}, err
	}
	return t.v.Find(path, strings.ToLower(strings.TrimSpace(uuid)))
}

// logWrite is the one line per write: tool, entry UUID, result. Never a value.
func (t *tools) logWrite(tool, uuid string, err error) {
	result := "ok"
	if err != nil {
		result = err.Error()
	}
	t.log.Info("write", "tool", tool, "uuid", uuid, "device", device, "result", result)
}

func summary(e Entry) map[string]any {
	names := make([]string, len(e.Attachments))
	for i, a := range e.Attachments {
		names[i] = a.Name
	}
	sort.Strings(names)
	return map[string]any{"path": e.Path, "title": e.Title, "expired": e.Expired, "username": e.Username,
		"url": e.URL, "notes": e.Notes, "attachments": names}
}

func (t *tools) list(ctx context.Context, in listIn) (any, error) {
	if err := t.fresh(ctx); err != nil {
		return nil, err
	}
	prefix := strings.Trim(in.Prefix, "/")
	out := []map[string]any{}
	for _, e := range t.v.Entries() {
		if strings.HasPrefix(strings.TrimLeft(e.Path, "/"), prefix) {
			out = append(out, summary(e))
		}
	}
	return out, nil
}

func (t *tools) get(ctx context.Context, in getIn) (any, error) {
	e, err := t.find(ctx, in.Path, in.UUID)
	if err != nil {
		return nil, err
	}
	out := summary(e)
	props := e.Props
	if e.Expired {
		props = map[string]string{}
		for k, val := range e.Props {
			if !e.ProtectedProps[k] {
				props[k] = val
			}
		}
	}
	out["custom_properties"] = props
	switch {
	case e.Expired:
		out["password"] = nil
		out["note"] = expiredNote
	case in.IncludePassword == nil || *in.IncludePassword:
		out["password"] = e.Password
	}
	return out, nil
}

// deref treats a null props argument as empty.
func deref[V any](m *map[string]V) map[string]V {
	if m == nil {
		return nil
	}
	return *m
}

func checkProps[V any](props map[string]V) error {
	for k := range props {
		if k == "" || standardKeys[k] {
			return fmt.Errorf("props key %q is reserved; use the dedicated argument", k)
		}
	}
	return nil
}

func (t *tools) add(ctx context.Context, in addIn) (any, error) {
	e, err := t.addEntry(ctx, in)
	t.logWrite("vault_add", e.UUID, err)
	if err != nil {
		return nil, err
	}
	return summary(e), nil
}

func (t *tools) addEntry(ctx context.Context, in addIn) (Entry, error) {
	props := deref(in.Props)
	if err := checkProps(props); err != nil {
		return Entry{}, err
	}
	fields := map[string]String{"UserName": {V: in.Username}, "Password": {V: in.Password}, "URL": {V: in.URL}, "Notes": {V: in.Notes}}
	for k, val := range props {
		fields[k] = String{V: val}
	}
	return t.v.Add(ctx, in.Path, fields)
}

func (t *tools) update(ctx context.Context, in updateIn) (any, error) {
	e, err := t.updateEntry(ctx, in)
	t.logWrite("vault_update", e.UUID, err)
	if err != nil {
		return nil, err
	}
	return summary(e), nil
}

// updateEntry returns the entry (UUID set as soon as it is known) after the write.
func (t *tools) updateEntry(ctx context.Context, in updateIn) (Entry, error) {
	props := deref(in.Props)
	if err := checkProps(props); err != nil {
		return Entry{}, err
	}
	e, err := t.find(ctx, in.Path, in.UUID)
	if err != nil {
		return Entry{}, err
	}
	err = t.v.Write(ctx, e.UUID, func(raw map[string]any) error {
		strs := stringsOf(raw)
		for k, val := range map[string]*string{"Title": in.Title, "UserName": in.Username, "Password": in.Password, "URL": in.URL, "Notes": in.Notes} {
			if val != nil {
				setString(strs, k, *val)
			}
		}
		for k, val := range props {
			if val == nil {
				delete(strs, k)
			} else {
				setString(strs, k, *val)
			}
		}
		return nil
	})
	if err != nil {
		return Entry{UUID: e.UUID}, err
	}
	updated, err := t.v.Find("", e.UUID)
	if err != nil {
		return Entry{UUID: e.UUID}, err
	}
	return updated, nil
}

func (t *tools) trash(ctx context.Context, in trashIn) (any, error) {
	e, err := t.find(ctx, in.Path, in.UUID)
	if err == nil {
		err = t.v.Trash(ctx, e.UUID)
	}
	t.logWrite("vault_trash", e.UUID, err)
	if err != nil {
		return nil, err
	}
	return map[string]string{"trashed": strings.Trim(e.Path, "/")}, nil
}

func (t *tools) attach(ctx context.Context, in attachIn) (any, error) {
	data, err := base64.StdEncoding.DecodeString(in.ContentB64)
	if err == nil && len(data) == 0 {
		err = errors.New("content_b64 is empty")
	}
	if err != nil {
		return nil, err
	}
	e, err := t.find(ctx, in.Path, "")
	if err == nil {
		err = t.v.Write(ctx, e.UUID, func(raw map[string]any) error {
			bins := []any{}
			old, _ := raw["binaries"].([]any)
			for _, b := range old {
				if m, _ := b.(map[string]any); m["name"] != in.Filename {
					bins = append(bins, b)
				}
			}
			raw["binaries"] = append(bins, map[string]any{"name": in.Filename, "data": base64.StdEncoding.EncodeToString(data)})
			return nil
		})
	}
	t.logWrite("vault_attach", e.UUID, err)
	if err != nil {
		return nil, err
	}
	return map[string]any{"attached": in.Filename, "entry": strings.Trim(in.Path, "/"), "bytes": len(data)}, nil
}

func (t *tools) attachment(ctx context.Context, in attachmentIn) (any, error) {
	e, err := t.find(ctx, in.Path, "")
	if err != nil {
		return nil, err
	}
	if e.Expired {
		return nil, errors.New(expiredNote)
	}
	for _, a := range e.Attachments {
		if a.Name == in.Filename {
			return map[string]string{"filename": in.Filename, "entry": strings.Trim(in.Path, "/"), "content_b64": base64.StdEncoding.EncodeToString(a.Data)}, nil
		}
	}
	return nil, fmt.Errorf("no attachment '%s' on '%s' (see 'attachments' in vault_get)", in.Filename, in.Path)
}

// stringsOf returns raw["strings"], creating it when missing.
func stringsOf(raw map[string]any) map[string]any {
	strs, _ := raw["strings"].(map[string]any)
	if strs == nil {
		strs = map[string]any{}
		raw["strings"] = strs
	}
	return strs
}

// setString replaces a string field's value and keeps its protected flag.
// Password is always protected.
func setString(strs map[string]any, key, val string) {
	old, _ := strs[key].(map[string]any)
	prot, _ := old["protected"].(bool)
	strs[key] = map[string]any{"v": val, "protected": prot || key == "Password"}
}
