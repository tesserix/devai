# CHART-11: base image pinned by digest (node:24-alpine) so the build is
# reproducible and can't drift when the upstream tag is re-pushed.
FROM node:24-alpine@sha256:2bdb65ed1dab192432bc31c95f94155ca5ad7fc1392fb7eb7526ab682fa5bf14 AS base
WORKDIR /app

# CHART-10: copy the lockfile and use `npm ci` so the committed package-lock.json
# is honored (deterministic install). The old `npm install --frozen-lockfile`
# was a no-op — that flag does not exist for npm, the error was swallowed, and
# the full dependency tree was silently re-resolved at build time.
COPY package.json package-lock.json ./
RUN npm ci

COPY . .
RUN npm run build

FROM node:24-alpine@sha256:2bdb65ed1dab192432bc31c95f94155ca5ad7fc1392fb7eb7526ab682fa5bf14 AS runner
WORKDIR /app

ENV NODE_ENV=production

RUN addgroup --system --gid 1001 nodejs && adduser --system --uid 1001 nextjs

COPY --from=base /app/.next/standalone ./
COPY --from=base /app/.next/static ./.next/static
COPY --from=base /app/public ./public

USER nextjs

EXPOSE 3100
ENV PORT=3100

CMD ["node", "server.js"]
