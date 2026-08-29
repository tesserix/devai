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

func TestLogout_RejectsCrossSiteRequests(t *testing.T) {
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

	cases := []struct {
		name    string
		method  string
		headers map[string]string
		want    int
	}{
		{"cross-site GET img tag", http.MethodGet, map[string]string{"Sec-Fetch-Site": "cross-site"}, http.StatusForbidden},
		{"cross-site POST form", http.MethodPost, map[string]string{"Sec-Fetch-Site": "cross-site"}, http.StatusForbidden},
		{"legacy cross-origin POST", http.MethodPost, map[string]string{"Origin": "https://evil.example"}, http.StatusForbidden},
		{"opaque origin POST", http.MethodPost, map[string]string{"Origin": "null"}, http.StatusForbidden},
		{"direct navigation GET", http.MethodGet, map[string]string{"Sec-Fetch-Site": "none"}, http.StatusSeeOther},
		{"same-site navigation GET", http.MethodGet, map[string]string{"Sec-Fetch-Site": "same-site"}, http.StatusSeeOther},
		{"same-origin POST", http.MethodPost, map[string]string{"Sec-Fetch-Site": "same-origin"}, http.StatusOK},
		{"non-browser POST", http.MethodPost, nil, http.StatusOK},
	}
	for _, tc := range cases {
		req := httptest.NewRequest(tc.method, "/auth/logout", nil)
		for k, v := range tc.headers {
			req.Header.Set(k, v)
		}
		res := httptest.NewRecorder()
		mux.ServeHTTP(res, req)
		if res.Code != tc.want {
			t.Errorf("%s: status = %d, want %d", tc.name, res.Code, tc.want)
		}
	}
}
