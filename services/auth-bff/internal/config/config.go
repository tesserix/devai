// Package config loads devai-auth-bff settings from environment variables.
//
// Single source of truth for which env vars exist; the Helm chart's
// values.yaml and the deployment template both read from this list.
package config

import (
	"fmt"
	"os"
	"strings"
	"time"
)

// Config holds runtime settings.
type Config struct {
	// HTTP
	HTTPPort string // default ":8090"

	// GIP
	GCPProjectID  string // tesseracthub-480811
	ALMTenantID   string // GIP tenant pool for ALM dashboard
	SRETenantID   string // GIP tenant pool for SRE dashboard
	AgenticTenant string // (optional) tenant for kagent + aregistry reverse-proxy
	// Public Firebase config served to dashboards via /auth/config. These
	// values are technically not secret (Firebase docs explicitly say the
	// web API key is a project identifier, not a credential) — but we
	// avoid checking them into git and source them from GCP Secret
	// Manager via ExternalSecret, so rotation flows through one path.
	GIPWebAPIKey  string
	GIPAuthDomain string // e.g. tesseracthub-480811.firebaseapp.com

	// Hostnames that map onto pools. The /auth/config endpoint reads
	// the incoming Host header and picks the right tenant/pool.
	ALMHosts []string // e.g. devai.tesserix.app, kagent..., aregistry...
	SREHosts []string // e.g. sre.tesserix.app

	// Session
	SessionCookieName string // "devai_session"
	SessionDomain     string // ".tesserix.app"
	SessionSecure     bool   // true in prod
	SessionMaxAge     time.Duration
	SessionEncryptKey string // 16/24/32 raw bytes

	// Authorization
	AdminAllowedEmails string // comma-separated

	// SharedSecret is stamped as X-Auth-Bff-Secret on proxied requests so the
	// upstream (devai identity._forward_trusted) can confirm the X-Forwarded-*
	// identity came from this BFF, not a spoofing in-mesh pod. Blank = unset
	// (legacy behavior: header not sent). Sourced from the same ExternalSecret
	// the upstream reads. Both env names are accepted (DEVAI_AUTH_BFF_SHARED_SECRET
	// preferred, DEVAI_BFF_SHARED_SECRET kept for parity with the upstream).
	SharedSecret string

	// Reverse-proxy targets (kagent + aregistry).
	//
	// kagent and aregistry are internal-only services today — devai-api
	// talks to them over cluster DNS, and there is no public hostname
	// pointing at the BFF for either. The proxy wiring stays in place
	// so a future deployment can opt-in by setting the four env vars
	// below; with the defaults blank, the proxy never matches anything.
	KagentUpstreamURL    string // e.g. http://kagent-ui.kagent-system.svc.cluster.local:8080
	AregistryUpstreamURL string // e.g. http://agentregistry.agentregistry-system.svc.cluster.local:12121

	// Trusted-host enforcement on the proxy paths. Blank = disabled.
	KagentHost    string // e.g. kagent.tesserix.app
	AregistryHost string // e.g. aregistry.tesserix.app

	// Live preview reverse-proxy. PreviewDomain is the apex preview hosts
	// live under (e.g. tesserix.app → preview-<id>.tesserix.app /
	// api-<id>.tesserix.app); PreviewNamespace is the in-cluster namespace
	// the per-session Services live in (e.g. devai-previews). Both blank =
	// preview proxy disabled.
	PreviewDomain    string
	PreviewNamespace string
}

