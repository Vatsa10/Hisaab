#!/usr/bin/env bash
# Start the local Supabase stack (idempotent) and write the dev env files:
#   web/.env.local      - what the PWA needs (public URL + anon key + API URL)
#   .env.server.local   - what the API / worker need (DB URL, JWT secret, JWKS URL)
# Both files are gitignored. The JWT secret is written to a file only, never echoed.
set -euo pipefail

cd "$(dirname "$0")/.."
SUPABASE="npx --yes supabase@2.122.0"

$SUPABASE start >&2

ENV_DUMP="$($SUPABASE status -o env)"
get() { printf '%s\n' "$ENV_DUMP" | sed -n "s/^$1=\"\{0,1\}\([^\"]*\)\"\{0,1\}\r\{0,1\}$/\1/p" | head -n1; }

API_URL="$(get API_URL)"
ANON_KEY="$(get ANON_KEY)"
JWT_SECRET="$(get JWT_SECRET)"
DB_URL="$(get DB_URL)"

for v in API_URL ANON_KEY JWT_SECRET DB_URL; do
  if [ -z "${!v}" ]; then
    echo "could not read $v from 'supabase status -o env'" >&2
    exit 1
  fi
done

mkdir -p web
umask 077
cat > web/.env.local <<EOF
NEXT_PUBLIC_SUPABASE_URL=$API_URL
NEXT_PUBLIC_SUPABASE_ANON_KEY=$ANON_KEY
NEXT_PUBLIC_API_URL=http://localhost:8000
EOF

# Local Supabase signs tokens with ES256, so the API must verify them via JWKS. server/auth.py prefers
# SUPABASE_JWT_SECRET when it is set (HS256 only), so the secret is written commented out: uncomment it
# only if you also want HS256 tokens and remove the JWKS line.
cat > .env.server.local <<EOF
DATABASE_URL=$DB_URL
# SUPABASE_JWT_SECRET=$JWT_SECRET
SUPABASE_JWKS_URL=$API_URL/auth/v1/.well-known/jwks.json
EOF

echo "Supabase API URL : $API_URL"
echo "Anon key         : $ANON_KEY"
echo "JWT secret       : written to .env.server.local (not printed)"
echo "Wrote web/.env.local and .env.server.local"
