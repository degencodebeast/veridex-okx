// Frozen Veridex API contract (Plan A, Task 0). Generated/pinned by the backend; do not hand-edit
// field names without updating veridex/api/schemas.py + contracts/fixtures + tests/test_api_contract.py.
//
// SEC-001 MIGRATION NOTE: `checks` is pinned here to its FINAL target shape — the 7 CheckId only,
// with CLV/performance in `metrics` (NOT in `checks`). Consumers (C1/C2/D) bind to this final shape.
// The live backend completes this migration in Plan A, Task 5 (WD-5b): until then it still emits the
// legacy `clv`-in-`checks` block. A strict-xfail test in tests/test_api_contract.py asserts the SEC-001
// target against the live response and flips to a hard failure the moment Task 5 lands.

export type CheckStatus = "pass" | "fail" | "pending" | "not_applicable";
export type CheckSeverity = "blocking" | "warning" | "info";
export type CheckId =
  | "evidence_integrity" | "llm_boundary" | "metrics_recomputed" | "manifest_bound"
  | "policy_obeyed" | "receipt_separation" | "anchor";

export interface CheckResult {
  id: CheckId; label: string; result: CheckStatus; severity: CheckSeverity;
  method: string; scope: string; evidence_refs: string[]; rules: Record<string, unknown>[];
  details: Record<string, unknown>; error: string | null;
}

export interface ProofArtifact {
  verifier_version: string;
  run: Record<string, unknown>;
  lineage: Record<string, unknown>;
  evidence: { evidence_hash: string; run_event_count: number };
  checks: Record<CheckId, CheckResult>;   // SEC-001 target: the 7 CheckId only; CLV lives in `metrics` (see migration note)
  anchor: { status: string; signature: string | null; cluster: string | null };
  metrics?: PerformanceMetrics | null;     // Performance Metrics block (CLV lives here)
}

export interface PerformanceMetrics {
  clv: number | null; sim_pnl: number | null; brier: number | null;
  max_drawdown: number | null; hit_rate: number | null; scored_actions: number;
  per_agent: Record<string, unknown>[];
}

export interface VerifyResult {
  run_id: string; verified: boolean; evidence_hash: string; recomputed_evidence_hash: string;
  manifest_hash: string; checks: Record<CheckId, CheckResult>; metrics: PerformanceMetrics | null;
  anchor: Record<string, unknown>; proof_card: ProofArtifact;
}

export interface LeaderboardRow {
  rank: number; agent_id: string; runs: number; avg_clv_bps: number | null; total_clv_bps: number;
  sim_pnl: number; brier: number | null; max_drawdown: number; action_count: number;
  valid_pct: number; proof_mode: string; eligibility_badge: string; anchor_status: string; source_mode: string;
  // WD-7 CLV confidence (display-only — NEVER a rank input, SEC-005):
  valid_count: number; clv_confidence: string; low_sample: boolean;
}
export interface LeaderboardResponse { rows: LeaderboardRow[]; }

export interface CockpitState {           // GET /competitions/{id}
  competition_id: string; status: string; config: Record<string, unknown>;
  roster: Record<string, unknown>[];
  leaderboard: Record<string, unknown>[]; latest_seq: number; anchor_status: string;
  run_id: string | null; proof_card: ProofArtifact | null; execution: Record<string, unknown> | null;
}

export interface InspectorRecord {
  run_id: string; agent_id: string; tick_seq: number;
  market_state: Record<string, unknown>; agent_action: Record<string, unknown>;
  recompute: Record<string, unknown>; clv_bps: number | string;
  untrusted_llm_metadata: Record<string, unknown>;   // "NOT AN INPUT TO SCORE" (SEC-007)
}

export interface FeedHealth {           // GET /feed/health — read-only telemetry (NOT scored)
  source_mode: string; events_per_min: number | null; ws_live: boolean;
  last_tick_ts: number | null; anchor_status: string;
  // WD-4 staleness view (additive — ws_live mirrors connected):
  txline_configured: boolean; connected: boolean; ticks_seen: number;
  fixture_id: number | null; staleness_s: number | null; stale: boolean;
}

