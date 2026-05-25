// Package gip verifies Google Identity Platform ID tokens.
//
// DevAI is multi-tenant inside one GCP project: ALM dashboard users come
// from one GIP tenant pool (DEVAI_ALM_TENANT_ID), SRE dashboard users from
// another (DEVAI_SRE_TENANT_ID). Tokens carry the tenant pool ID in
// `firebase.tenant`; the verifier rejects cross-pool tokens.
//
// Same approach as mark8ly-auth-bff: pure-Go JWT verification against
// Google's public JWKS. No Firebase Admin SDK (no ADC, no service account
// JSON), no Redis lookup — the JWT itself is the proof.
package gip

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/MicahParks/keyfunc/v3"
	"github.com/golang-jwt/jwt/v5"
)

// VerifiedToken is the subset of an ID token claims the BFF needs.
type VerifiedToken struct {
	UID      string // sub claim
	Email    string
	TenantID string // firebase.tenant
}

// Verifier validates a bearer ID token against a specific GIP tenant pool.
type Verifier interface {
	VerifyToken(ctx context.Context, idToken, expectedTenantID string) (*VerifiedToken, error)
}

var (
	ErrTokenInvalid   = errors.New("gip: token invalid or expired")
	ErrTenantMismatch = errors.New("gip: token tenant does not match expected pool")
)

const (
	jwksURL         = "https://www.googleapis.com/service_accounts/v1/jwk/securetoken@system.gserviceaccount.com"
	jwtIssuerPrefix = "https://securetoken.google.com/"
)

// Config holds Verifier construction parameters.
type Config struct {
	ProjectID string
}

type jwtVerifier struct {
	projectID string
	keys      keyfunc.Keyfunc
}

// New constructs a Verifier wired to Google's public JWKS endpoint. JWKS
// is fetched lazily and re-fetched on kid-not-found (keyfunc's default).
func New(ctx context.Context, cfg Config) (Verifier, error) {
	if cfg.ProjectID == "" {
		return nil, fmt.Errorf("gip: ProjectID is required")
	}
	k, err := keyfunc.NewDefaultCtx(ctx, []string{jwksURL})
	if err != nil {
		return nil, fmt.Errorf("gip: init jwks: %w", err)
	}
	return &jwtVerifier{projectID: cfg.ProjectID, keys: k}, nil
}

type gipClaims struct {
	Email    string `json:"email"`
	Firebase struct {
		Tenant         string `json:"tenant"`
		SignInProvider string `json:"sign_in_provider"`
	} `json:"firebase"`
	jwt.RegisteredClaims
}

func (v *jwtVerifier) VerifyToken(ctx context.Context, idToken, expectedTenantID string) (*VerifiedToken, error) {
	if idToken == "" {
		return nil, ErrTokenInvalid
	}
	if expectedTenantID == "" {
		return nil, fmt.Errorf("gip: expectedTenantID is required (refusing to verify pool-less tokens)")
	}

	expectedIssuer := jwtIssuerPrefix + v.projectID

	claims := &gipClaims{}
	tok, err := jwt.ParseWithClaims(idToken, claims, v.keys.Keyfunc,
		jwt.WithValidMethods([]string{"RS256"}),
		jwt.WithIssuer(expectedIssuer),
		jwt.WithAudience(v.projectID),
		jwt.WithExpirationRequired(),
		jwt.WithIssuedAt(),
		jwt.WithLeeway(10*time.Second),
	)
	if err != nil {
		return nil, fmt.Errorf("%w: %s", ErrTokenInvalid, err)
	}
	if !tok.Valid {
		return nil, ErrTokenInvalid
	}

	if claims.Firebase.Tenant != expectedTenantID {
		return nil, fmt.Errorf("%w: token tenant %q != expected %q",
			ErrTenantMismatch, claims.Firebase.Tenant, expectedTenantID)
	}

	if claims.Subject == "" {
		return nil, fmt.Errorf("%w: missing sub claim", ErrTokenInvalid)
	}

	return &VerifiedToken{
		UID:      claims.Subject,
		Email:    claims.Email,
		TenantID: claims.Firebase.Tenant,
	}, nil
}
