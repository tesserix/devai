// Package proxy reverse-proxies kagent + aregistry traffic.
//
// Both services are internal-only by default — devai-api dials them over
// cluster DNS and no public hostname routes here. The proxy stays
// available so a future deployment can opt-in by setting all four of
// DEVAI_BFF_{KAGENT,AREGISTRY}_{HOST,UPSTREAM_URL}. When the host envs
// are blank (the default), ServeHTTP refuses to match — empty Host
// headers cannot fall through to either proxy.
//
// When opted in: requests to KagentHost / AregistryHost land on the BFF;
// this handler inspects the session cookie, redirects to the login page
// on miss, and otherwise proxies to the upstream UI service
// (kagent-ui:8080 or agentregistry:12121).
//
// The login page is served by devai-dashboard at /login?return_to=... and
// uses the Firebase JS SDK to obtain an ID token, posts it to
// /auth/auto-login, then redirects back. Same pattern as the dashboards.
package proxy

import (
	"net/http"
	"net/http/httputil"
	"net/url"
	"strings"

	"github.com/tesserix/devai/services/auth-bff/internal/session"
)

// LoginURL is where we send unauthenticated visitors. It's served by
// devai-dashboard (Next.js) and supports a return_to query param. We
// build the login URL per-request because we want the same cookie to
// span all *.tesserix.app subdomains.
const LoginURL = "https://devai.tesserix.app/login"

// Handler is the reverse-proxy entrypoint mounted as the default handler
// on hostnames that should be gated by the BFF.
type Handler struct {
	session        *session.Manager
	kagentProxy    *httputil.ReverseProxy
	aregistryProxy *httputil.ReverseProxy
	previewProxy   *httputil.ReverseProxy
	kagentHost     string
	aregistryHost  string
	previewDomain  string // e.g. tesserix.app — gates which hosts the preview proxy serves
}

// Config holds Handler dependencies.
type Config struct {
	Session           *session.Manager
	KagentUpstream    string // e.g. http://kagent-ui.kagent-system.svc.cluster.local:8080 (blank = disabled)
	AregistryUpstream string // e.g. http://agentregistry.agentregistry-system.svc.cluster.local:12121 (blank = disabled)
	KagentHost        string // e.g. kagent.tesserix.app (blank = no public exposure)
	AregistryHost     string // e.g. aregistry.tesserix.app (blank = no public exposure)
	// Live preview proxy. PreviewDomain is the apex the preview hosts live
	// under (e.g. "tesserix.app", matching preview-<id>.tesserix.app /
	// api-<id>.tesserix.app). PreviewNamespace is the in-cluster namespace the
	// per-session Services live in (e.g. "devai-previews"). Both blank =
	// preview proxy disabled.
	PreviewDomain    string
	PreviewNamespace string
}

// New constructs a Handler.
func New(cfg Config) (*Handler, error) {
	kagentURL, err := url.Parse(cfg.KagentUpstream)
	if err != nil {
		return nil, err
	}
	aregistryURL, err := url.Parse(cfg.AregistryUpstream)
	if err != nil {
		return nil, err
	}
	h := &Handler{
		session:        cfg.Session,
		kagentProxy:    newSingleHostReverseProxy(kagentURL),
		aregistryProxy: newSingleHostReverseProxy(aregistryURL),
		kagentHost:     cfg.KagentHost,
		aregistryHost:  cfg.AregistryHost,
		previewDomain:  strings.ToLower(cfg.PreviewDomain),
	}
	if cfg.PreviewDomain != "" && cfg.PreviewNamespace != "" {
		h.previewProxy = newDynamicPreviewProxy(cfg.PreviewNamespace)
	}
	return h, nil
}

