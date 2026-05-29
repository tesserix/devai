// Package handlers serves the auth-bff HTTP routes.
//
// Endpoints:
//
//	POST /auth/auto-login   — exchange GIP id_token for a session cookie
//	GET  /auth/me           — introspect current session
//	POST /auth/logout       — clear the session cookie
//	GET  /healthz           — liveness
//	GET  /readyz            — readiness (always 200 once handler is constructed)
//
// The reverse-proxy paths for kagent + aregistry live in the proxy
// package (mounted on a separate Handler).
package handlers

import (
	"encoding/json"
	"errors"
	"net/http"
	"strings"

	"github.com/tesserix/devai/services/auth-bff/internal/allowlist"
	"github.com/tesserix/devai/services/auth-bff/internal/gip"
	"github.com/tesserix/devai/services/auth-bff/internal/session"
)

// AuthHandler wires the auth endpoints.
type AuthHandler struct {
	verifier     gip.Verifier
	session      *session.Manager
	allowlist    *allowlist.Allowlist
	almTenant    string
	sreTenant    string
	gipAPIKey    string
	gipAuthDom   string
	gipProjectID string
	almHosts     map[string]struct{}
	sreHosts     map[string]struct{}
}

// AuthDeps groups handler dependencies.
type AuthDeps struct {
	Verifier         gip.Verifier
	Session          *session.Manager
	Allowlist        *allowlist.Allowlist
	ALMTenant        string
	SRETenant        string
	GIPWebAPIKey     string
	GIPAuthDomain    string
	GIPProjectID     string
	ALMHosts         []string
	SREHosts         []string
}

// NewAuthHandler constructs the AuthHandler.
func NewAuthHandler(d AuthDeps) *AuthHandler {
	almHosts := map[string]struct{}{}
	for _, h := range d.ALMHosts {
		almHosts[strings.ToLower(h)] = struct{}{}
	}
	sreHosts := map[string]struct{}{}
	for _, h := range d.SREHosts {
		sreHosts[strings.ToLower(h)] = struct{}{}
	}
	return &AuthHandler{
		verifier:     d.Verifier,
		session:      d.Session,
		allowlist:    d.Allowlist,
		almTenant:    d.ALMTenant,
		sreTenant:    d.SRETenant,
		gipAPIKey:    d.GIPWebAPIKey,
		gipAuthDom:   d.GIPAuthDomain,
		gipProjectID: d.GIPProjectID,
		almHosts:     almHosts,
		sreHosts:     sreHosts,
	}
}

// Register mounts the auth routes on a net/http ServeMux.
func (h *AuthHandler) Register(mux *http.ServeMux) {
	mux.HandleFunc("/auth/config", h.config)
	mux.HandleFunc("/auth/auto-login", h.autoLogin)
	mux.HandleFunc("/auth/me", h.me)
	mux.HandleFunc("/auth/logout", h.logout)
	mux.HandleFunc("/healthz", h.healthz)
	mux.HandleFunc("/readyz", h.readyz)
}

// config serves the Firebase Web client config to the login page. We pick
// the tenant/pool based on the inbound Host so the same image serves both
// the ALM dashboard (devai.tesserix.app) and the SRE dashboard
// (sre.tesserix.app) without rebuilding.
//
// These values are not secret per Firebase's threat model — the API key
// is a project identifier and verification happens server-side — but we
// still avoid checking them into the repo so rotation flows through
// GCP Secret Manager like every other DevAI credential.
func (h *AuthHandler) config(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, `{"error":"method_not_allowed"}`, http.StatusMethodNotAllowed)
		return
	}
	host := strings.ToLower(stripHostPort(r.Host))
	// The Istio VirtualService rewrites X-Forwarded-Host on incoming
	// requests — prefer it when present (Host is the in-cluster service
	// name once you're past the gateway, which would always be alm).
	if xfh := r.Header.Get("X-Forwarded-Host"); xfh != "" {
		host = strings.ToLower(stripHostPort(xfh))
	}

	pool := "alm"
	tenant := h.almTenant
	switch {
	case host == "" || lookupHost(h.almHosts, host):
		pool = "alm"
		tenant = h.almTenant
	case lookupHost(h.sreHosts, host):
		pool = "sre"
		tenant = h.sreTenant
	}

	writeJSON(w, http.StatusOK, map[string]string{
		"apiKey":     h.gipAPIKey,
		"authDomain": h.gipAuthDom,
		"projectId":  h.gipProjectID,
		"tenantId":   tenant,
		"pool":       pool,
	})
}

func stripHostPort(host string) string {
	if i := strings.IndexByte(host, ':'); i >= 0 {
		return host[:i]
	}
	return host
}