// Load reads env vars into a Config. Returns an error listing every missing
// required field — we want one failure with all the gaps, not three sequential
// "ENV X required" pod restarts.
func Load() (*Config, error) {
	cfg := &Config{
		HTTPPort:             getEnv("DEVAI_BFF_HTTP_PORT", ":8090"),
		GCPProjectID:         os.Getenv("DEVAI_BFF_GCP_PROJECT_ID"),
		ALMTenantID:          os.Getenv("DEVAI_BFF_ALM_TENANT_ID"),
		SRETenantID:          os.Getenv("DEVAI_BFF_SRE_TENANT_ID"),
		AgenticTenant:        os.Getenv("DEVAI_BFF_AGENTIC_TENANT_ID"),
		GIPWebAPIKey:         os.Getenv("DEVAI_BFF_GIP_WEB_API_KEY"),
		GIPAuthDomain:        getEnv("DEVAI_BFF_GIP_AUTH_DOMAIN", ""),
		SessionCookieName:    getEnv("DEVAI_BFF_SESSION_COOKIE_NAME", "devai_session"),
		SessionDomain:        getEnv("DEVAI_BFF_SESSION_DOMAIN", ".tesserix.app"),
		SessionSecure:        getEnv("DEVAI_BFF_SESSION_SECURE", "true") == "true",
		SessionEncryptKey:    os.Getenv("DEVAI_BFF_SESSION_ENCRYPT_KEY"),
		AdminAllowedEmails:   os.Getenv("DEVAI_BFF_ADMIN_ALLOWED_EMAILS"),
		SharedSecret:         firstEnv("DEVAI_AUTH_BFF_SHARED_SECRET", "DEVAI_BFF_SHARED_SECRET"),
		KagentUpstreamURL:    os.Getenv("DEVAI_BFF_KAGENT_UPSTREAM_URL"),
		AregistryUpstreamURL: os.Getenv("DEVAI_BFF_AREGISTRY_UPSTREAM_URL"),
		KagentHost:           os.Getenv("DEVAI_BFF_KAGENT_HOST"),
		AregistryHost:        os.Getenv("DEVAI_BFF_AREGISTRY_HOST"),
		PreviewDomain:        os.Getenv("DEVAI_BFF_PREVIEW_DOMAIN"),
		PreviewNamespace:     os.Getenv("DEVAI_BFF_PREVIEW_NAMESPACE"),
	}
	// Hostname → pool routing. ALM pool always covers the ALM dashboard.
	// The kagent / aregistry reverse-proxy hostnames are opt-in via the
	// DEVAI_BFF_*_HOST env vars and only join ALMHosts when set — the
	// services are otherwise internal-only and never reach the BFF.
	cfg.ALMHosts = []string{
		getEnv("DEVAI_BFF_ALM_HOST", "devai.tesserix.app"),
	}
	if cfg.KagentHost != "" {
		cfg.ALMHosts = append(cfg.ALMHosts, cfg.KagentHost)
	}
	if cfg.AregistryHost != "" {
		cfg.ALMHosts = append(cfg.ALMHosts, cfg.AregistryHost)
	}
	cfg.SREHosts = []string{
		getEnv("DEVAI_BFF_SRE_HOST", "sre.tesserix.app"),
	}

	if v := os.Getenv("DEVAI_BFF_SESSION_MAX_AGE_SECONDS"); v != "" {
		secs, err := time.ParseDuration(v + "s")
		if err != nil {
			return nil, fmt.Errorf("DEVAI_BFF_SESSION_MAX_AGE_SECONDS: %w", err)
		}
		cfg.SessionMaxAge = secs
	}
	if cfg.SessionMaxAge == 0 {
		cfg.SessionMaxAge = 24 * time.Hour
	}

	var missing []string
	if cfg.GCPProjectID == "" {
		missing = append(missing, "DEVAI_BFF_GCP_PROJECT_ID")
	}
	if cfg.ALMTenantID == "" {
		missing = append(missing, "DEVAI_BFF_ALM_TENANT_ID")
	}
	if cfg.SRETenantID == "" {
		missing = append(missing, "DEVAI_BFF_SRE_TENANT_ID")
	}
	if cfg.SessionEncryptKey == "" {
		missing = append(missing, "DEVAI_BFF_SESSION_ENCRYPT_KEY")
	}
	if cfg.AdminAllowedEmails == "" {
		missing = append(missing, "DEVAI_BFF_ADMIN_ALLOWED_EMAILS")
	}
	if cfg.GIPWebAPIKey == "" {
		missing = append(missing, "DEVAI_BFF_GIP_WEB_API_KEY")
	}
	if cfg.GIPAuthDomain == "" {
		// Reasonable default; can still override via env if a custom auth
		// domain is provisioned in Firebase.
		cfg.GIPAuthDomain = cfg.GCPProjectID + ".firebaseapp.com"
	}
	if len(missing) > 0 {
		return nil, fmt.Errorf("config: required env vars missing: %s", strings.Join(missing, ", "))
	}

	return cfg, nil
}

func getEnv(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

// firstEnv returns the value of the first env var in keys that is set and
// non-empty, else "". Used where one setting accepts several alias names.
func firstEnv(keys ...string) string {
	for _, k := range keys {
		if v := os.Getenv(k); v != "" {
			return v
		}
	}
	return ""
}
