// keepass-mcp serves a DeltaSync-backed KeePass vault as MCP tools.
//
//	keepass-mcp [serve]          MCP over HTTP at /keepass, health at /healthz
//	keepass-mcp enroll ...       trade an enrollment token for a device token
//	keepass-mcp healthcheck      exit 0 when /healthz answers 200 (for exec probes)
package main

import (
	"context"
	"crypto/subtle"
	"encoding/base64"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"
	"gitlab.com/Star95/keepass-deltasync/client/mobile"
)

const (
	defaultURL = "http://deltasync.deltasync.svc"
	// healthStale is how old the last successful /changes may be before /healthz fails.
	healthStale = 5 * time.Minute
	// pollEvery keeps lastSync fresh while no tool is called.
	pollEvery = time.Minute
)

func main() {
	cmd, args := "serve", []string{}
	if len(os.Args) > 1 {
		cmd, args = os.Args[1], os.Args[2:]
	}
	var err error
	switch cmd {
	case "serve":
		err = serve()
	case "enroll":
		err = runEnroll(context.Background(), os.Stdout, args)
	case "healthcheck":
		err = healthcheck()
	default:
		err = fmt.Errorf("unknown command %q (serve, enroll, healthcheck)", cmd)
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, "keepass-mcp:", err)
		os.Exit(1)
	}
}

func env(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func serve() error {
	var missing []string
	need := func(key string) string {
		v := os.Getenv(key)
		if v == "" {
			missing = append(missing, key)
		}
		return v
	}
	token, passphrase, secret := need("DELTASYNC_DEVICE_TOKEN"), need("KEEPASS_PASSPHRASE"), need("KEEPASS_HUB_SECRET")
	if len(missing) > 0 {
		return fmt.Errorf("missing env: %s", strings.Join(missing, ", "))
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	log := slog.New(slog.NewJSONHandler(os.Stderr, nil))
	slog.SetDefault(log) // the vault logs unreadable objects through the default logger

	api := &API{Base: strings.TrimRight(env("DELTASYNC_URL", defaultURL), "/"), Token: token, HTTP: &http.Client{Timeout: 30 * time.Second}}
	dbName := env("DELTASYNC_DATABASE", "passwords")
	dbID, err := api.DatabaseID(ctx, dbName)
	if err != nil {
		return err
	}
	v, err := Open(ctx, api, dbID, []byte(passphrase))
	if err != nil {
		return err
	}

	go func() {
		for tick := time.NewTicker(pollEvery); ; {
			select {
			case <-ctx.Done():
				return
			case <-tick.C:
				if err := v.Refresh(ctx); err != nil {
					log.Error("refresh failed", "err", err.Error())
				}
			}
		}
	}()

	srv := &http.Server{Addr: env("LISTEN", ":8000"), Handler: newHandler(v, secret, log), ReadHeaderTimeout: 10 * time.Second}
	ln, err := net.Listen("tcp", srv.Addr)
	if err != nil {
		return err
	}
	log.Info("serving", "addr", srv.Addr, "database", dbName, "entries", len(v.Entries()))
	return run(ctx, srv, ln)
}

// run serves srv on ln until ctx ends, then drains in-flight requests
// (up to 5 s) before it returns.
func run(ctx context.Context, srv *http.Server, ln net.Listener) error {
	drained := make(chan struct{})
	go func() {
		defer close(drained)
		<-ctx.Done()
		shutdown, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		srv.Shutdown(shutdown)
	}()
	if err := srv.Serve(ln); !errors.Is(err, http.ErrServerClosed) {
		return err
	}
	<-drained
	return nil
}

// newHandler mounts the MCP endpoint behind the hub secret and an open /healthz.
func newHandler(v *Vault, secret string, log *slog.Logger) http.Handler {
	server := newServer(v, log)
	// Stateless like the Python server (stateless_http, json_response): no
	// sessions to lose on a pod restart or to pile up.
	mcpHandler := mcp.NewStreamableHTTPHandler(func(*http.Request) *mcp.Server { return server },
		&mcp.StreamableHTTPOptions{Stateless: true, JSONResponse: true})
	mux := http.NewServeMux()
	mux.Handle("/keepass", http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if secret == "" || subtle.ConstantTimeCompare([]byte(r.Header.Get("X-Hub-Secret")), []byte(secret)) != 1 {
			http.Error(w, "unauthorized", http.StatusUnauthorized)
			return
		}
		mcpHandler.ServeHTTP(w, r)
	}))
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		if age := time.Since(v.LastSync()); age > healthStale {
			http.Error(w, fmt.Sprintf("last sync %s ago", age.Round(time.Second)), http.StatusServiceUnavailable)
			return
		}
		io.WriteString(w, "ok\n")
	})
	return mux
}

// healthcheck lets a kubelet exec probe work in the shell-less image.
func healthcheck() error {
	host, port, err := net.SplitHostPort(env("LISTEN", ":8000"))
	if err != nil {
		return err
	}
	if host == "" || host == "0.0.0.0" || host == "::" {
		host = "127.0.0.1"
	}
	resp, err := (&http.Client{Timeout: 5 * time.Second}).Get("http://" + net.JoinHostPort(host, port) + "/healthz")
	if err != nil {
		return err
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("healthz: %s", resp.Status)
	}
	return nil
}

// runEnroll trades an enrollment token for a device token. It prints
// {device_token, private_key_b64} to w and writes no files.
func runEnroll(ctx context.Context, w io.Writer, args []string) error {
	fs := flag.NewFlagSet("enroll", flag.ContinueOnError)
	server := fs.String("server", env("DELTASYNC_URL", defaultURL), "DeltaSync base URL")
	name := fs.String("name", "", "device name")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if fs.NArg() != 1 {
		return errors.New("usage: enroll [--server URL] [--name NAME] <enrollment-token>")
	}
	kp, err := mobile.GenerateDeviceKeypair()
	if err != nil {
		return err
	}
	body := map[string]string{"public_key": base64.StdEncoding.EncodeToString(kp.PublicKey)}
	if *name != "" {
		body["device_name"] = *name
	}
	api := &API{Base: strings.TrimRight(*server, "/"), Token: fs.Arg(0), HTTP: &http.Client{Timeout: 30 * time.Second}}
	var out struct {
		Token string `json:"token"`
	}
	if err := api.do(ctx, http.MethodPost, "/api/v1/devices/enroll", body, &out); err != nil {
		return err
	}
	if out.Token == "" {
		return errors.New("server returned an empty device token")
	}
	return json.NewEncoder(w).Encode(map[string]string{
		"device_token":    out.Token,
		"private_key_b64": base64.StdEncoding.EncodeToString(kp.PrivateKey),
	})
}
