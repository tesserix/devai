# CHART-11: pin python:3.12-slim by digest for reproducible builds.
FROM python:3.14-slim@sha256:cea0e6040540fb2b965b6e7fb5ffa00871e632eef63719f0ea54bca189ce14a6 AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl && rm -rf /var/lib/apt/lists/*

# CHART-11: pin kubectl and verify its SHA256 (same pattern as Dockerfile.sre).
# The API image needs it for the GitOps surface: services/argocd.py (release
# deploys) and adapters/gitops (argocd/kargo/flux tools + /mcp/gitops) all
# talk to the cluster through kubectl + CRD RBAC. Lands in the runtime stage
# via the existing COPY --from=builder /usr/local/bin.
ARG KUBECTL_VERSION=v1.31.4
ARG KUBECTL_SHA256=298e19e9c6c17199011404278f0ff8168a7eca4217edad9097af577023a5620f
RUN curl -fsSL "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl" \
        -o /usr/local/bin/kubectl \
    && echo "${KUBECTL_SHA256}  /usr/local/bin/kubectl" | sha256sum -c - \
    && chmod +x /usr/local/bin/kubectl

WORKDIR /app
COPY pyproject.toml .
COPY src/ src/
RUN pip install --no-cache-dir .
# Security scanners the security_expert agent shells out to. Python-native
# ones install cheaply here; without them every SAST/dependency scan
# silently returned zero findings (a false "clean" verdict).
RUN pip install --no-cache-dir bandit pip-audit

# CHART-11: pin python:3.12-slim by digest for reproducible builds.
FROM python:3.14-slim@sha256:cea0e6040540fb2b965b6e7fb5ffa00871e632eef63719f0ea54bca189ce14a6

RUN apt-get update && apt-get install -y --no-install-recommends \
    git && rm -rf /var/lib/apt/lists/*

COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY --from=builder /app/src /app/src
# Ship the YAML catalogues alongside the package so the PipelineService
# can load blueprints + specializations + curated templates from disk
# when DEVAI_PIPELINE_ENABLED=true.
COPY blueprints/ /app/blueprints/
COPY specializations/ /app/specializations/

WORKDIR /app

RUN useradd -m -u 1000 devai && chown -R devai:devai /app
USER 1000

EXPOSE 8080

ENTRYPOINT ["python", "-m", "devai"]
CMD ["serve"]
