#!/usr/bin/env bash
#
# demo_pay.sh — drive one paid Signal Trials commitment end to end over x402.
#
# This is the H6.0 demonstration path: it proves that the deployed API charges for
# POST /signal-trials/commit, that a real X Layer payment satisfies the charge, and
# that the resulting receipt is publicly verifiable.
#
# WHAT IT DOES
#   1. POST the commitment unpaid          -> require 402 and a PAYMENT-REQUIRED header
#   2. Select the payer account, then sign the challenge with the onchainos CLI
#   3. Replay the identical request with PAYMENT-SIGNATURE -> require 200
#   4. Require a decodable PAYMENT-RESPONSE with a real transaction, then print it
#
# SECRETS — what is and is not guaranteed
#   No key material is read, stored or printed by this script; the wallet lives
#   inside the onchainos CLI's own configuration. The signed authorization IS
#   spendable, so xtrace is disabled around it: bash xtrace expands assignment
#   values and would otherwise print the authorization even though no echo does.
#   That is enforced below, not merely asserted.
#
# USAGE
#   BASE_URL=https://api.proofarena.xyz \
#   TRIAL_ID=<id> \
#   ONCHAINOS_ACCOUNT_ID=<payer account id> \
#     ./demo_pay.sh
#
set -euo pipefail

BASE_URL="${BASE_URL:?BASE_URL is required, e.g. https://api.proofarena.xyz}"
TRIAL_ID="${TRIAL_ID:?TRIAL_ID is required — get one from GET /signal-trials/open-trial}"
# The CLI signs with the CURRENTLY SELECTED account and has no per-invocation wallet
# flag, so the payer must be selected explicitly. This is a real account id; the
# label "second" is not one.
ACCOUNT_ID="${ONCHAINOS_ACCOUNT_ID:?ONCHAINOS_ACCOUNT_ID is required — the payer account id, not a label}"
P_FOLLOW="${P_FOLLOW:-0.62}"

COMMIT_URL="${BASE_URL%/}/signal-trials/commit"
BODY="$(printf '{"trial_id":"%s","p_follow_profitable":%s}' "$TRIAL_ID" "$P_FOLLOW")"

need() { command -v "$1" >/dev/null 2>&1 || { echo "FATAL: $1 is not installed" >&2; exit 1; }; }
need curl
need jq
need onchainos

header_value() {
  # Case-insensitive header lookup; HTTP header casing is not significant.
  awk -v want="$1" 'BEGIN{IGNORECASE=1}
    tolower($0) ~ "^" tolower(want) ":" { sub(/^[^:]*:[ ]*/, ""); gsub(/\r/, ""); print; exit }' "$2"
}

HDRS="$(mktemp)"; BODY_OUT="$(mktemp)"
trap 'rm -f "$HDRS" "$BODY_OUT"' EXIT

# The commit window is 300s and live-only, so a stale trial id fails here rather
# than after a payment has already been made.
echo "==> 1/4  unpaid POST ${COMMIT_URL}"
STATUS="$(curl -sS -o "$BODY_OUT" -D "$HDRS" -w '%{http_code}' \
  -X POST "$COMMIT_URL" \
  -H 'content-type: application/json' \
  --data "$BODY")"

if [ "$STATUS" != "402" ]; then
  echo "FATAL: expected 402 Payment Required, got ${STATUS}" >&2
  # 503 here means the deployment has no payment gate configured, which is a
  # different failure from an unpaid request being allowed through.
  jq . < "$BODY_OUT" 2>/dev/null || cat "$BODY_OUT" >&2
  exit 1
fi
echo "    402 received"

# The complete encoded challenge is carried ONLY in PAYMENT-REQUIRED. The 402 body
# is {"error": ...} and contains no usable challenge, so there is no fallback to
# take: absence of the header is a hard failure.
CHALLENGE="$(header_value 'payment-required' "$HDRS")"
[ -n "$CHALLENGE" ] || {
  echo "FATAL: the 402 carried no PAYMENT-REQUIRED header — nothing to pay" >&2
  exit 1
}
echo "    challenge extracted from PAYMENT-REQUIRED (${#CHALLENGE} chars)"

echo "==> 2/4  selecting payer account and signing"
onchainos wallet switch "$ACCOUNT_ID" >/dev/null

