package main

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"time"
)

// API is a minimal DeltaSync HTTP client. Wire shapes mirror upstream
// client/internal/api/client.go (GetChanges, writeObject, ListDatabases).
type API struct {
	Base, Token string
	HTTP        *http.Client
}

// Changes is the GET /changes response.
type Changes struct {
	CurrentSeq int64    `json:"current_seq"`
	Objects    []Change `json:"entries"`
}

// Change is the newest version of one object. Kind 2 is a group; 0 or 1 is an entry.
type Change struct {
	UUID       string `json:"uuid"`
	Blob       string `json:"blob"`
	ModifiedAt string `json:"modified_at"`
	Deleted    bool   `json:"deleted"`
	Seq        int64  `json:"seq"`
	Kind       int    `json:"kind"`
}

const kindGroup = 2

func (a *API) do(ctx context.Context, method, path string, body, out any) error {
	var rd io.Reader
	if body != nil {
		buf, err := json.Marshal(body)
		if err != nil {
			return err
		}
		rd = bytes.NewReader(buf)
	}
	req, err := http.NewRequestWithContext(ctx, method, a.Base+path, rd)
	if err != nil {
		return err
	}
	req.Header.Set("Authorization", "Bearer "+a.Token)
	req.Header.Set("Accept", "application/json")
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	client := a.HTTP
	if client == nil {
		client = http.DefaultClient
	}
	resp, err := client.Do(req)
	if err != nil {
		return fmt.Errorf("%s %s: %w", method, path, err)
	}
	defer resp.Body.Close()
	if resp.StatusCode/100 != 2 {
		msg, _ := io.ReadAll(io.LimitReader(resp.Body, 512))
		return fmt.Errorf("%s %s: %s: %s", method, path, resp.Status, bytes.TrimSpace(msg))
	}
	if err := json.NewDecoder(resp.Body).Decode(out); err != nil {
		return fmt.Errorf("%s %s: decode response: %w", method, path, err)
	}
	return nil
}

// Changes returns every object with server seq > since, groups included.
func (a *API) Changes(ctx context.Context, dbID string, since int64) (Changes, error) {
	var out Changes
	err := a.do(ctx, http.MethodGet, fmt.Sprintf("/api/v1/databases/%s/changes?since=%d&include=groups", dbID, since), nil, &out)
	return out, err
}

// Put uploads a new version. kind is the URL segment: "entries" or "groups".
func (a *API) Put(ctx context.Context, dbID, kind, uuid string, blob []byte, modified time.Time) (int64, error) {
	return a.write(ctx, http.MethodPut, dbID, kind, uuid, blob, modified)
}

// Delete writes a tombstone for the object.
func (a *API) Delete(ctx context.Context, dbID, kind, uuid string, modified time.Time) (int64, error) {
	return a.write(ctx, http.MethodDelete, dbID, kind, uuid, nil, modified)
}

func (a *API) write(ctx context.Context, method, dbID, kind, uuid string, blob []byte, modified time.Time) (int64, error) {
	if kind != "entries" && kind != "groups" {
		return 0, fmt.Errorf("kind must be entries or groups, got %q", kind)
	}
	body := map[string]string{"modified_at": modified.UTC().Format("2006-01-02T15:04:05Z")}
	if blob != nil {
		body["blob"] = base64.StdEncoding.EncodeToString(blob)
	}
	var out struct {
		Entry, Group *struct {
			Seq int64 `json:"seq"`
		}
	}
	if err := a.do(ctx, method, fmt.Sprintf("/api/v1/databases/%s/%s/%s", dbID, kind, uuid), body, &out); err != nil {
		return 0, err
	}
	switch {
	case out.Group != nil:
		return out.Group.Seq, nil
	case out.Entry != nil:
		return out.Entry.Seq, nil
	}
	return 0, fmt.Errorf("%s %s/%s: response missing entry/group object", method, kind, uuid)
}

// DatabaseID resolves a database name to its server UUID.
func (a *API) DatabaseID(ctx context.Context, name string) (string, error) {
	var out struct {
		Databases []struct{ ID, Name string } `json:"databases"`
	}
	if err := a.do(ctx, http.MethodGet, "/api/v1/databases", nil, &out); err != nil {
		return "", err
	}
	for _, d := range out.Databases {
		if d.Name == name {
			return d.ID, nil
		}
	}
	return "", fmt.Errorf("no database named %q on the server", name)
}
