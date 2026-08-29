# syntax=docker/dockerfile:1.7
ARG ADK_BASE=ghcr.io/tesserix/base-python-adk-3.13:20260827@sha256:575f845a640619b19a4612fd2fd483b85547ebbe47793b28b971534de3d4cfb9

FROM ${ADK_BASE} AS wheels

USER root
ARG KUBECTL_VERSION=v1.35.6
ARG KUBECTL_SHA256=5d11e2ba01ea68ffd053f56e27738e2b4330013ee67f7e46c6da6c585d3c9926
RUN curl -fsSL "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl" \
        -o /usr/local/bin/kubectl \
    && echo "${KUBECTL_SHA256}  /usr/local/bin/kubectl" | sha256sum -c - \
    && chmod +x /usr/local/bin/kubectl

WORKDIR /build
COPY pyproject.toml .
COPY src/ src/
RUN python -m pip wheel --wheel-dir /wheels . bandit pip-audit

FROM ${ADK_BASE}

USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
RUN --mount=type=bind,from=wheels,source=/wheels,target=/wheels,ro \
    python -m pip install --no-cache-dir --no-index --find-links=/wheels devai bandit pip-audit \
    && python -m pip check \
    && python -c "import importlib.metadata as m; assert m.version('tesserix-adk') == '0.51.0'"

COPY --from=wheels /usr/local/bin/kubectl /usr/local/bin/kubectl
COPY blueprints/ /app/blueprints/
COPY specializations/ /app/specializations/
COPY crews/ /app/crews/

WORKDIR /app
USER 10001:10001

EXPOSE 8080

ENTRYPOINT ["/usr/bin/tini", "--", "python", "-m", "devai"]
CMD ["serve"]
