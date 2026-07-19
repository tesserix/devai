# devai-auth-bff — GIP-backed auth + reverse-proxy.
#
# Multi-stage build mirroring the mark8ly/homechef auth-bff pattern. No
# tesserix-internal base image dependency — we use the upstream golang
# builder and the distroless nonroot runtime so this builds clean in CI
# even before the org's base-image pipeline catches up.
#
# Go binary is PID 1; signal.Notify + shutdown context handle SIGTERM.

FROM golang:1.26-bookworm AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build \
    -trimpath \
    -ldflags="-s -w" \
    -o /out/server \
    ./cmd/server

FROM gcr.io/distroless/static-debian12:nonroot AS server
COPY --from=build --chown=65532:65532 /out/server /server
EXPOSE 8090
USER 65532:65532
ENTRYPOINT ["/server"]