// ServeHTTP routes by Host header to the right upstream. Any host that
// doesn't match falls through to a 404 — the auth-bff isn't a catch-all
// proxy. We expect Istio to only deliver kagent / aregistry traffic here.
//
// Empty configured hostnames are never matchable. When the operator
// hasn't set DEVAI_BFF_KAGENT_HOST / DEVAI_BFF_AREGISTRY_HOST, the
// corresponding case is skipped so a request with a blank Host header
// can't accidentally land on the empty branch.
func (h *Handler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	host := strings.ToLower(stripPort(r.Host))

	var target *httputil.ReverseProxy
	switch {
	case h.kagentHost != "" && host == h.kagentHost:
		target = h.kagentProxy
	case h.aregistryHost != "" && host == h.aregistryHost:
		target = h.aregistryProxy
	case h.previewProxy != nil && h.isPreviewHost(host):
		// Live preview hosts (preview-<id>.<domain> / api-<id>.<domain>).
		// The dynamic proxy derives the in-cluster Service from the host
		// label on each request. WebSocket/HMR upgrades pass through
		// because httputil.ReverseProxy handles 101 Switching Protocols.
		target = h.previewProxy
	default:
		http.NotFound(w, r)
		return
	}

	// Session check. Missing/invalid session → redirect to dashboard login.
	sess, err := h.session.Read(r)
	if err != nil || sess == nil {
		returnTo := "https://" + host + r.URL.RequestURI()
		http.Redirect(w, r, LoginURL+"?return_to="+url.QueryEscape(returnTo)+"&pool=agentic", http.StatusFound)
		return
	}

	// Stamp identity headers for upstream consumption.
	r.Header.Set("X-Forwarded-User", sess.Email)
	r.Header.Set("X-Forwarded-Email", sess.Email)
	r.Header.Set("X-Forwarded-Uid", sess.UID)
	r.Header.Set("X-Forwarded-Tenant", sess.TenantID)

	target.ServeHTTP(w, r)
}

// newSingleHostReverseProxy is httputil.NewSingleHostReverseProxy with
// the default director extended to preserve the request host (kagent-ui
// reads Host for its CSRF check) and to send X-Forwarded-Proto.
func newSingleHostReverseProxy(target *url.URL) *httputil.ReverseProxy {
	rp := httputil.NewSingleHostReverseProxy(target)
	defaultDirector := rp.Director
	rp.Director = func(req *http.Request) {
		defaultDirector(req)
		// httputil's default replaces Host with the target's; that
		// breaks Next.js apps that inspect the original Host for CSRF.
		// Preserve the inbound host explicitly.
		req.Header.Set("X-Forwarded-Host", req.Host)
		req.Header.Set("X-Forwarded-Proto", "https")
		req.Host = target.Host
	}
	return rp
}

func stripPort(host string) string {
	if i := strings.IndexByte(host, ':'); i >= 0 {
		return host[:i]
	}
	return host
}

// isPreviewHost reports whether host is a preview host this BFF should serve:
// a single-level subdomain of previewDomain whose label is prefixed
// "preview-" or "api-". The prefix allowlist bounds the proxy so an
// authenticated user can't turn the BFF into a catch-all SSRF into the
// preview namespace — only Services named preview-*/api-* are reachable.
func (h *Handler) isPreviewHost(host string) bool {
	if h.previewDomain == "" {
		return false
	}
	suffix := "." + h.previewDomain
	if !strings.HasSuffix(host, suffix) {
		return false
	}
	label := host[:len(host)-len(suffix)]
	if label == "" || strings.Contains(label, ".") { // single-level only
		return false
	}
	return strings.HasPrefix(label, "preview-") || strings.HasPrefix(label, "api-")
}

// previewServiceName maps a preview host to its in-cluster Service name. By
// convention the leftmost DNS label IS the Service name (preview-<id> /
// api-<id>), so routing needs no session→service lookup for the spike. Owner
// scoping (session email == preview owner) is layered on in Phase 1 once the
// preview_sessions map is wired.
func previewServiceName(host, domain string) string {
	return strings.TrimSuffix(host, "."+domain)
}

// newDynamicPreviewProxy builds one ReverseProxy whose director resolves the
// upstream per request from the inbound Host — so a single proxy serves every
// ephemeral preview Service in namespace ns. WebSocket/HMR upgrades work
// automatically (ReverseProxy proxies the Upgrade since Go 1.12). The preview
// Service exposes port 80 → the dev/app port.
func newDynamicPreviewProxy(ns string) *httputil.ReverseProxy {
	rp := &httputil.ReverseProxy{}
	rp.Director = func(req *http.Request) {
		host := strings.ToLower(stripPort(req.Host))
		// domain = everything after the first label.
		domain := host
		if i := strings.IndexByte(host, '.'); i >= 0 {
			domain = host[i+1:]
		}
		svc := previewServiceName(host, domain)
		req.Header.Set("X-Forwarded-Host", req.Host)
		req.Header.Set("X-Forwarded-Proto", "https")
		req.URL.Scheme = "http"
		req.URL.Host = svc + "." + ns + ".svc.cluster.local:80"
		req.Host = req.URL.Host
	}
	return rp
}
