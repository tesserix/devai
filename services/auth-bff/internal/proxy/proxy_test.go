// Tests for the kagent + aregistry reverse-proxy router.
//
// These lock in the "internal-only by default" contract: when the
// operator has not opted-in by setting DEVAI_BFF_{KAGENT,AREGISTRY}_HOST,
// the handler must refuse to match any request, even one carrying an
// empty Host header. A regression here would expose either service
// publicly without authentication.
package proxy

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/tesserix/devai/services/auth-bff/internal/session"
)

func newTestSessionManager(t *testing.T) *session.Manager {
	t.Helper()
	m, err := session.NewManager(session.Config{
		CookieName: "devai_session",
		Domain:     "test.local",
		Secure:     false,
		MaxAge:     time.Hour,
		EncryptKey: "0123456789abcdef", // 16 bytes for AES-128
	})
	if err != nil {
		t.Fatalf("session.NewManager: %v", err)
	}
	return m
}

// TestServeHTTP_BlankHostsNeverMatch is the load-bearing test: with no
// hostnames configured, requests must 404 — including the malicious case
// of an empty Host header that previously would have matched the
// empty-string switch arm.
func TestServeHTTP_BlankHostsNeverMatch(t *testing.T) {
	h, err := New(Config{
		Session:           newTestSessionManager(t),
		KagentUpstream:    "", // url.Parse("") returns an empty *URL with no error
		AregistryUpstream: "",
		KagentHost:        "",
		AregistryHost:     "",
	})
	if err != nil {
		t.Fatalf("proxy.New: %v", err)
	}

	cases := []struct{ name, host string }{
		{"empty host header", ""},
		{"unrelated host", "evil.example.com"},
		{"would-have-been-kagent", "kagent.tesserix.app"},
		{"would-have-been-aregistry", "aregistry.tesserix.app"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			req := httptest.NewRequest(http.MethodGet, "http://example/", nil)
			req.Host = tc.host
			w := httptest.NewRecorder()
			h.ServeHTTP(w, req)
			if w.Code != http.StatusNotFound {
				t.Fatalf("expected 404 with blank hosts (Host=%q), got %d", tc.host, w.Code)
			}
		})
	}
}

// TestServeHTTP_OnlyKagentEnabled verifies that opt-in is granular:
// configuring just kagent must not accidentally enable aregistry, and an
// empty Host header must still not match the unconfigured aregistry arm.
func TestServeHTTP_OnlyKagentEnabled(t *testing.T) {
	// A real upstream — we only assert that requests reach it, not its
	// behavior.
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusTeapot)
	}))
	defer upstream.Close()

	h, err := New(Config{
		Session:           newTestSessionManager(t),
		KagentUpstream:    upstream.URL,
		AregistryUpstream: "", // aregistry not opted in
		KagentHost:        "kagent.test",
		AregistryHost:     "",
	})
	if err != nil {
		t.Fatalf("proxy.New: %v", err)
	}

	// Empty Host header → 404 (must not match the empty AregistryHost).
	{
		req := httptest.NewRequest(http.MethodGet, "http://example/", nil)
		req.Host = ""
		w := httptest.NewRecorder()
		h.ServeHTTP(w, req)
		if w.Code != http.StatusNotFound {
			t.Fatalf("empty Host: expected 404, got %d", w.Code)
		}
	}

	// Configured kagent host → 302 (sessionless → redirect to login).
	{
		req := httptest.NewRequest(http.MethodGet, "http://kagent.test/foo", nil)
		req.Host = "kagent.test"
		w := httptest.NewRecorder()
		h.ServeHTTP(w, req)
		if w.Code != http.StatusFound {
			t.Fatalf("kagent.test sessionless: expected 302, got %d", w.Code)
		}
		loc := w.Header().Get("Location")
		if !strings.Contains(loc, "/login") {
			t.Fatalf("expected redirect to /login, got %q", loc)
		}
	}
}
