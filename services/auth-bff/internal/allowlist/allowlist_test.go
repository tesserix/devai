package allowlist

import "testing"

func TestParse(t *testing.T) {
	a := Parse("alice@example.com, BOB@Example.com ,charlie@example.com")
	if a.Size() != 3 {
		t.Errorf("Size = %d, want 3", a.Size())
	}
	if !a.Has("alice@example.com") {
		t.Errorf("alice missing")
	}
	if !a.Has("bob@example.com") { // case-folded
		t.Errorf("bob (case-insensitive) missing")
	}
	if a.Has("mallory@example.com") {
		t.Errorf("mallory should not be present")
	}
}

func TestParse_Empty(t *testing.T) {
	a := Parse("")
	if a.Size() != 0 {
		t.Errorf("empty input should give empty list, got Size=%d", a.Size())
	}
	if a.Has("anyone@example.com") {
		t.Errorf("empty allowlist must deny all")
	}
}

func TestHas_NilSafe(t *testing.T) {
	var a *Allowlist
	if a.Has("anyone@example.com") {
		t.Errorf("nil allowlist must deny all")
	}
}
