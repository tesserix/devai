package handlers

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/tesserix/devai/services/auth-bff/internal/gip"
	"github.com/tesserix/devai/services/auth-bff/internal/session"
)

type verifiedUser struct{}

func (verifiedUser) VerifyToken(_ context.Context, _, expectedTenantID string) (*gip.VerifiedToken, error) {
	return &gip.VerifiedToken{UID: "user-1", Email: "user@example.com", TenantID: expectedTenantID}, nil
}

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

func TestAutoLogin_MintsShortLivedCLISession(t *testing.T) {
	sessions, err := session.NewManager(session.Config{
		CookieName: "devai_session",
		EncryptKey: "01234567890123456789012345678901",
	})
	if err != nil {
		t.Fatal(err)
	}
	h := NewAuthHandler(AuthDeps{
		Verifier:     verifiedUser{},
		Session:      sessions,
		PublicSignup: true,
		ALMTenant:    "alm-tenant",
	})
	mux := http.NewServeMux()
	h.Register(mux)

	body := `{"id_token":"proof","expected_tenant_id":"alm-tenant","pool":"alm","client_type":"cli"}`
	req := httptest.NewRequest(http.MethodPost, "/auth/auto-login", strings.NewReader(body))
	res := httptest.NewRecorder()
	mux.ServeHTTP(res, req)

	if res.Code != http.StatusOK {
		t.Fatalf("POST /auth/auto-login status = %d, want %d", res.Code, http.StatusOK)
	}
	cookies := res.Result().Cookies()
	if len(cookies) != 1 {
		t.Fatalf("POST /auth/auto-login cookies = %d, want 1", len(cookies))
	}
	if cookies[0].MaxAge < 55*60 || cookies[0].MaxAge > 65*60 {
		t.Fatalf("POST /auth/auto-login cookie MaxAge = %d, want about 1h", cookies[0].MaxAge)
	}
	readReq := httptest.NewRequest(http.MethodGet, "/auth/me", nil)
	readReq.AddCookie(cookies[0])
	cliSession, err := sessions.Read(readReq)
	if err != nil {
		t.Fatal(err)
	}
	if cliSession == nil {
		t.Fatal("CLI session was not readable")
	}
	remaining := time.Until(cliSession.ExpiresAt)
	if remaining < 55*time.Minute || remaining > 65*time.Minute {
		t.Fatalf("CLI session lifetime = %s, want about 1h", remaining)
	}
}

func TestAutoLogin_RejectsUnsupportedClientType(t *testing.T) {
	sessions, err := session.NewManager(session.Config{
		CookieName: "devai_session",
		EncryptKey: "01234567890123456789012345678901",
	})
	if err != nil {
		t.Fatal(err)
	}
	h := NewAuthHandler(AuthDeps{
		Verifier:     verifiedUser{},
		Session:      sessions,
		PublicSignup: true,
		ALMTenant:    "alm-tenant",
	})
	mux := http.NewServeMux()
	h.Register(mux)

	body := `{"id_token":"proof","expected_tenant_id":"alm-tenant","pool":"alm","client_type":"unknown"}`
	req := httptest.NewRequest(http.MethodPost, "/auth/auto-login", strings.NewReader(body))
	res := httptest.NewRecorder()
	mux.ServeHTTP(res, req)

	if res.Code != http.StatusBadRequest {
		t.Fatalf("POST /auth/auto-login status = %d, want %d", res.Code, http.StatusBadRequest)
	}
}
