# CHART-11: base image pinned by digest (node:24-alpine) so the build is
# reproducible and can't drift when the upstream tag is re-pushed.
FROM node:24-alpine@sha256:d32cdf619f63fe0471182d08996dd516c6275bb5fd31ae06e55a570bd9e1ad43 AS base
WORKDIR /app

# CHART-10: copy the lockfile and use `npm ci` so the committed package-lock.json
# is honored (deterministic install). The old `npm install --frozen-lockfile`
# was a no-op — that flag does not exist for npm, the error was swallowed, and
# the full dependency tree was silently re-resolved at build time.
COPY package.json package-lock.json ./
RUN npm ci

COPY . .
RUN npm run build

FROM node:24-alpine@sha256:d32cdf619f63fe0471182d08996dd516c6275bb5fd31ae06e55a570bd9e1ad43 AS runner
WORKDIR /app

ENV NODE_ENV=production

RUN addgroup --system --gid 1001 nodejs && adduser --system --uid 1001 nextjs \
    && rm -rf /usr/local/lib/node_modules /usr/local/bin/npm /usr/local/bin/npx /usr/local/bin/corepack /opt/yarn*

COPY --from=base /app/.next/standalone ./
COPY --from=base /app/.next/static ./.next/static
COPY --from=base /app/public ./public

USER nextjs

EXPOSE 3200
ENV PORT=3200

CMD ["node", "server.js"]
