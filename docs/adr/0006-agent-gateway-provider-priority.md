# ADR 0006: Agent Gateway provider priority

## Context

Every Tesserix product needs the same LLM control-plane contract while choosing its own providers and models. Provider-specific agent definitions previously influenced runtime ordering, so a Claude model preference could move Anthropic ahead of a product configured with Vertex as primary. Settings also inferred fallback order from connector save order.

Current production scale is one DevAI API replica and two AI Gateway replicas; measured peak LLM RPS and a dedicated inference p99 SLO are not yet recorded. This decision adds no parallel requests or new infrastructure. Sequential fallback can add the failed primary's bounded call latency to the successful secondary call, so provider deadlines remain the limiting tail-latency control.

## Decision

- Every product declares one ordered chain: `llm_provider` is primary and `llm_fallback_provider` lists secondaries.
- Model or agent preferences select a capability tier; they never reorder providers. Each adapter maps that tier to a provider-native model.
- Production sets `llm_gateway_required=true`. Every provider adapter resolves a dedicated Solo.io Agent Gateway route. Missing gateway configuration fails closed; there is no direct-provider escape path.
- ADK runtimes receive only the product's adapter chain. They do not construct vendor clients.
- Settings persists and displays the effective primary and ordered fallbacks. Connected but unlisted providers follow the explicit fallbacks for backward compatibility.
- DevAI production uses Vertex Gemini as primary and Anthropic as secondary. All currently enabled tiers map to `gemini-2.5-flash` until additional Vertex models are enabled.

When Vertex fails with a retryable exception or an error response, the same logical request is attempted once on Anthropic with the Anthropic-native model for that tier. When Agent Gateway is unavailable, the request fails; bypassing governance is not an availability mechanism.

## Consequences

- Provider policy is predictable across legacy agents, YAML agents, and Tesserix ADK agents.
- Gateway availability is critical. Its existing two replicas are the shared failure domain; monitoring and rollout safety belong to the gateway service.
- Failover consumes secondary-provider quota only during primary failures. No new steady-state infrastructure cost is introduced.
- Existing connector records remain valid. The new fallback preference is additive and rollback is a code/config revert; no data migration is required.

Rejected alternatives: letting each agent pin a provider (contradicts product policy), direct provider fallback when the gateway is down (bypasses governance), and parallel hedged calls (doubles normal inference cost).

## Product adoption

Each product must set the gateway URL and required flag, configure gateway routes and credentials for every provider in its chain, select primary and fallbacks in Settings/config, map enabled models for each tier, and verify primary success plus secondary failover through the gateway before production rollout.
