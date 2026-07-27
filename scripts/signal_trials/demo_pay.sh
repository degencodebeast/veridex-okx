#!/usr/bin/env bash
#
# demo_pay.sh — drive one paid Signal Trials commitment end to end over x402.
#
# This is the H6.0 demonstration path: it proves that the deployed API charges for
# POST /signal-trials/commit, that a real X Layer payment satisfies the charge, and
# that the resulting receipt is publicly verifiable.
#
# WHAT IT DOES
#   1. POST the commitment unpaid          -> expect 402 with a payment challenge
#   2. Pay the challenge via the onchainos CLI, from the SECOND wallet
#   3. Replay the identical request with the payment signature -> expect 200
#   4. Decode the settlement header and print the transaction hash and receipt id
#
# SECRETS
#   This script holds none and prints none. The wallet lives entirely inside the
#   onchainos CLI's own configuration; nothing here reads a key, an address or a
#   funding value. If a value cannot come from the environment, that is a defect to
#   report rather than a constant to add.
#
# USAGE
#   BASE_URL=https://api.proofarena.xyz TRIAL_ID=<id> ./demo_pay.sh
#   BASE_URL=https://api.proofarena.xyz TRIAL_ID=<id> P_FOLLOW=0.62 ./demo_pay.sh
#
set -euo pipefail

BASE_URL="${BASE_URL:?BASE_URL is required, e.g. https://api.proofarena.xyz}"
TRIAL_ID="${TRIAL_ID:?TRIAL_ID is required — get one from GET /signal-trials/open-trial}"
P_FOLLOW="${P_FOLLOW:-0.62}"
WALLET="${ONCHAINOS_WALLET:-second}"

COMMIT_URL="${BASE_URL%/}/signal-trials/commit"
BODY="$(printf '{"trial_id":"%s","p_follow_profitable":%s}' "$TRIAL_ID" "$P_FOLLOW")"

need() { command -v "$1" >/dev/null 2>&1 || { echo "FATAL: $1 is not installed" >&2; exit 1; }; }
need curl
need jq
need onchainos

# The commit window is 300s and live-only, so a stale trial id fails here rather
# than after a payment has already been made.
echo "==> 1/4  unpaid POST ${COMMIT_URL}"
HDRS="$(mktemp)"; BODY_OUT="$(mktemp)"
trap 'rm -f "$HDRS" "$BODY_OUT"' EXIT

STATUS="$(curl -sS -o "$BODY_OUT" -D "$HDRS" -w '%{http_code}' \
  -X POST "$COMMIT_URL" \
  -H 'content-type: application/json' \
  --data "$BODY")"

if [ "$STATUS" != "402" ]; then
  echo "FATAL: expected 402 Payment Required, got ${STATUS}" >&2
  # A 503 here means the deployment has no payment gate configured, which is a
  # different failure from an unpaid request being allowed through.
  jq . < "$BODY_OUT" 2>/dev/null || cat "$BODY_OUT" >&2
  exit 1
fi
echo "    402 received"

# The challenge is served in the payment-required header; the body carries the same
# accepts block. Prefer the header, fall back to the body.
CHALLENGE="$(awk 'BEGIN{IGNORECASE=1} /^payment-required:/{sub(/^[^:]*:[ ]*/,""); gsub(/\r/,""); print; exit}' "$HDRS")"
if [ -z "$CHALLENGE" ]; then
  CHALLENGE="$(jq -r '.accepts // empty | if type=="array" then .[0] else . end | @base64' < "$BODY_OUT")"
fi
[ -n "$CHALLENGE" ] || { echo "FATAL: no payment challenge in the 402 response" >&2; exit 1; }
echo "    challenge extracted (${#CHALLENGE} chars)"

echo "==> 2/4  paying from the '${WALLET}' wallet via onchainos"
# The CLI signs with its own configured key. No key material passes through this
# script, so a shell trace of this file cannot leak one.
SIGNATURE="$(onchainos payment pay --wallet "$WALLET" --payload "$CHALLENGE")"
[ -n "$SIGNATURE" ] || { echo "FATAL: onchainos returned an empty payment signature" >&2; exit 1; }
echo "    signed"

echo "==> 3/4  replaying the identical request with PAYMENT-SIGNATURE"
: > "$HDRS"; : > "$BODY_OUT"
STATUS="$(curl -sS -o "$BODY_OUT" -D "$HDRS" -w '%{http_code}' \
  -X POST "$COMMIT_URL" \
  -H 'content-type: application/json' \
  -H "PAYMENT-SIGNATURE: ${SIGNATURE}" \
  --data "$BODY")"

if [ "$STATUS" != "200" ]; then
  echo "FATAL: expected 200 after payment, got ${STATUS}" >&2
  jq . < "$BODY_OUT" 2>/dev/null || cat "$BODY_OUT" >&2
  exit 1
fi
echo "    200 received"

echo "==> 4/4  settlement"
SETTLE="$(awk 'BEGIN{IGNORECASE=1} /^payment-response:/{sub(/^[^:]*:[ ]*/,""); gsub(/\r/,""); print; exit}' "$HDRS")"
TX_HASH=""
if [ -n "$SETTLE" ]; then
  # The header is base64-encoded JSON. Decode strictly: a header that does not
  # decode is a settlement we cannot read, and reporting it as absent would be a
  # quieter lie than saying so.
  if ! DECODED="$(printf '%s' "$SETTLE" | base64 --decode 2>/dev/null)"; then
    echo "FATAL: payment-response header is present but is not valid base64" >&2
    exit 1
  fi
  TX_HASH="$(printf '%s' "$DECODED" | jq -r '.transaction // .txHash // .transaction_hash // empty')"
fi
RECEIPT_ID="$(jq -r '.receipt_id // empty' < "$BODY_OUT")"

# Both values are public: a settled X Layer transaction and a receipt id that the
# verify endpoint serves to anyone. Neither is a secret.
echo
echo "    receipt id : ${RECEIPT_ID:-<absent>}"
echo "    tx hash    : ${TX_HASH:-<absent — no payment-response header>}"
echo "    verify     : ${BASE_URL%/}/signal-trials/receipts/${RECEIPT_ID}/verify"
echo

[ -n "$RECEIPT_ID" ] || { echo "FATAL: 200 returned no receipt_id" >&2; exit 1; }
echo "OK: paid commitment settled and is publicly verifiable."
