// Package session implements an AES-GCM-encrypted cookie session.
//
// Same shape as mark8ly: cookie value is base64(nonce || ciphertext). The
// JSON payload (UID, email, tenant, exp, iat) lives inside the AEAD, so
// any tampering fails the auth tag.
//
// No Redis. The cookie IS the session. Logout just clears the cookie.
package session

import (
	"crypto/aes"
	"crypto/cipher"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"time"
)

// Session is the decoded payload of a session cookie.
type Session struct {
	UID       string    `json:"uid"`
	Email     string    `json:"email"`
	TenantID  string    `json:"tenant_id"`
	Pool      string    `json:"pool"` // "alm" or "sre" — which dashboard the user logged into
	IssuedAt  time.Time `json:"iat"`
	ExpiresAt time.Time `json:"exp"`
}

func (s *Session) IsExpired() bool { return time.Now().After(s.ExpiresAt) }

// Manager mints, reads, and clears session cookies.
type Manager struct {
	cookieName string
	domain     string
	secure     bool
	maxAge     time.Duration
	gcm        cipher.AEAD
}

// Config holds Manager dependencies.
type Config struct {
	CookieName string
	Domain     string        // e.g. ".tesserix.app" for prod, "localhost" for dev
	Secure     bool          // true on HTTPS
	MaxAge     time.Duration // defaults to 24h
	EncryptKey string        // 16/24/32 bytes
}

// NewManager constructs a Manager. EncryptKey must be 16/24/32 raw bytes.
func NewManager(cfg Config) (*Manager, error) {
	if cfg.CookieName == "" {
		return nil, fmt.Errorf("session: CookieName is required")
	}
	if cfg.EncryptKey == "" {
		return nil, fmt.Errorf("session: EncryptKey is required")
	}
	keyBytes := []byte(cfg.EncryptKey)
	if l := len(keyBytes); l != 16 && l != 24 && l != 32 {
		return nil, fmt.Errorf("session: EncryptKey must be 16/24/32 bytes (got %d)", l)
	}
	block, err := aes.NewCipher(keyBytes)
	if err != nil {
		return nil, fmt.Errorf("session: aes cipher: %w", err)
	}
	gcm, err := cipher.NewGCM(block)
	if err != nil {
		return nil, fmt.Errorf("session: gcm: %w", err)
	}

	maxAge := cfg.MaxAge
	if maxAge == 0 {
		maxAge = 24 * time.Hour
	}
	return &Manager{
		cookieName: cfg.CookieName,
		domain:     cfg.Domain,
		secure:     cfg.Secure,
		maxAge:     maxAge,
		gcm:        gcm,
	}, nil
}

// Mint encodes the session and writes it as a cookie on w.
func (m *Manager) Mint(w http.ResponseWriter, s Session) error {
	if s.UID == "" {
		return errors.New("session: UID is required")
	}
	now := time.Now()
	if s.IssuedAt.IsZero() {
		s.IssuedAt = now
	}
	if s.ExpiresAt.IsZero() {
		s.ExpiresAt = now.Add(m.maxAge)
	}
	cookieMaxAge := int(s.ExpiresAt.Sub(now).Seconds())

	encoded, err := m.encode(s)
	if err != nil {
		return err
	}

	http.SetCookie(w, &http.Cookie{
		Name:     m.cookieName,
		Value:    encoded,
		Path:     "/",
		Domain:   m.domain,
		Expires:  s.ExpiresAt,
		MaxAge:   cookieMaxAge,
		HttpOnly: true,
		Secure:   m.secure,
		SameSite: http.SameSiteLaxMode,
	})
	return nil
}

// Read returns the session if the cookie is present + valid + not expired.
// Returns (nil, nil) when no cookie is present.
func (m *Manager) Read(r *http.Request) (*Session, error) {
	c, err := r.Cookie(m.cookieName)
	if err != nil {
		return nil, nil
	}
	s, err := m.decode(c.Value)
	if err != nil {
		return nil, ErrInvalidSession
	}
	if s.IsExpired() {
		return nil, ErrExpiredSession
	}
	return s, nil
}

// Clear writes a max-age=-1 cookie so the browser deletes its copy.
func (m *Manager) Clear(w http.ResponseWriter) {
	http.SetCookie(w, &http.Cookie{
		Name:     m.cookieName,
		Value:    "",
		Path:     "/",
		Domain:   m.domain,
		MaxAge:   -1,
		HttpOnly: true,
		Secure:   m.secure,
		SameSite: http.SameSiteLaxMode,
	})
}

var (
	ErrInvalidSession = errors.New("session: invalid or tampered cookie")
	ErrExpiredSession = errors.New("session: cookie expired")
)

func (m *Manager) encode(s Session) (string, error) {
	plaintext, err := json.Marshal(s)
	if err != nil {
		return "", fmt.Errorf("session: marshal: %w", err)
	}
	nonce := make([]byte, m.gcm.NonceSize())
	if _, err := io.ReadFull(rand.Reader, nonce); err != nil {
		return "", fmt.Errorf("session: nonce: %w", err)
	}
	ciphertext := m.gcm.Seal(nonce, nonce, plaintext, nil)
	return base64.RawURLEncoding.EncodeToString(ciphertext), nil
}

func (m *Manager) decode(encoded string) (*Session, error) {
	raw, err := base64.RawURLEncoding.DecodeString(encoded)
	if err != nil {
		return nil, err
	}
	if len(raw) < m.gcm.NonceSize() {
		return nil, errors.New("session: ciphertext too short")
	}
	nonce, ciphertext := raw[:m.gcm.NonceSize()], raw[m.gcm.NonceSize():]
	plaintext, err := m.gcm.Open(nil, nonce, ciphertext, nil)
	if err != nil {
		return nil, err
	}
	var s Session
	if err := json.Unmarshal(plaintext, &s); err != nil {
		return nil, err
	}
	return &s, nil
}
