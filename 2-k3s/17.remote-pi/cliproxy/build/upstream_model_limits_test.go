package cliproxy

import (
	"context"
	"io"
	"net/http"
	"strings"
	"testing"
	"time"

	coreauth "github.com/router-for-me/CLIProxyAPI/v7/sdk/cliproxy/auth"
	"github.com/router-for-me/CLIProxyAPI/v7/sdk/config"
)

func TestUpstreamLimitsPaginationAndProviderFields(t *testing.T) {
	calls := 0
	limits, err := fetchUpstreamLimits(context.Background(), "https://example.com/models", func(req *http.Request) (*http.Response, error) {
		calls++
		body := `{"data":[{"id":"claude-new","max_input_tokens":456000,"max_tokens":32000}],"has_more":true,"last_id":"claude-new"}`
		if calls == 2 {
			if req.URL.Query().Get("after_id") != "claude-new" {
				t.Fatal("missing cursor")
			}
			body = `{"data":[{"id":"vendor/new","context_length":900000,"top_provider":{"context_length":700000,"max_completion_tokens":42000}}]}`
		}
		return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader(body))}, nil
	})
	if err != nil {
		t.Fatal(err)
	}
	if calls != 2 || limits["claude-new"] != (upstreamModelLimit{456000, 32000}) || limits["vendor/new"] != (upstreamModelLimit{700000, 42000}) {
		t.Fatalf("calls=%d limits=%v", calls, limits)
	}
}

func TestUpstreamLimitsRejectsPartialCatalog(t *testing.T) {
	for _, body := range []string{`{}`, `{"data":[{"id":"x","max_input_tokens":123}],"has_more":true}`, `{"data":[{"id":"x","max_input_tokens":-1}]}`, `{broken`} {
		limits, err := fetchUpstreamLimits(context.Background(), "https://example.com/models", func(*http.Request) (*http.Response, error) {
			return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader(body))}, nil
		})
		if err == nil || limits != nil {
			t.Fatalf("accepted invalid catalog %s", body)
		}
	}
}

func TestUpstreamLimitsCachedAliasesAndOverrides(t *testing.T) {
	s := &Service{}
	auth := &coreauth.Auth{ID: "account", Provider: "openrouter"}
	s.upstreamLimits.Store("account|https://openrouter.ai/api/v1/models", &upstreamLimitCache{next: time.Now().Add(time.Hour), limits: map[string]upstreamModelLimit{"vendor/new": {700000, 42000}}})
	compat := &config.OpenAICompatibility{BaseURL: "https://openrouter.ai/api/v1", Models: []config.OpenAICompatibilityModel{{Name: "vendor/new", Alias: "friendly"}}}
	models := []*ModelInfo{{ID: "friendly", ContextLength: 123, MaxCompletionTokens: 456}}
	got := s.applyUpstreamLimits(context.Background(), auth, models, compat)
	if got[0].MaxContextLength != 700000 || got[0].MaxCompletionTokens != 42000 {
		t.Fatalf("limits=%+v", got[0])
	}
	if models[0].ContextLength != 123 {
		t.Fatal("mutated shared catalog")
	}
	compat.Models[0].MaxCompletionTokens = 500
	models[0].MaxContextLength = 600
	got = s.applyUpstreamLimits(context.Background(), auth, models, compat)
	if got[0].MaxContextLength != 600 || got[0].MaxCompletionTokens != 456 {
		t.Fatal("overwrote configured limits")
	}
}
