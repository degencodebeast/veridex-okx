# ProofArena REST signal demo runbook

This is an operator checklist, not an authorization. It grants no deployment, signing, or payment authority.
Run it only against an already reviewed deployment, with a named human separately
authorizing the one state publication and exactly one payment. Never paste credentials, account
identifiers, payment headers, or signing material into this file or a terminal transcript.

The public web host is `https://proofarena.xyz`; the public API host is
`https://api.proofarena.xyz`. The frozen commit window is five-minute (`300000` ms) and the outcome
horizon is one-hour (`3600000` ms). Stop on every failed command or mismatch. `REST` identifies how
the signal was fetched; it makes no claim about the WebSocket path.

## 1. Preflight

Boundary: **container operator**. Start read-only at the public boundary and require both liveness
and durable readiness before any mutation:

```sh
curl -fsS https://api.proofarena.xyz/healthz
curl -fsS https://api.proofarena.xyz/readyz
```

The safe pre-open state is specifically HTTP `404` with `{"error":"no_open_trial"}`. HTTP `200`
means a trial is already open and this run must not create another. Every other status/body pair is
a refusal:

<!-- PREOPEN_CHECK_START -->
```sh
PREOPEN_BODY="$(mktemp)"
trap 'rm -f "$PREOPEN_BODY"' EXIT
if ! PREOPEN_STATUS="$(curl -sS -o "$PREOPEN_BODY" -w '%{http_code}' \
  https://api.proofarena.xyz/signal-trials/open-trial)"; then
  echo "STOP: unexpected pre-open curl failure; status/body artifacts are untrusted" >&2
  exit 1
fi
if [ "$PREOPEN_STATUS" = "404" ] && \
  jq -e 'type == "object" and keys == ["error"] and .error == "no_open_trial"' \
    "$PREOPEN_BODY" >/dev/null; then
  echo "expected safe pre-open state: 404 no_open_trial"
elif [ "$PREOPEN_STATUS" = "200" ]; then
  echo "STOP: a trial is already open; do not start a new-open sequence" >&2
  exit 1
else
  echo "STOP: unexpected pre-open status/body mismatch (HTTP $PREOPEN_STATUS)" >&2
  exit 1
fi
```
<!-- PREOPEN_CHECK_END -->

Inside the already deployed API container, use credentials already injected by the deployment
environment. Do not export or print them:

```sh
python /app/scripts/signal_trials/run_preflight.py \
  --out /var/lib/veridex/signal-trials/preflight_result.json
jq -e '.probe_status == "completed" and .season_status != "no_season"' \
  /var/lib/veridex/signal-trials/preflight_result.json
jq -e '.chain_index == "196" and (.bar == "1m" or .bar == "1H")' \
  /var/lib/veridex/signal-trials/preflight_result.json
```

If chain `196` is not the reviewed winner, stop: this producer defaults to `chainIndex=196`.

## 2. Seal

Boundary: **container operator**. Seal only the just-reviewed completed preflight artifact:

```sh
python /app/scripts/signal_trials/fetch_and_seal.py \
  --from-preflight /var/lib/veridex/signal-trials/preflight_result.json \
  --out /var/lib/veridex/signal-trials/packs/
```

Record the emitted sealed pack path and content hash. Do not continue if the path is ambiguous, the
hash is absent, or the command reports `no_season`, `REFUSED`, `ABORTED`, or `FAILED`.

## 3. Publish

Boundary: **container operator**. This is the first durable publication gate. A named human must
approve the exact sealed pack hash before replacing `<reviewed-sealed-pack-dir>`:

