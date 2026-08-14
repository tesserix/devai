# CHART-11: pin python:3.12-slim by digest for reproducible builds.
FROM python:3.12-slim@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36 AS builder
RUN python -m pip install --no-cache-dir --upgrade pip

RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
    git curl && rm -rf /var/lib/apt/lists/*

# CHART-11: pin kubectl and verify its SHA256 (same pattern as Dockerfile.sre).
# The API image needs it for the GitOps surface: services/argocd.py (release
# deploys) and adapters/gitops (argocd/kargo/flux tools + /mcp/gitops) all
# talk to the cluster through kubectl + CRD RBAC. Lands in the runtime stage
# via the existing COPY --from=builder /usr/local/bin.
ARG KUBECTL_VERSION=v1.35.6
ARG KUBECTL_SHA256=5d11e2ba01ea68ffd053f56e27738e2b4330013ee67f7e46c6da6c585d3c9926
RUN curl -fsSL "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl" \
        -o /usr/local/bin/kubectl \
    && echo "${KUBECTL_SHA256}  /usr/local/bin/kubectl" | sha256sum -c - \
    && chmod +x /usr/local/bin/kubectl

WORKDIR /app
COPY pyproject.toml .
COPY src/ src/
RUN pip install --no-cache-dir .
# The agent runtime, from its private repo. Optional: without a build token the
# image ships without the kit and runs on the in-tree dispatcher instead. The
# credential is unset in the same layer, and this stage is discarded regardless.
RUN --mount=type=secret,id=gh_token \
    if [ -s /run/secrets/gh_token ]; then \
        git config --global url."https://$(cat /run/secrets/gh_token)@github.com/".insteadOf "https://github.com/" ; \
        pip install --no-cache-dir ".[kit]" || echo "kit unavailable — running on the in-tree dispatcher" ; \
        rm -f /root/.gitconfig ; \
    fi
# Security scanners the security_expert agent shells out to. Python-native
# ones install cheaply here; without them every SAST/dependency scan
# silently returned zero findings (a false "clean" verdict).
RUN pip install --no-cache-dir bandit pip-audit

# CHART-11: pin python:3.12-slim by digest for reproducible builds.
FROM python:3.12-slim@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36
RUN python -m pip install --no-cache-dir --upgrade pip

RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
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
