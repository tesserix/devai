// Package allowlist gates which email addresses can mint a session.
//
// DevAI is an admin-only platform. The allow-list is short and static:
// the operators and on-call. Loaded from the DEVAI_ADMIN_ALLOWED_EMAILS
// env (comma-separated). Whitespace tolerated.
package allowlist

import (
	"strings"
)

// Allowlist is a set of lower-cased emails.
type Allowlist struct {
	emails map[string]struct{}
}

// Parse builds an Allowlist from a comma-separated string. Empty string
// returns an empty (deny-all) list — explicit configuration is required.
func Parse(raw string) *Allowlist {
	out := &Allowlist{emails: map[string]struct{}{}}
	for _, part := range strings.Split(raw, ",") {
		e := strings.TrimSpace(strings.ToLower(part))
		if e == "" {
			continue
		}
		out.emails[e] = struct{}{}
	}
	return out
}

// Has returns true if the email is on the list. Case-insensitive.
func (a *Allowlist) Has(email string) bool {
	if a == nil || len(a.emails) == 0 {
		return false
	}
	_, ok := a.emails[strings.ToLower(strings.TrimSpace(email))]
	return ok
}

// Size returns the number of distinct emails.
func (a *Allowlist) Size() int {
	if a == nil {
		return 0
	}
	return len(a.emails)
}