```sh
APPROVED_PACK_CONTENT_HASH="<reviewed-sealed-pack-content-hash>"
python /app/scripts/signal_trials/score_and_publish.py \
  --pack-dir <reviewed-sealed-pack-dir> \
  --expected-content-hash "$APPROVED_PACK_CONTENT_HASH" \
  --data-dir /var/lib/veridex/signal-trials

# Pack provenance remains local; the frozen public season schema has no hash field.
jq -e --arg expected "$APPROVED_PACK_CONTENT_HASH" \
  '.detail.approved_pack_content_hash == $expected' \
  /var/lib/veridex/signal-trials/published/state.json

LOCAL_SEASON_ID="$(jq -er '.season_id' \
  /var/lib/veridex/signal-trials/published/season.json)"
LOCAL_CHAIN="$(jq -er '.combo.chain_index' \
  /var/lib/veridex/signal-trials/published/season.json)"
LOCAL_BAR="$(jq -er '.combo.bar' \
  /var/lib/veridex/signal-trials/published/season.json)"
PUBLIC_SEASON="$(mktemp)"
trap 'rm -f "$PUBLIC_SEASON"' EXIT
curl -fsS https://api.proofarena.xyz/signal-trials/season > "$PUBLIC_SEASON"
jq -e \
  --arg season_id "$LOCAL_SEASON_ID" \
  --arg chain "$LOCAL_CHAIN" \
  --arg bar "$LOCAL_BAR" '
    .season_id == $season_id and
    .combo.chain_index == $chain and
    .combo.bar == $bar
  ' "$PUBLIC_SEASON"
rm -f "$PUBLIC_SEASON"
trap - EXIT
curl -fsS https://api.proofarena.xyz/readyz
```

No command here deploys code. Stop unless local state binds the reviewed pack hash and the public
season reports the same public season ID, chain, and bar. Pack provenance is intentionally not
added to the frozen five-key public season response.

## 4. Dry-run and open

Boundary: **container operator**. Fetching is separate from opening: the producer prints exactly one
raw JSON signal and never opens a trial.

```sh
python /app/scripts/signal_trials/fetch_latest_signal.py \
  > /tmp/proofarena-rest-signal.json
python /app/scripts/signal_trials/open_live_trial.py \
  --source rest \
  --dry-run \
  --signal-file /tmp/proofarena-rest-signal.json \
  --data-dir /var/lib/veridex/signal-trials \
  > /tmp/proofarena-open-dry-run.json
jq -e '.published == false' /tmp/proofarena-open-dry-run.json
```

The dry-run must say `published=false`; it makes no durable write. Compare its evidence hash and
fields with the reviewed raw signal. Only after a named human approves that exact dry-run may the
container operator perform the single open.

Every operator using this reviewed workflow must use the atomic lock below. Bypassing the lock is
unsupported. The lock coordinates reviewed operators; it does not add repository compare-and-swap
and cannot exclude an arbitrary writer that bypasses the workflow.

