package auth

import (
	"context"
	"testing"
	"time"

	"github.com/router-for-me/CLIProxyAPI/v7/internal/registry"
	cliproxyexecutor "github.com/router-for-me/CLIProxyAPI/v7/sdk/cliproxy/executor"
)

// Reproduce an unrelated OpenRouter model leaving aggregate credential state
// unavailable while GLM itself has no failed model state.
func TestOpenRouterPrefixIncident(t *testing.T) {
	const provider = "openai-compatible-openrouter"
	const model = "or-glm-5.3-flash"
	for _, route := range []string{model, "openrouter/" + model} {
		t.Run(route, func(t *testing.T) {
			manager := NewManager(nil, &RoundRobinSelector{}, nil)
			manager.RegisterExecutor(&openAICompatPoolExecutor{id: provider})
			a := &Auth{ID: "prefix-incident-" + route, Provider: provider, Prefix: "openrouter", Status: StatusActive,
				ModelStates: map[string]*ModelState{"or-minimax-m3:free": {Status: StatusError, Unavailable: true, NextRetryAfter: time.Now().Add(time.Hour)}},
			}
			updateAggregatedAvailability(a, time.Now())
			if !a.Unavailable {
				t.Fatal("precondition: unrelated failure must set aggregate unavailable")
			}
			if _, err := manager.Register(context.Background(), a); err != nil {
				t.Fatal(err)
			}
			reg := registry.GetGlobalRegistry()
			reg.RegisterClient(a.ID, provider, []*registry.ModelInfo{{ID: model}, {ID: "openrouter/" + model}})
			t.Cleanup(func() { reg.UnregisterClient(a.ID) })
			manager.RefreshSchedulerEntry(a.ID)
			stream, err := manager.ExecuteStream(context.Background(), []string{provider}, cliproxyexecutor.Request{Model: route}, cliproxyexecutor.Options{})
			if err != nil {
				t.Fatalf("healthy GLM route rejected: %v", err)
			}
			for chunk := range stream.Chunks {
				if chunk.Err != nil {
					t.Fatal(chunk.Err)
				}
			}
		})
	}
}
