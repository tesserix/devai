package handlers

import (
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/tesserix/devai/services/auth-bff/internal/session"
)

func TestLogout_AllowsBrowserGet(t *testing.T) {
	sessions, err := session.NewManager(session.Config{
		CookieName: "devai_session",
		EncryptKey: "01234567890123456789012345678901",
	})
	if err != nil {
		t.Fatal(err)
	}
	h := NewAuthHandler(AuthDeps{Session: sessions})
	mux := http.NewServeMux()
	h.Register(mux)

	req := httptest.NewRequest(http.MethodGet, "/auth/logout", nil)
	res := httptest.NewRecorder()
	mux.ServeHTTP(res, req)

	if res.Code != http.StatusSeeOther {
		t.Fatalf("GET /auth/logout status = %d, want %d", res.Code, http.StatusSeeOther)
	}
	if got := res.Header().Get("Location"); got != "/login" {
		t.Fatalf("GET /auth/logout Location = %q, want /login", got)
	}
	if cookie := res.Header().Get("Set-Cookie"); cookie == "" {
		t.Fatal("GET /auth/logout did not clear the session cookie")
	}
}