```sh
SIGNAL_TRIALS_DATA_DIR="${SIGNAL_TRIALS_DATA_DIR:-/var/lib/veridex/signal-trials}"
OPEN_LOCK="$SIGNAL_TRIALS_DATA_DIR/.rest-signal-open.lock"
OPEN_LOCK_HELD=0
OPEN_PRECHECK_BODY="$(mktemp)"
OPEN_SUMMARY="$(mktemp)"
PUBLIC_TRIAL_BODY="$(mktemp)"
cleanup_open_workflow() {
  cleanup_status=0
  rm -f "$OPEN_PRECHECK_BODY" "$OPEN_SUMMARY" "$PUBLIC_TRIAL_BODY" || cleanup_status=1
  if [ "$OPEN_LOCK_HELD" = "1" ]; then
    if rmdir "$OPEN_LOCK"; then
      OPEN_LOCK_HELD=0
    else
      cleanup_status=1
    fi
  fi
  return "$cleanup_status"
}
trap 'cleanup_open_workflow' EXIT
trap 'exit 1' HUP INT TERM

if ! mkdir "$OPEN_LOCK"; then
  echo "STOP: another reviewed opener holds the Signal trial mutation lock" >&2
  exit 1
fi
OPEN_LOCK_HELD=1

if ! OPEN_PRECHECK_STATUS="$(curl -sS -o "$OPEN_PRECHECK_BODY" -w '%{http_code}' \
  https://api.proofarena.xyz/signal-trials/open-trial)"; then
  echo "STOP: immediate pre-open curl failed; status/body artifacts are untrusted" >&2
  exit 1
fi
if [ "$OPEN_PRECHECK_STATUS" != "404" ] || \
  ! jq -e 'type == "object" and keys == ["error"] and .error == "no_open_trial"' \
    "$OPEN_PRECHECK_BODY" >/dev/null; then
  echo "STOP: immediate pre-open state is not exact 404 no_open_trial" >&2
  exit 1
fi

if ! python /app/scripts/signal_trials/open_live_trial.py \
  --source rest \
  --signal-file /tmp/proofarena-rest-signal.json \
  --data-dir "$SIGNAL_TRIALS_DATA_DIR" > "$OPEN_SUMMARY"; then
  echo "STOP: local trial open failed or was indeterminate" >&2
  exit 1
fi
if ! jq -e '
  type == "object" and
  .published == true and
  .trial_mode == "live" and
  (.trial_id | type == "string" and test("^trial_[0-9a-f]{24}_[0-9]+$")) and
  .decision_window_ms == 300000 and
  (.t0_ms | type == "number" and floor == .) and
  (.commit_deadline_ms | type == "number" and floor == .) and
  .commit_deadline_ms == (.t0_ms + .decision_window_ms) and
  (.evidence_hash | type == "string" and test("^[0-9a-f]{64}$"))
' "$OPEN_SUMMARY" >/dev/null; then
  echo "STOP: local open summary failed publication, id, window, hash, or deadline validation" >&2
  exit 1
fi
if ! TRIAL_ID="$(jq -er '.trial_id' "$OPEN_SUMMARY")" || \
  ! LOCAL_EVIDENCE_HASH="$(jq -er '.evidence_hash' "$OPEN_SUMMARY")" || \
  ! LOCAL_COMMIT_DEADLINE_MS="$(jq -er '.commit_deadline_ms' "$OPEN_SUMMARY")"; then
  echo "STOP: local open binding could not be extracted" >&2
  exit 1
fi

if ! PUBLIC_TRIAL_STATUS="$(curl -sS -o "$PUBLIC_TRIAL_BODY" -w '%{http_code}' \
  "https://api.proofarena.xyz/signal-trials/trials/$TRIAL_ID")"; then
  echo "STOP: direct public trial read failed; no payment handoff is allowed" >&2
  exit 1
fi
if [ "$PUBLIC_TRIAL_STATUS" != "200" ] || \
  ! jq -e \
    --arg trial_id "$TRIAL_ID" \
    --arg evidence_hash "$LOCAL_EVIDENCE_HASH" \
    --argjson commit_deadline_ms "$LOCAL_COMMIT_DEADLINE_MS" '
      type == "object" and
      .trial_id == $trial_id and
      .evidence_hash == $evidence_hash and
      .commit_deadline_ms == $commit_deadline_ms
    ' "$PUBLIC_TRIAL_BODY" >/dev/null; then
  echo "STOP: direct public trial does not match the local id, evidence hash, and deadline" >&2
  exit 1
fi

if ! cleanup_open_workflow; then
  echo "STOP: open workflow cleanup or lock release failed" >&2
  exit 1
fi
trap - EXIT HUP INT TERM
printf 'TRIAL_ID=%s\n' "$TRIAL_ID"
```

Copy only the value after `TRIAL_ID=` from this verified local handoff; never derive payment
identity from the mutable open-trial pointer. The buyer must act inside the five-minute (`300000`
ms) commit window. If the window is stale, stop and do not pay.

## 5. Explicit single payment

Boundary: **buyer workstation**, never the API container. The buyer reviews the unpaid `402`
challenge and explicitly authorizes exactly one payment from a separately selected wallet account.
Replace placeholders locally; do not paste their values into shared logs:

```sh
BASE_URL=https://api.proofarena.xyz \
TRIAL_ID=<public-trial-id> \
ONCHAINOS_ACCOUNT_ID=<payer-account-id> \
  ./scripts/signal_trials/demo_pay.sh
```

Do not retry after an indeterminate settlement or missing response. A retry could pay twice. Stop
unless the command proves one confirmed transaction and prints one public receipt id.

## 6. Receipt

Boundary: **buyer workstation**. These are public, read-only checks:

```sh
curl -fsS \
  https://api.proofarena.xyz/signal-trials/receipts/<public-receipt-id>/verify
curl -fsS \
  https://api.proofarena.xyz/signal-trials/trials/<public-trial-id>/receipts
```

Save the public receipt id, trial id, evidence hash, and transaction hash. Never save the payment
challenge, payment response header, wallet account identifier, or signing material.

## 7. One-hour settlement