export type RuntimeEventType =
  | "run_started" | "status_changed" | "action_emitted" | "schema_validation"
  | "run_completed" | "run_failed" | "model_call_started" | "model_call_completed"
  | "token_usage" | "latency" | "tool_call" | "retry" | "error" | "trace_link";
export interface RuntimeEvent {
  type: RuntimeEventType; agent_id: string; run_id: string | null; session_id: string | null;
  ts: number; channel: "OPS"; payload: Record<string, unknown>;
}
export interface RuntimeEventsResponse { events: RuntimeEvent[]; }  // object wrapper; bind to .events

// ---- MAKER LANE (maker_arena_result.v1) ----
// SEPARATE Maker*-prefixed family (SEC-005): MUST NOT reuse LeaderboardRow/ProofArtifact. The maker
// lane ranks on avg_toxicity_loss_bps (asc — lower is better), NOT any directional CLV.
// real_executable_edge_bps is ALWAYS null (no fill/PnL claim).
export interface MakerFalsificationWire {
  delta_bps: number; ci_low_bps: number; ci_high_bps: number; verdict: string; headline: string;
}
export interface MakerWindowClvAnalogWire {
  window_markout_bps: number; window_action_count: number; note: string;
}
export interface MakerLeaderboardRowWire {   // `maker_rank` (NOT `rank`); edge always null
  agent_id: string; avg_markout_bps: number; avg_toxicity_loss_bps: number;
  quote_count: number; scored: number; abstained: number;
  excluded: Record<string, unknown>; real_executable_edge_bps: null; maker_rank: number;
}
export interface MakerPerAgentWire {         // same fields, NO maker_rank
  agent_id: string; avg_markout_bps: number; avg_toxicity_loss_bps: number;
  quote_count: number; scored: number; abstained: number;
  excluded: Record<string, unknown>; real_executable_edge_bps: null;
}
export interface MakerArenaResultWire {
  protocol_id: string; config_hash: string; rung: string; fixtures: number[];
  per_agent: MakerPerAgentWire[]; maker_leaderboard: MakerLeaderboardRowWire[];
  falsification: MakerFalsificationWire;
  trade_aware_diagnostic: Record<string, unknown> | null;
  markout_adverse_decomposition: Record<string, unknown> | null;
  event_gate_timeline: Record<string, unknown> | null;
  window_clv_analog: MakerWindowClvAnalogWire;
  real_executable_edge_bps: null; fixture_universe_n: number; small_n_flag: boolean;
  excluded_by_reason: Record<string, unknown>; r2_bracket: Record<string, unknown> | null;
}
export interface MakerProofCardWire {
  rung: string; uncalibrated: boolean; headline: string;
  window_clv_analog: MakerWindowClvAnalogWire; falsification: MakerFalsificationWire;
  n_fixtures: number; small_n_note: string; trades_not_fills_caveat: string | null;
}
export interface MakerDiagnosticsWire {
  avg_markout_bps_label: string; avg_toxicity_loss_bps_label: string; real_executable_edge_bps_label: string;
}
export interface MakerArenaResultResponseWire {   // GET /maker/arena-result
  schema_version: string; lane: string; source_mode: string;
  rank_axis: string; rank_axis_direction: string;
  result: MakerArenaResultWire; proof_card: MakerProofCardWire; diagnostics: MakerDiagnosticsWire;
}

// ---- SIGNAL TRIALS ----
// The eight frontend-consumed models, frozen at veridex/api/signal_trials_schemas.py @ 2d303d4
// (SCHEMA_FREEZE: 8 models, 60 fields, ZERO defaults — extracted by Pydantic runtime introspection,
// not by reading source). Do not hand-edit field names without updating
// veridex/api/signal_trials_schemas.py + contracts/fixtures + tests/test_api_contract.py.
//
// EVERY NULLABLE FIELD IS REQUIRED WITH NO DEFAULT. A consumer that renders `0` for `null`
// publishes a fabricated result on a public leaderboard: a zero markout is a real FLAT outcome and
// a zero Brier is a PERFECT score. `pending` and `UNSCORED` carry IDENTICAL null metrics, so the
// `status` label is the only thing separating them.

