// Command devai-auth-bff is the GIP-backed auth + reverse-proxy BFF for
// the DevAI platform.
//
// Serves up to three audiences:
//
//   - devai.tesserix.app (ALM dashboard) → mints a session against the
//     DEVAI_BFF_ALM_TENANT_ID GIP tenant pool.
//   - sre.tesserix.app (SRE dashboard) → mints a session against the
//     DEVAI_BFF_SRE_TENANT_ID GIP tenant pool.
//   - kagent + aregistry (opt-in) → reverse-proxies to the matching
//     upstream service after the session cookie is verified. Disabled
//     by default: both services are internal-only and devai-api dials
//     them over cluster DNS. Enable by setting all four of
//     DEVAI_BFF_{KAGENT,AREGISTRY}_{HOST,UPSTREAM_URL}.
//
// Configuration via DEVAI_BFF_* env vars (see internal/config).
package main

import (
	"context"
	"log"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/tesserix/devai/services/auth-bff/internal/allowlist"
	"github.com/tesserix/devai/services/auth-bff/internal/config"
	"github.com/tesserix/devai/services/auth-bff/internal/gip"
	"github.com/tesserix/devai/services/auth-bff/internal/handlers"
	"github.com/tesserix/devai/services/auth-bff/internal/proxy"
	"github.com/tesserix/devai/services/auth-bff/internal/session"
)

func main() {
	logger := log.New(os.Stdout, "auth-bff ", log.LstdFlags|log.Lmicroseconds)

	cfg, err := config.Load()
	if err != nil {
		logger.Fatalf("config: %v", err)
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	verifier, err := gip.New(ctx, gip.Config{ProjectID: cfg.GCPProjectID})
	if err != nil {
		logger.Fatalf("gip: %v", err)
	}

	sessions, err := session.NewManager(session.Config{
		CookieName: cfg.SessionCookieName,
		Domain:     cfg.SessionDomain,
		Secure:     cfg.SessionSecure,
		MaxAge:     cfg.SessionMaxAge,
		EncryptKey: cfg.SessionEncryptKey,
	})
	if err != nil {
		logger.Fatalf("session: %v", err)
	}

	allowed := allowlist.Parse(cfg.AdminAllowedEmails)
	logger.Printf("loaded %d allow-listed admin emails", allowed.Size())

	authHandler := handlers.NewAuthHandler(handlers.AuthDeps{
		Verifier:      verifier,
		Session:       sessions,
		Allowlist:     allowed,
		PublicSignup:  cfg.PublicSignup,
		ALMTenant:     cfg.ALMTenantID,
		SRETenant:     cfg.SRETenantID,
		GIPWebAPIKey:  cfg.GIPWebAPIKey,
		GIPAuthDomain: cfg.GIPAuthDomain,
		GIPProjectID:  cfg.GCPProjectID,
		ALMHosts:      cfg.ALMHosts,
		SREHosts:      cfg.SREHosts,
	})

	proxyHandler, err := proxy.New(proxy.Config{
		Session:           sessions,
		KagentUpstream:    cfg.KagentUpstreamURL,
		AregistryUpstream: cfg.AregistryUpstreamURL,
		KagentHost:        cfg.KagentHost,
		AregistryHost:     cfg.AregistryHost,
		PreviewDomain:     cfg.PreviewDomain,
		PreviewNamespace:  cfg.PreviewNamespace,
		SharedSecret:      cfg.SharedSecret,
	})
	if err != nil {
		logger.Fatalf("proxy: %v", err)
	}
	// Surface the reverse-proxy state at boot so the disabled-by-default
	// posture is obvious in pod logs (no public exposure of kagent /
	// aregistry unless an operator explicitly opts in).
	switch {
	case cfg.KagentHost == "" && cfg.AregistryHost == "":
		logger.Printf("agentic reverse-proxy: disabled (kagent + aregistry are internal-only)")
	case cfg.KagentHost != "" && cfg.AregistryHost != "":
		logger.Printf("agentic reverse-proxy: enabled (kagent=%s, aregistry=%s)", cfg.KagentHost, cfg.AregistryHost)
	default:
		logger.Printf("agentic reverse-proxy: partial (kagent=%q, aregistry=%q)", cfg.KagentHost, cfg.AregistryHost)
	}
	// Surface (never the value of) the X-Auth-Bff-Secret posture so the
	// spoofing-defense state is obvious in pod logs. Unset = legacy behavior
	// (header not stamped); the upstream fails closed only with its own secret.
	if cfg.SharedSecret != "" {
		logger.Printf("X-Auth-Bff-Secret: enabled (stamped on proxied identity)")
	} else {
		logger.Printf("X-Auth-Bff-Secret: unset (forwarded identity unsigned — set DEVAI_AUTH_BFF_SHARED_SECRET to enable)")
	}

	// One mux. Auth + health routes for everything; everything else falls
	// through to the proxy handler which routes by Host header.
	mux := http.NewServeMux()
	authHandler.Register(mux)
	mux.Handle("/", proxyHandler)

	srv := &http.Server{
		Addr:              cfg.HTTPPort,
		Handler:           withRequestLog(logger, mux),
		ReadHeaderTimeout: 10 * time.Second,
		ReadTimeout:       30 * time.Second,
		WriteTimeout:      60 * time.Second,
		IdleTimeout:       120 * time.Second,
	}

	go func() {
		logger.Printf("listening on %s (project=%s, alm=%s, sre=%s)",
			cfg.HTTPPort, cfg.GCPProjectID, cfg.ALMTenantID, cfg.SRETenantID)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			logger.Fatalf("listen: %v", err)
		}
	}()

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGINT, syscall.SIGTERM)
	<-stop

	logger.Printf("shutdown signal received")
	shutdownCtx, sc := context.WithTimeout(context.Background(), 15*time.Second)
	defer sc()
	if err := srv.Shutdown(shutdownCtx); err != nil {
		logger.Printf("shutdown error: %v", err)
	}
}

// withRequestLog logs one line per request — enough for production triage,
// not verbose enough to dominate stdout.
func withRequestLog(l *log.Logger, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		sr := &statusRecorder{ResponseWriter: w, code: 200}
		next.ServeHTTP(sr, r)
		path := r.URL.Path
		if strings.HasPrefix(path, "/_next/") || path == "/favicon.ico" {
			return // skip static asset noise
		}
		l.Printf("%s %s %d %s %s", r.Method, path, sr.code, time.Since(start), r.Host)
	})
}

type statusRecorder struct {
	http.ResponseWriter
	code int
}

func (s *statusRecorder) WriteHeader(c int) {
	s.code = c
	s.ResponseWriter.WriteHeader(c)
}
