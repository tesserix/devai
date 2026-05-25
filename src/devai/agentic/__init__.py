"""Agentic control-plane status surface.

One module that aggregates the live state of the agentic stack
(agentregistry, agentgateway, kagent, ai-gateway) so the DevAI
dashboard can render a single 'Catalog → Gateway' page without
talking to each control plane directly from the browser. Browser
calls hit only ``/api/agentic/*`` on the webhook app; the webhook
fans out to in-cluster endpoints using either the existing registry
client or a small HTTP probe.

Why centralise: each control plane lives behind a different K8s
namespace + AuthorizationPolicy. The browser would need separate
credentials and CORS allowances for each. The webhook already has
egress allow-rules to all three namespaces (charts/thirdparty/
istio-config crossProductNamespaceAccess.devai = [kagent-system,
agentregistry-system, agentgateway-system]) so it's the cheapest
proxy point.
"""

from devai.agentic.status import AgenticStatus, fetch_agentic_status

__all__ = ["AgenticStatus", "fetch_agentic_status"]
