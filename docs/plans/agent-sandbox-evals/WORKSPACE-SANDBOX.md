# Workspace sandbox — a self-managed environment agents (and people) work in

Companion to `IMPLEMENTATION-PLAN.md`. That plan makes a sandbox a **boundary
around a run**: pinned config, TTL, tool modes, isolation manifests (#179–#181).
This one adds the other half — a **place to work**: a live workspace with a
filesystem, a shell, a browser, a preview URL and an editor, self-hosted in our
own cluster, that both an agent and a human can drive.

Prior art read for this: [agent-infra/sandbox](https://github.com/agent-infra/sandbox)
(AIO Sandbox), Browser Use's [two ways to sandbox agents](https://browser-use.com/posts/two-ways-to-sandbox-agents),
McQuaid's [sandboxed agent worktrees](https://mikemcquaid.com/sandboxed-agent-worktrees-my-coding-and-ai-setup-in-2026/),
and a [practical security guide for agentic AI](https://manjit28.medium.com/sandboxing-agentic-ai-a-practical-security-guide-for-openclaw-and-agentic-ai-in-general-a794640d876e).

## 1. The one decision everything else follows from

Browser Use names two patterns:

| Pattern | Meaning | Cost |
|---|---|---|
| Isolate the **tool** | agent stays on our infra, risky operations (shell, browser, code) go to a sandbox it calls over HTTP/MCP | agent still holds credentials |
| Isolate the **agent** | the whole agent runs inside the sandbox holding no credentials, reaching the world only through a control plane that owns every secret | an extra hop, three services instead of one |

**DevAI does both, and they are not alternatives — they are the same picture at
two scopes.** The agent process is isolated (it already runs as a Job with
rescoped secrets, #180), and inside that isolation the dangerous *capabilities*
are further isolated behind a service it calls (this doc). The property to
preserve is theirs: *the agent should have nothing worth stealing and nothing
worth preserving.* Anything durable lives in the workspace volume or the control
plane; the pod is disposable.

## 2. What the workspace is

One container per sandbox, one port, several protocols — AIO Sandbox's central
design decision, and the right one. The reason is not packaging convenience: it
is that **a shared filesystem is the integration mechanism**. A file the browser
downloads is immediately visible to the shell, to the file API and to the agent,
with no copy step and no sync layer to get wrong.

```
sandbox-<id> pod
├── workspace volume  /workspace          ← PVC, git worktree, survives the run
├── shell             exec + session, non-root, no service-account token
├── file              read / write / list / search / replace
├── browser           CDP (programmatic) + VNC (a human can watch and take over)
├── code-server       human takeover in the same workspace the agent used
├── preview proxy     the app the agent just built, on a URL, before it merges
└── MCP endpoint      all of the above as tools, through our own hub
```

Everything is reached through the DevAI MCP Hub, so an agent needs no new
integration: a sandbox is a set of MCP legs with a `sandbox` label, subject to
the same tool gateway (#181), the same audit, the same budgets.

## 3. Where we deliberately differ from AIO Sandbox

AIO is a good reference and a bad default. Three of its choices are wrong for a
multi-tenant platform:

- **Auth is optional there.** Omit `SANDBOX_API_KEY` and the shell, VNC and
  Jupyter are open. Ours is closed by construction: the workspace listens only
  inside the pod, and every route is behind the existing session auth plus a
  per-sandbox capability token. There is no unauthenticated mode to forget.
- **`seccomp=unconfined`.** We keep the restricted pod security context and pay
  for the browser's syscalls with a narrow, explicit profile instead — or run
  that leg under gVisor, which we already have a node pool for.
- **Two replicas of one shared container.** A sandbox is per-tenant, per-owner
  and TTL-bounded. Sharing one workspace across callers destroys both the
  isolation and the reproducibility that make the eval numbers mean anything.

## 4. Egress — the control that actually matters

Today a sandbox gets a default-deny NetworkPolicy with no internet (#180). That
is safe and unusable: real work needs `pypi`, `npm`, `github`. The security
guide's answer is the right shape — not "open 443", but a **forward proxy with a
domain allowlist and full access logging**, with the NetworkPolicy permitting
egress *only to the proxy*.

That gives three things at once: the sandbox can install what it needs; every
outbound request is attributable and logged; and a blocked request returns a
legible 403 instead of a mysterious hang. The friction of adding a domain is a
feature, not a defect — it is what stops an allowlist eroding into allow-all.

**Shipped shape** (`src/devai/sandbox/egress.py`, `egress_proxy.py`): one proxy
pod per sandbox rather than one shared proxy, so a per-sandbox `allow_domains`
addition is genuinely per-sandbox and the access log needs no demultiplexing
before it joins that run's trace. The proxy is provisioned for every sandbox,
its allowlist arrives as a ConfigMap, and `HTTP(S)_PROXY`/`NO_PROXY` are pinned
into the sandboxed Job's env. A tool that ignores those variables does not
escape the allowlist — it fails closed against the NetworkPolicy.

Note the honest limit, which that guide states and we should record: domain
allowlisting cannot tell a `git clone` from a `git push` to an attacker's repo.
Exfiltration through an allowed domain is bounded by the credential broker
(§5), not by the proxy.

## 5. Credentials — the sandbox holds none

The control plane owns every secret. A sandbox receives, at provision time, a
short-lived capability token scoped to that sandbox, and nothing else. When the
agent needs a real credential — an SCM token, an LLM key — it asks the broker,
which mints a scoped, expiring one and records the grant. Production secrets are
already rescoped away at the manifest level (#180); this closes the other half.

## 6. Workspace persistence and worktrees

McQuaid's setup is the model for the code case: repositories are **not copied**
into the sandbox — a git worktree per task, on shared storage, is the workspace.
Reviewer and agent see identical files, and several agents can work competing
approaches in parallel without stepping on each other.

For us: one PVC per sandbox mounted at `/workspace`, containing a worktree of
the target repo at a pinned ref. It outlives individual runs within the
sandbox's TTL, so a failed run can be re-entered and inspected rather than
re-created from scratch. Snapshot on completion is what makes a failing eval
case debuggable a day later.

## 7. Human takeover

The point of VNC and code-server is not novelty. When an eval case fails, the
useful next action is to open the exact workspace, at the exact state, and look.
Everything else in the eval stack produces a number; this produces the thing the
number was an index into.

## 8. Issues

| Issue | Scope |
|---|---|
| Workspace runtime | the pod, `/workspace` PVC, shell + file services, capability token, lifecycle tied to the existing `sandboxes` table |
| Browser leg | CDP + VNC, gVisor or a narrow seccomp profile, screenshot/navigate/click as tools |
| MCP profile | workspace tools registered as `kind:Tool`, federated by the hub, gated by #181 |
| Egress proxy | allowlist forward proxy, NetworkPolicy pinned to it, access log to the run trace |
| Credential broker | short-lived scoped credentials on request, every grant recorded (extends #194) |
| Preview + takeover | preview proxy for the app under construction, code-server for a human, both behind session auth |
