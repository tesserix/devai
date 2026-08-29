# Install and authenticate the DevAI CLI

The DevAI Homebrew formula installs a standalone macOS executable for Apple
Silicon or Intel. A Python environment and a checkout of the private DevAI source
repository are not required.

## Install

Install Homebrew first, then add the public Tesserix tap and install DevAI:

```bash
brew tap tesserix/tap
brew install devai
```

Verify the installed formula and CLI entry points:

```bash
brew info tesserix/tap/devai
devai --help
devai auth --help
```

Homebrew downloads the binary for the current Mac architecture and verifies the
release archive against the SHA-256 checksum in the formula.

## Sign in

DevAI authentication is separate from Agent Registry authentication. Start the
browser sign-in flow against the production control plane:

```bash
devai auth login --api-url https://devai.tesserix.app
devai auth status
```

The browser flow creates a one-hour CLI session. DevAI stores the encrypted
session only in the operating-system keychain; it does not write the session to a
project file or environment file. Sign in again after the session expires.

Remove the local session immediately when it is no longer needed:

```bash
devai auth logout
```

Do not paste session tokens, Registry client secrets, provider keys, or cookies
into source files, shell history, issue descriptions, or CI logs.

## Upgrade

Refresh the tap metadata and install the latest published version:

```bash
brew update
brew upgrade devai
brew info tesserix/tap/devai
```

`brew upgrade devai` reports that the current version is already installed when
there is no newer release.

## Resolve an older command shadowing Homebrew

An earlier pip, uv, or locally linked installation can appear before the Homebrew
keg. Inspect every matching executable before changing links:

```bash
which -a devai
brew --prefix devai
brew link --overwrite --dry-run devai
```

The Homebrew executable is available directly at:

```bash
"$(brew --prefix devai)/bin/devai" --help
```

If the dry run shows only a link you intend to replace, make the Homebrew command
active:

```bash
brew link --overwrite devai
```

This overwrites conflicting links in the Homebrew prefix. It does not uninstall
the older package that created them; remove that package separately when it is no
longer needed.

## Reinstall or uninstall

Reinstall the current release if the keg is incomplete:

```bash
brew reinstall devai
```

Sign out before removing the CLI if the session should also be revoked locally:

```bash
devai auth logout
brew uninstall devai
```

The `tesserix/tap` tap can remain installed for upgrades or other Tesserix
formulae.

## Next steps

- [Bring your own Agent to DevAI](bring-your-own-agent.md)
- [Feedback and support](feedback-and-support.md)
- [Sandboxes and evaluations](../concepts/sandbox-and-evals.md)