# Verify the switch actually took, and treat an unreadable status as a FAILURE
# rather than a skip. Suppressing this read would mean signing with whatever
# account happened to be selected — which spends from the wrong wallet while the
# script claims it checked. The read command is `wallet status`; the selected id
# lives at .data.currentAccountId, not at the root.
STATUS_JSON="$(onchainos wallet status)"
SELECTED="$(printf '%s' "$STATUS_JSON" | jq -r '.data.currentAccountId // empty')"
[ -n "$SELECTED" ] || {
  echo "FATAL: could not read .data.currentAccountId from 'onchainos wallet status'" >&2
  exit 1
}
if [ "$SELECTED" != "$ACCOUNT_ID" ]; then
  echo "FATAL: wallet selection did not take — asked for ${ACCOUNT_ID}, active is ${SELECTED}" >&2
  exit 1
fi
echo "    payer account ${SELECTED} selected and verified"

# xtrace off from here: it expands assignment values and curl arguments, and the
# authorization below is spendable. This is the enforcement behind the claim above.
XTRACE_WAS_ON=0
case "$-" in *x*) XTRACE_WAS_ON=1 ;; esac
set +x

# v2 output contract: {authorization_header, header_name, scheme, wallet}. The raw
# authorization is one FIELD of that document, not the document.
PAY_JSON="$(onchainos payment pay --payload "$CHALLENGE")"
HEADER_NAME="$(printf '%s' "$PAY_JSON" | jq -r '.header_name // empty')"
if [ "$HEADER_NAME" != "PAYMENT-SIGNATURE" ]; then
  set +x
  echo "FATAL: CLI returned header_name '${HEADER_NAME:-<absent>}', expected PAYMENT-SIGNATURE" >&2
  exit 1
fi
AUTHORIZATION="$(printf '%s' "$PAY_JSON" | jq -r '.authorization_header // empty')"
[ -n "$AUTHORIZATION" ] || {
  set +x
  echo "FATAL: CLI returned an empty authorization_header" >&2
  exit 1
}
unset PAY_JSON
echo "    signed"

echo "==> 3/4  replaying the identical request with PAYMENT-SIGNATURE"
: > "$HDRS"; : > "$BODY_OUT"
STATUS="$(curl -sS -o "$BODY_OUT" -D "$HDRS" -w '%{http_code}' \
  -X POST "$COMMIT_URL" \
  -H 'content-type: application/json' \
  -H "PAYMENT-SIGNATURE: ${AUTHORIZATION}" \
  --data "$BODY")"
unset AUTHORIZATION
[ "$XTRACE_WAS_ON" -eq 1 ] && set -x

if [ "$STATUS" != "200" ]; then
  echo "FATAL: expected 200 after payment, got ${STATUS}" >&2
  jq . < "$BODY_OUT" 2>/dev/null || cat "$BODY_OUT" >&2
  exit 1
fi
echo "    200 received"

echo "==> 4/4  settlement"
# A 200 alone does not mean THIS request settled a payment: the route deliberately
# omits PAYMENT-RESPONSE on an idempotent replay, because no new payment occurred.
# Printing success for that case would claim a settlement the response disproves.
SETTLE="$(header_value 'payment-response' "$HDRS")"
[ -n "$SETTLE" ] || {
  echo "FATAL: 200 with no PAYMENT-RESPONSE — this request settled no payment." >&2
  echo "       An idempotent replay looks like this. It is not a demonstration of payment." >&2
  exit 1
}
if ! DECODED="$(printf '%s' "$SETTLE" | base64 --decode 2>/dev/null)"; then
  echo "FATAL: PAYMENT-RESPONSE is present but is not valid base64" >&2
  exit 1
fi
TX_HASH="$(printf '%s' "$DECODED" | jq -r '.transaction // .txHash // .transaction_hash // empty')"
[ -n "$TX_HASH" ] || { echo "FATAL: PAYMENT-RESPONSE carries no transaction" >&2; exit 1; }

RECEIPT_ID="$(jq -r '.receipt_id // empty' < "$BODY_OUT")"
[ -n "$RECEIPT_ID" ] || { echo "FATAL: 200 returned no receipt_id" >&2; exit 1; }

# Both values are public: a settled X Layer transaction and a receipt id the verify
# endpoint serves to anyone. Neither is a secret.
echo
echo "    receipt id : ${RECEIPT_ID}"
echo "    tx hash    : ${TX_HASH}"
echo "    verify     : ${BASE_URL%/}/signal-trials/receipts/${RECEIPT_ID}/verify"
echo
echo "OK: paid commitment settled and is publicly verifiable."
