FROM python:3.12-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml .
COPY src/ src/
RUN pip install --no-cache-dir .

FROM python:3.12-slim

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