Boundary: **container operator**. The frozen horizon is 60 minutes, but terminal evidence also
requires a confirmed eligible candle. With no eligible candle, the earliest honest terminal
`UNSCORED` evidence is **71 minutes** for `1m` and **130 minutes** for `1H`: one complete selected
bar plus the frozen ten-minute fetch grace after the horizon. Read the reviewed bar from the
published preflight; do not guess it. A process exit 0 proves only that the command applied its
gates; the validated result below proves whether this exact trial was recorded terminally.

```sh
TRIAL_ID="${TRIAL_ID:?TRIAL_ID is required from the verified open handoff}"
BAR="$(jq -er '.bar | select(. == "1m" or . == "1H")' \
  /var/lib/veridex/signal-trials/preflight_result.json)"
SETTLEMENT_DRY_RUN="$(mktemp)"
SETTLEMENT_LIVE_RESULT="$(mktemp)"
cleanup_settlement_workflow() {
  rm -f "$SETTLEMENT_DRY_RUN" "$SETTLEMENT_LIVE_RESULT"
}
trap 'cleanup_settlement_workflow' EXIT

python /app/scripts/signal_trials/settle_live_trials.py \
  --data-dir /var/lib/veridex/signal-trials \
  --chain-index 196 \
  --bar "$BAR" \
  --trial-id "$TRIAL_ID" \
  --dry-run > "$SETTLEMENT_DRY_RUN"

if ! jq -e --arg trial_id "$TRIAL_ID" '
  type == "object" and
  .dry_run == true and
  .eligible == 1 and
  (.trials | type == "array" and length == 1) and
  .trials[0].trial_id == $trial_id and
  .trials[0].recorded == false and
  (.trials[0].status == "settled" or .trials[0].status == "UNSCORED")
' "$SETTLEMENT_DRY_RUN" >/dev/null; then
  echo "STOP: dry-run did not find exactly one terminally eligible row for the captured trial" >&2
  exit 1
fi

python /app/scripts/signal_trials/settle_live_trials.py \
  --data-dir /var/lib/veridex/signal-trials \
  --chain-index 196 \
  --bar "$BAR" \
  --trial-id "$TRIAL_ID" > "$SETTLEMENT_LIVE_RESULT"

if ! jq -e --arg trial_id "$TRIAL_ID" '
  type == "object" and
  .dry_run == false and
  .eligible == 1 and
  (.trials | type == "array" and length == 1) and
  .trials[0].trial_id == $trial_id and
  .trials[0].recorded == true and
  .trials[0].settlements_recorded == 1 and
  (.trials[0].status == "settled" or .trials[0].status == "UNSCORED")
' "$SETTLEMENT_LIVE_RESULT" >/dev/null; then
  echo "STOP: live run did not record the captured trial as settled or UNSCORED" >&2
  exit 1
fi

cat "$SETTLEMENT_LIVE_RESULT"
cleanup_settlement_workflow
trap - EXIT
```

If candle coverage is incomplete, eligibility is zero or ambiguous, or either command refuses,
record nothing manually and wait for a later reviewed run.

## 8. QA

Boundary: either workstation, read-only. Recheck both service gates, the open-trial surface, the
receipt, and the public web application:

```sh
curl -fsS https://api.proofarena.xyz/healthz
curl -fsS https://api.proofarena.xyz/readyz
QA_OPEN_BODY="$(mktemp)"
trap 'rm -f "$QA_OPEN_BODY"' EXIT
QA_OPEN_STATUS="$(curl -sS -o "$QA_OPEN_BODY" -w '%{http_code}' \
  https://api.proofarena.xyz/signal-trials/open-trial)"
if [ "$QA_OPEN_STATUS" = "200" ]; then
  jq -e '.trial_id | type == "string"' "$QA_OPEN_BODY"
elif [ "$QA_OPEN_STATUS" = "404" ] && \
  jq -e '.error == "no_open_trial"' "$QA_OPEN_BODY" >/dev/null; then
  echo "QA: closed trial is no longer open, as expected"
else
  echo "STOP: unexpected QA open-trial status/body mismatch (HTTP $QA_OPEN_STATUS)" >&2
  exit 1
fi
curl -fsS \
  https://api.proofarena.xyz/signal-trials/receipts/<public-receipt-id>/verify
curl -fsS https://proofarena.xyz/trials
```

QA is evidence collection only. It authorizes no follow-up deploy, second open, second payment,
manual receipt edit, or forced settlement.
