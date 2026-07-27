# apps/web/Dockerfile — D-0 STATIC Next.js image (Wave 0 scaffold).
#
# Build context = apps/web (see compose.coolify.yml); apps/web/.dockerignore
# governs this context. Standalone pnpm project (own pnpm-lock.yaml), so the
# app dir is a self-sufficient context — no monorepo root needed.
#
# STATIC scaffold: authored before any app dependency, wired/built at D-1.
# NEXT_PUBLIC_* build args are injected via Coolify in D-1, never committed.

# ── base: node + PINNED pnpm, shared by every stage ──────────────────────
# pnpm is pinned to an exact 9.x: pnpm-lock.yaml is lockfileVersion '9.0'
# (the pnpm-9 format), and pnpm 10 changed lifecycle-script defaults — a
# floating corepack resolution would let the interpreter of
# --frozen-lockfile drift across builds with zero repo diff. One base stage
# keeps the pin identical everywhere pnpm runs.
FROM node:22-alpine AS base
WORKDIR /app
RUN corepack enable && corepack prepare pnpm@9.15.4 --activate

# ── deps: install with the lockfile only, for layer-cache stability ──────
FROM base AS deps
COPY package.json pnpm-lock.yaml .npmrc ./
# Native addons (keccak, bufferutil, utf-8-validate) ship no Node-22 prebuilds, so pnpm
# compiles them via node-gyp — which needs Python3 + a C toolchain absent from node:22-alpine.
# Install the toolchain as a virtual package and drop it in the SAME layer so the image stays lean.
RUN apk add --no-cache --virtual .build-deps python3 make g++ \
 && pnpm install --frozen-lockfile \
 && apk del .build-deps

# ── build: compile the Next.js app ───────────────────────────────────────
FROM base AS build
COPY --from=deps /app/node_modules ./node_modules
COPY . .
ENV NEXT_TELEMETRY_DISABLED=1
# NEXT_PUBLIC_* are inlined into the browser bundle at BUILD time (never runtime), so they must be
# ARG-declared HERE to reach `pnpm build` — a Compose/Coolify build arg is invisible to the stage
# otherwise. Promote to ENV so `next build` reads them from process.env. These are PUBLIC identifiers
# (an API origin + a Privy app id), never secrets.
ARG NEXT_PUBLIC_API_BASE
ARG NEXT_PUBLIC_PRIVY_APP_ID
ENV NEXT_PUBLIC_API_BASE=${NEXT_PUBLIC_API_BASE}
ENV NEXT_PUBLIC_PRIVY_APP_ID=${NEXT_PUBLIC_PRIVY_APP_ID}
# Fail CLOSED on an empty value: the live Coolify build path bypasses Compose's ${VAR:?} guard, so a
# missing var would otherwise silently bake a broken image (empty API base → SSR throws; empty Privy id
# → no login). .next/cache is build-machine cache — dead weight in the runtime image.
RUN : "${NEXT_PUBLIC_API_BASE:?required — set it as a Coolify Build Variable (empty bakes a broken image)}" \
 && : "${NEXT_PUBLIC_PRIVY_APP_ID:?required — set it as a Coolify Build Variable (empty disables login)}" \
 && pnpm build && rm -rf .next/cache

# ── runner: production-only artifacts, non-root ──────────────────────────
FROM base AS runner
ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1
COPY package.json pnpm-lock.yaml .npmrc ./
# Same native-addon compile need as the deps stage (keccak et al. are production deps) — the prod
# install also runs node-gyp; add+drop the toolchain in one layer so the runtime image stays lean.
RUN apk add --no-cache --virtual .build-deps python3 make g++ \
 && pnpm install --frozen-lockfile --prod \
 && apk del .build-deps
# --chown: Next writes .next/cache at runtime (ISR / image optimization);
# a root-owned .next would EACCES under USER node.
COPY --from=build --chown=node:node /app/.next ./.next
COPY next.config.mjs ./
# public/ exists as of H5.5 and carries SKILL.md, the one-prompt onboarding artifact the
# marketplace listing links to. WITHOUT this COPY the image silently drops it with no build
# error — `next start` serves public/ from the image, and every source-tree gate (pnpm test,
# pnpm build, docker build, /readyz) stays green while https://proofarena.xyz/SKILL.md 404s.
# PKT-DEC-C64 makes this line and the file a single acceptance criterion; lib/signal-trials-api.test.ts
# asserts this COPY is ACTIVE, so re-commenting it fails the suite rather than the deploy.
COPY --from=build --chown=node:node /app/public ./public
USER node
EXPOSE 3000
# Invoke the next binary directly — pnpm/corepack must NOT be on the runtime
# path (corepack under USER node would re-download pnpm from the registry at
# container start, making production startup depend on registry egress).
CMD ["node_modules/.bin/next", "start"]