export type TrialStatusWire = "pending" | "settled" | "UNSCORED";
export type TrialActionWire = "FOLLOW" | "FADE" | "ABSTAIN";
export type SignalTrialsCheckStatusWire = "pass" | "fail" | "pending";   // THREE-valued, no not_applicable
export type SignalTrialsSeasonStatusWire = "qualified" | "exploratory" | "no_season";
export type SeasonStateWire = "not_built" | "qualified" | "exploratory" | "no_season";

// GET /signal-trials/health — UNTYPED on the backend (dict[str, Any], no response_model) with
// EXACTLY these two fields. read_state's `detail` is projected away at the route: do NOT model it.
// Both `not_built` and `no_season` answer 404 on /season, so this is the ONLY place they differ.
export interface SignalTrialsHealthWire { ok: boolean; season_state: SeasonStateWire; }

export interface SignalTrialsRowWire {
  agent_id: string; qualified: boolean;
  avg_brier: number | null;               // null until something settles
  capped_avg_markout_bps: number | null;  // null until something settles
  active_decisions: number; active_coverage: number; unscored: number; is_control: boolean;
}
export interface SignalTrialsSeasonWire {   // GET /signal-trials/season — 404 no_season_published
  season_id: string; season_status: SignalTrialsSeasonStatusWire;
  combo: Record<string, unknown>; sample_size: number; rows: SignalTrialsRowWire[];
}
export interface OpenTrialWire {            // GET /signal-trials/open-trial — 404 no_open_trial
  trial_id: string; trial_mode: "live"; t0_ms: number; commit_deadline_ms: number;
  evidence: Record<string, unknown>; evidence_hash: string;
}
export interface TrialOutcomeWire {         // embedded in TrialWire; `entry` is NOT nullable
  trial_id: string; status: TrialStatusWire; entry: number;
  future: number | null; close_ts_ms: number | null; observation_lag_ms: number | null;
  follow_markout_bps: number | null; fade_markout_bps: number | null; follow_profitable: boolean | null;
}
// GET /signal-trials/trials/{id} — 404 trial_not_found. SEVEN fields: there is NO participants list
// on this model and no route enumerates a trial's receipts (`ParticipantSettlement` does not exist).
export interface TrialWire {
  trial_id: string; trial_mode: "live"; t0_ms: number; commit_deadline_ms: number;
  evidence: Record<string, unknown>; evidence_hash: string;
  outcome: TrialOutcomeWire | null;   // null ⇒ nothing computed at all (WEAKER than a recorded `pending`)
}
export interface CommitReceiptWire {
  receipt_id: string; trial_id: string; payer: string; p_follow_profitable: number;
  methodology_version: string | null; action: TrialActionWire; status: TrialStatusWire;
  brier: number | null; chosen_markout_bps: number | null;   // null unless status === "settled"
  committed_at_ms: number; commit_deadline_ms: number | null;
  trial_mode: string | null;          // UNCONSTRAINED str here, unlike the trial models' "live"
  body_hash: string; payment_tx_hash: string;
}
// GET /signal-trials/agents/{payer} — 404 agent_not_found on zero finalized commits (an all-zero
// record would assert the payer participated and scored nothing). `qualified` is false on live records.
export interface AgentRecordWire {
  payer: string; commits: number; settled: number; pending: number; unscored: number;
  avg_brier: number | null; capped_avg_markout_bps: number | null; qualified: boolean;
}
// GET /signal-trials/receipts/{id}/verify — 404 receipt_not_found. A failed check is a 200 carrying
// a `fail`, NEVER a 500. `checks` KEYS are published as unconstrained str over the frozen eight:
//   commit-time  body_hash  manifest  deadline_respected  live_mode
//   outcome      bar_version  law_version  evidence_equality  outcome_source
// `pending` does NOT distinguish "not settled yet" from "settled UNSCORED and never will be" —
// that lives in receipt.status, which must be read alongside the checks.
export interface VerifyReceiptWire {
  receipt_id: string; checks: Record<string, SignalTrialsCheckStatusWire>;
  receipt: CommitReceiptWire | null;  // null ONLY when the row's bytes are unreadable — still 200 + 8 verdicts
}
