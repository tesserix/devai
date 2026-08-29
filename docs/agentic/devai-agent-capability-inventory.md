# DevAI agent capability inventory

All 40 catalog agents use `runtime: tesserix_adk`, dynamic user-authorized provider
routing, and the existing DevAI tool dispatcher. The registry names are the agent name
with underscores converted to hyphens and `-agent` appended. Tool authority is an
allowlist: `none` means the agent can reason and return a typed handover but cannot call
a tool.

| Agent | Area | Risk | Specialized capability | Tool authority |
|---|---|---:|---|---|
| `requirements_analyst` | Planning | Low | Refine raw requirements, acceptance criteria, gaps, and assumptions | none |
| `document_analyzer` | Planning | Low | Extract requirements from PDFs, URLs, Markdown, OpenAPI, and diagrams | document read/parse |
| `tech_detector` | Planning | Low | Detect language, framework, data, test, CI, and deployment conventions | repository/file read |
| `product_director` | Planning | Medium | Convert refined requirements into an epic and sized user stories | issue create; repository read |
| `engineering_manager` | Planning | Medium | Produce file-level implementation plans and story dependencies | repository read |
| `supervisor` | Orchestration | Low | Build the role delegation plan for an ALM run | repository read; issue create |
| `orchestrator` | Orchestration | Low | Decide whether quality gates advance or loop back | none |
| `sre_supervisor` | Orchestration | Low | Plan an SRE investigation from topology and alerts | Kubernetes/alert read |
| `sre_orchestrator` | Orchestration | Low | Decide SRE escalation from severity, blast radius, and confidence | Kubernetes/alert read |
| `deployment_engineer` | Orchestration | High | Verify and operate Argo CD/Flux rollouts and rollback | GitOps read/write; SCM read/comment |
| `infra_provisioner` | Orchestration | High | Create deployment manifests, Helm, and infrastructure files | SCM read/write; validation |
| `release_manager` | Orchestration | Critical | Merge approved work, close the epic, and trigger deployment | SCM merge/close; Argo CD/Kargo/Flux write |
| `release_promoter` | Orchestration | Critical | Promote immutable freight through Kargo stages | Kargo promote; Argo CD read; SCM comment |
| `senior_developer` | Coding | Medium | Implement one story with tests on a feature branch and open a PR | SCM branch/write/PR; compile/lint/test/format |
| `db_engineer` | Coding | High | Create safe schema migrations and protect data integrity | SCM read/write; lint |
| `staff_reviewer` | Review | Medium | Review correctness, scope, quality, performance, and testability | diff/repository read; PR review |
| `security_expert` | Review | High | Run SAST, SCA, secret, container, OWASP, and SBOM gates | security scan; SCM read/review/issue |
| `qa_tester` | Review | Medium | Write and run acceptance and end-to-end tests | SCM read/write; Playwright/unit test |
| `ci_monitor` | Review | Medium | Observe CI and repair broken workflow configuration | CI read/rerun; SCM read/write |
| `intake` | Specialist | Low | Decide whether to proceed, clarify, or propose another plan | none |
| `negotiator` | Specialist | Low | Conduct bounded multi-turn requirements clarification | none |
| `prototyper` | Specialist | Low | Build a fast React/Vite prototype and open a PR | SCM branch/write/PR |
| `reflector` | Specialist | Low | Extract lessons from completed or failed runs into memory | agent memory read/write |
| `discovery` | SRE | Low | Discover cluster services, workloads, events, and topology | Kubernetes/Prometheus read |
| `infra_monitor` | SRE | Low | Detect node, pod, restart, OOM, storage, and workload health issues | Kubernetes/log read |
| `perf_monitor` | SRE | Low | Evaluate latency, throughput, errors, and saturation against SLOs | Prometheus read |
| `log_analyzer` | SRE | Low | Find error spikes, exceptions, and security signals in logs | Kubernetes log/Prometheus read |
| `observability_analyst` | SRE | Low | Query connected metrics, logs, and alerts across supported vendors | observability read |
| `root_cause_analyst` | SRE | Low | Rank root-cause hypotheses and causal chains from all signals | Kubernetes/log/metrics read |
| `reliability_analyst` | SRE | Low | Produce service SLO and error-budget scorecards | Prometheus/alert read |
| `capacity_planner` | SRE | Low | Forecast resource exhaustion and recommend scaling changes | metrics/Kubernetes resource read |
| `cost_analyzer` | SRE | Low | Surface cluster and cloud cost-saving opportunities | GCP billing/Kubernetes read |
| `cost_optimizer` | SRE | Low | Quantify concrete savings from idle or oversized cloud resources | GCP inventory/recommender/Kubernetes read |
| `deployment_inspector` | SRE | Low | Detect rollout, replica, autoscaler, image, and deploy-correlated issues | Kubernetes read |
| `gitops_auditor` | SRE | Low | Audit Argo CD, Flux, and Kargo drift and stalled operations | GitOps read |
| `security_auditor` | SRE | Medium | Audit workload exposure, TLS, posture, alerts, and GCP findings | Kubernetes/GCP/alert read |
| `remediation_planner` | SRE | Medium | Separate safe remediation steps from changes needing approval | Kubernetes read |
| `code_remediator` | SRE | Medium | Trace a production defect to code and prepare a repair PR | SCM read/write/issue/PR |
| `incident_responder` | SRE | High | Decide incident creation, safe remediation, or human escalation | Kubernetes read; Argo CD sync; issue create |
| `postmortem_writer` | SRE | Low | Produce a blameless timeline, root cause, actions, and follow-ups | SCM read; issue create |

## Shared runtime rules

- The local inventory is the reviewed admission list. Registry publication alone does
  not make an agent runnable. Each invocation maps the logical capability to its exact
  canonical `*-agent` name and fetches a fresh Registry `/resolved` bundle.
- Registry Skill, Prompt, Tool, and MCP references must resolve completely and match
  the reviewed capability contract. Missing Registry or gateway dependencies fail
  closed; DevAI does not fall back to local-only or direct-provider execution.
- Product callers should use `POST /a2a/v1/capabilities/{capability}`. The named A2A
  route remains compatible but passes through the identical admission checks.
- Provider choice is resolved per invocation from the authenticated principal's enabled
  connectors. Same-provider model fallback is attempted before cross-provider fallback.
- Strict connector mode never borrows a platform key for a human without authorization.
- SCM and settings are resolved from the same full principal, including tenant and team.
- Model output can call only tools listed above; enforcement occurs outside the model.
- Sandbox work prefers Kagent when that capability is enabled, while mock, replay,
  blocked, and dry-run modes remain available for evaluation and safety.
- High- and critical-risk workflow stages continue to surface approval state through the
  pipeline; registry/A2A discovery does not itself grant approval.