func lookupHost(set map[string]struct{}, host string) bool {
	_, ok := set[host]
	return ok
}

type autoLoginRequest struct {
	IDToken          string `json:"id_token"`
	ExpectedTenantID string `json:"expected_tenant_id"`
	Pool             string `json:"pool"` // "alm" | "sre" | "agentic"
}

type autoLoginResponse struct {
	UID      string `json:"uid"`
	Email    string `json:"email"`
	TenantID string `json:"tenant_id"`
	Pool     string `json:"pool"`
}

func (h *AuthHandler) autoLogin(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, `{"error":"method_not_allowed"}`, http.StatusMethodNotAllowed)
		return
	}

	var req autoLoginRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, errorResp("invalid_request", "could not parse body"))
		return
	}
	if req.IDToken == "" || req.ExpectedTenantID == "" || req.Pool == "" {
		writeJSON(w, http.StatusBadRequest, errorResp("invalid_request", "id_token, expected_tenant_id, and pool are required"))
		return
	}
	// Pool/tenant cross-check: the caller cannot ask the BFF to mint an
	// "alm" session against the SRE tenant.
	switch req.Pool {
	case "alm":
		if req.ExpectedTenantID != h.almTenant {
			writeJSON(w, http.StatusBadRequest, errorResp("invalid_request", "expected_tenant_id does not match alm pool"))
			return
		}
	case "sre":
		if req.ExpectedTenantID != h.sreTenant {
			writeJSON(w, http.StatusBadRequest, errorResp("invalid_request", "expected_tenant_id does not match sre pool"))
			return
		}
	case "agentic":
		// agentic UIs reuse the alm tenant pool — a single login covers
		// the ALM dashboard plus any opt-in kagent / aregistry public
		// hostnames (disabled by default — both services are
		// internal-only). Pool just tags which surface initiated the
		// login for audit.
	default:
		writeJSON(w, http.StatusBadRequest, errorResp("invalid_request", "pool must be alm, sre, or agentic"))
		return
	}

	verified, err := h.verifier.VerifyToken(r.Context(), req.IDToken, req.ExpectedTenantID)
	if err != nil {
		status := http.StatusUnauthorized
		switch {
		case errors.Is(err, gip.ErrTokenInvalid):
			writeJSON(w, status, errorResp("unauthorized", "token invalid or expired"))
		case errors.Is(err, gip.ErrTenantMismatch):
			writeJSON(w, status, errorResp("unauthorized", "token tenant mismatch"))
		default:
			writeJSON(w, status, errorResp("unauthorized", err.Error()))
		}
		return
	}

	if !h.allowlist.Has(verified.Email) {
		writeJSON(w, http.StatusForbidden, errorResp("forbidden", "email not in admin allow-list"))
		return
	}

	sess := session.Session{
		UID:      verified.UID,
		Email:    verified.Email,
		TenantID: verified.TenantID,
		Pool:     req.Pool,
	}
	if err := h.session.Mint(w, sess); err != nil {
		writeJSON(w, http.StatusInternalServerError, errorResp("internal_error", "failed to mint session"))
		return
	}

	writeJSON(w, http.StatusOK, autoLoginResponse{
		UID:      verified.UID,
		Email:    verified.Email,
		TenantID: verified.TenantID,
		Pool:     req.Pool,
	})
}

func (h *AuthHandler) me(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, `{"error":"method_not_allowed"}`, http.StatusMethodNotAllowed)
		return
	}
	sess, err := h.session.Read(r)
	if err != nil || sess == nil {
		writeJSON(w, http.StatusUnauthorized, errorResp("unauthorized", "no valid session"))
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"uid":       sess.UID,
		"email":     sess.Email,
		"tenant_id": sess.TenantID,
		"pool":      sess.Pool,
		"exp":       sess.ExpiresAt,
	})
}

func (h *AuthHandler) logout(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, `{"error":"method_not_allowed"}`, http.StatusMethodNotAllowed)
		return
	}
	h.session.Clear(w)
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

func (h *AuthHandler) healthz(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

func (h *AuthHandler) readyz(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ready"})
}

// --- helpers ---

func writeJSON(w http.ResponseWriter, status int, body any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(body)
}

func errorResp(code, msg string) map[string]string {
	return map[string]string{"error": code, "message": msg}
}

// SessionFromHeader extracts a Bearer session id from the Authorization
// header, primarily for cases where the client cannot use cookies (server
// components calling the BFF). Returns empty string when absent.
func SessionFromHeader(r *http.Request) string {
	auth := r.Header.Get("Authorization")
	if !strings.HasPrefix(auth, "Bearer ") {
		return ""
	}
	return strings.TrimSpace(strings.TrimPrefix(auth, "Bearer "))
}
