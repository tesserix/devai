package session

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func newTestManager(t *testing.T) *Manager {
	t.Helper()
	m, err := NewManager(Config{
		CookieName: "devai_session",
		Domain:     ".tesserix.app",
		Secure:     true,
		MaxAge:     time.Hour,
		EncryptKey: "this-is-a-32-byte-test-key-aaaaa", // 32 bytes
	})
	if err != nil {
		t.Fatalf("NewManager: %v", err)
	}
	return m
}

func TestMintAndRead_RoundTrip(t *testing.T) {
	m := newTestManager(t)
	w := httptest.NewRecorder()
	want := Session{
		UID:      "uid-123",
		Email:    "alice@example.com",
		TenantID: "DevAI-ALM-abc12",
		Pool:     "alm",
	}
	if err := m.Mint(w, want); err != nil {
		t.Fatalf("Mint: %v", err)
	}
	cookies := w.Result().Cookies()
	if len(cookies) != 1 {
		t.Fatalf("expected 1 cookie, got %d", len(cookies))
	}
	// Go's net/http strips the leading dot from Domain when emitting the
	// header (RFC 6265 says the dot is implicit). Modern browsers treat
	// "Domain=tesserix.app" as covering "*.tesserix.app" — exactly what
	// we want. Verify the emitted attribute is the cross-subdomain form.
	rawSetCookie := w.Header().Get("Set-Cookie")
	if !strings.Contains(rawSetCookie, "Domain=tesserix.app") {
		t.Errorf("Set-Cookie header missing Domain=tesserix.app: %s", rawSetCookie)
	}

	r := httptest.NewRequest(http.MethodGet, "/", nil)
	r.AddCookie(cookies[0])
	got, err := m.Read(r)
	if err != nil {
		t.Fatalf("Read: %v", err)
	}
	if got.UID != want.UID || got.Email != want.Email || got.TenantID != want.TenantID || got.Pool != want.Pool {
		t.Errorf("round-trip mismatch: got %+v want %+v", got, want)
	}
}

func TestRead_TamperedCookie(t *testing.T) {
	m := newTestManager(t)
	w := httptest.NewRecorder()
	if err := m.Mint(w, Session{UID: "u", Email: "e", TenantID: "t"}); err != nil {
		t.Fatalf("Mint: %v", err)
	}
	c := w.Result().Cookies()[0]
	// Flip a single character in the encrypted payload — auth tag must reject it.
	c.Value = strings.ReplaceAll(c.Value, "A", "B")

	r := httptest.NewRequest(http.MethodGet, "/", nil)
	r.AddCookie(c)
	if _, err := m.Read(r); err == nil {
		t.Fatalf("expected ErrInvalidSession on tampered cookie, got nil")
	}
}

func TestRead_MissingCookie(t *testing.T) {
	m := newTestManager(t)
	r := httptest.NewRequest(http.MethodGet, "/", nil)
	sess, err := m.Read(r)
	if sess != nil || err != nil {
		t.Errorf("expected (nil, nil) when no cookie, got (%v, %v)", sess, err)
	}
}

func TestRead_Expired(t *testing.T) {
	m := newTestManager(t)
	w := httptest.NewRecorder()
	past := time.Now().Add(-time.Hour)
	if err := m.Mint(w, Session{UID: "u", Email: "e", IssuedAt: past.Add(-time.Hour), ExpiresAt: past}); err != nil {
		t.Fatalf("Mint: %v", err)
	}
	r := httptest.NewRequest(http.MethodGet, "/", nil)
	r.AddCookie(w.Result().Cookies()[0])
	_, err := m.Read(r)
	if err != ErrExpiredSession {
		t.Errorf("err = %v, want ErrExpiredSession", err)
	}
}

func TestNewManager_BadKeyLength(t *testing.T) {
	_, err := NewManager(Config{
		CookieName: "x",
		EncryptKey: "too-short", // 9 bytes
	})
	if err == nil {
		t.Fatalf("expected error for short key, got nil")
	}
}
