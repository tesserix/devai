# CHART-11: pin python:3.12-slim by digest for reproducible builds.
FROM python:3.12-slim@sha256:090ba77e2958f6af52a5341f788b50b032dd4ca28377d2893dcf1ecbdfdfe203 AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml .
COPY src/ src/
RUN pip install --no-cache-dir .
# Security scanners the security_expert agent shells out to. Python-native
# ones install cheaply here; without them every SAST/dependency scan
# silently returned zero findings (a false "clean" verdict).
RUN pip install --no-cache-dir bandit pip-audit

# CHART-11: pin python:3.12-slim by digest for reproducible builds.
FROM python:3.12-slim@sha256:090ba77e2958f6af52a5341f788b50b032dd4ca28377d2893dcf1ecbdfdfe203

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
