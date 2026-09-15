package cliproxy

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"time"

	"github.com/router-for-me/CLIProxyAPI/v7/internal/runtime/executor"
	coreauth "github.com/router-for-me/CLIProxyAPI/v7/sdk/cliproxy/auth"
	"github.com/router-for-me/CLIProxyAPI/v7/sdk/config"
	log "github.com/sirupsen/logrus"
)

type upstreamModelLimit struct {
	Context int
	Output  int
}

type upstreamLimitCache struct {
	mu     sync.Mutex
	next   time.Time
	limits map[string]upstreamModelLimit
}

// applyUpstreamLimits runs before aliases and prefixes. Codex already has its
// own refreshable subscription template catalog; never substitute API limits.
func (s *Service) applyUpstreamLimits(ctx context.Context, auth *coreauth.Auth, models []*ModelInfo, compat *config.OpenAICompatibility) []*ModelInfo {
	if auth == nil || auth.ID == "" {
		return models
	}
	s.cfgMu.RLock()
	cfg := s.cfg
	s.cfgMu.RUnlock()
	var endpoint string
	var request func(context.Context, *coreauth.Auth, *http.Request) (*http.Response, error)
	if compat != nil {
		base, err := url.Parse(compat.BaseURL)
		if err != nil || base.Scheme != "https" || base.Host != "openrouter.ai" || strings.TrimRight(base.Path, "/") != "/api/v1" {
			return models
		}
		endpoint = "https://openrouter.ai/api/v1/models"
		request = executor.NewOpenAICompatExecutor(auth.Provider, cfg).HttpRequest
	} else if auth.Provider == "claude" {
		if base := strings.TrimRight(auth.Attributes["base_url"], "/"); base != "" && base != "https://api.anthropic.com" {
			return models
		}
		endpoint = "https://api.anthropic.com/v1/models?limit=1000"
		request = executor.NewClaudeExecutor(cfg).HttpRequest
	} else {
		return models
	}
	key := auth.ID + "|" + endpoint
	value, _ := s.upstreamLimits.LoadOrStore(key, &upstreamLimitCache{})
	cache := value.(*upstreamLimitCache)
	cache.mu.Lock()
	defer cache.mu.Unlock()
	if time.Now().After(cache.next) {
		limits, err := fetchUpstreamLimits(ctx, endpoint, func(req *http.Request) (*http.Response, error) {
			if compat == nil {
				req.Header.Set("anthropic-version", "2023-06-01")
				if auth.AuthKind() != "apikey" {
					req.Header.Set("anthropic-beta", "oauth-2025-04-20")
				}
			}
			return request(ctx, auth, req)
		})
		cache.next = time.Now().Add(5 * time.Minute)
		if err != nil {
			// Do not log response bodies or credential-bearing request errors.
			log.Warnf("upstream model limits: provider=%s refresh failed; keeping last successful limits", auth.Provider)
		} else {
			cache.limits = limits
			cache.next = time.Now().Add(2 * time.Hour)
			log.Infof("upstream model limits: provider=%s refreshed models=%d", auth.Provider, len(limits))
		}
	}
	out := make([]*ModelInfo, 0, len(models))
	for _, model := range models {
		if model == nil {
			continue
		}
		clone := *model
		id := model.ID
		outputOverride := 0
		if compat != nil {
			for _, configured := range compat.Models {
				alias := configured.Alias
				if alias == "" {
					alias = configured.Name
				}
				if alias == id {
					id = configured.Name
					outputOverride = configured.MaxCompletionTokens
					break
				}
			}
		}
		limit := cache.limits[id]
		if limit.Context > 0 && clone.MaxContextLength == 0 {
			clone.ContextLength = limit.Context
			clone.MaxContextLength = limit.Context
		}
		if limit.Output > 0 && outputOverride == 0 {
			clone.MaxCompletionTokens = limit.Output
		}
		out = append(out, &clone)
	}
	return out
}

func fetchUpstreamLimits(ctx context.Context, endpoint string, request func(*http.Request) (*http.Response, error)) (map[string]upstreamModelLimit, error) {
	limits := make(map[string]upstreamModelLimit)
	seen := make(map[string]bool)
	for endpoint != "" {
		if seen[endpoint] {
			return nil, fmt.Errorf("repeated model cursor")
		}
		seen[endpoint] = true
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint, nil)
		if err != nil {
			return nil, err
		}
		resp, err := request(req)
		if err != nil {
			return nil, err
		}
		body, readErr := io.ReadAll(io.LimitReader(resp.Body, 8<<20))
		if errClose := resp.Body.Close(); errClose != nil {
			log.Debug("upstream model limits: response body close failed")
		}
		if resp.StatusCode != http.StatusOK {
			return nil, fmt.Errorf("model catalog HTTP %d", resp.StatusCode)
		}
		if readErr != nil {
			return nil, readErr
		}
		var page struct {
			Data []struct {
				ID      string `json:"id"`
				Input   int    `json:"max_input_tokens"`
				Output  int    `json:"max_tokens"`
				Context int    `json:"context_length"`
				Top     struct {
					Context int `json:"context_length"`
					Output  int `json:"max_completion_tokens"`
				} `json:"top_provider"`
			} `json:"data"`
			More bool   `json:"has_more"`
			Last string `json:"last_id"`
		}
		if err := json.Unmarshal(body, &page); err != nil {
			return nil, err
		}
		for _, model := range page.Data {
			limit := upstreamModelLimit{Context: model.Input, Output: model.Output}
			if model.Context > 0 {
				limit.Context = model.Context
				limit.Output = model.Top.Output
			}
			if model.Top.Context > 0 && model.Top.Context < limit.Context {
				limit.Context = model.Top.Context
			}
			if model.ID != "" && (limit.Context > 0 || limit.Output > 0) {
				limits[model.ID] = limit
			}
		}
		if !page.More {
			break
		}
		if page.Last == "" {
			return nil, fmt.Errorf("missing model cursor")
		}
		u := req.URL
		q := u.Query()
		q.Set("after_id", page.Last)
		u.RawQuery = q.Encode()
		endpoint = u.String()
	}
	if len(limits) == 0 {
		return nil, fmt.Errorf("empty model limits")
	}
	return limits, nil
}
