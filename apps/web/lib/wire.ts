// THE contract binding. Mirrors contracts/veridex_api.contract.ts EXACTLY (the
// frozen Plan A wire shapes). Do not diverge: per-fixture parse tests in
// lib/wire.test.ts assert every contracts/fixtures/*.json parses into these types.
// The frontend view-model (lib/contracts.ts) is mapped from these in lib/api.ts.
import type { CheckId } from '@/lib/checks';

export type { CheckId };
export type CheckStatus = 'pass' | 'fail' | 'pending' | 'not_applicable';
export type CheckSeverity = 'blocking' | 'warning' | 'info';

export interface CheckResult {
  id: CheckId;
  label: string;
  result: CheckStatus;
  severity: CheckSeverity;
  method: string;
  scope: string;
  evidence_refs: string[];
  rules: Record<string, unknown>[];
  details: Record<string, unknown>;
  error: string | null;
}

export interface ProofArtifact {
  verifier_version: string;
  run: Record<string, unknown>;
  lineage: Record<string, unknown>;
  evidence: { evidence_hash: string; run_event_count: number };
  // SEC-001 target: the 7 CheckId only; CLV lives in `metrics` (see migration note).
  checks: Record<CheckId, CheckResult>;
  anchor: { status: string; signature: string | null; cluster: string | null };
  metrics?: PerformanceMetrics | null;
}

export interface PerformanceMetrics {
  clv: number | null;
  sim_pnl: number | null;
  brier: number | null;
  max_drawdown: number | null;
  hit_rate: number | null;
  scored_actions: number;
  per_agent: Record<string, unknown>[];
}

export interface VerifyResult {
  run_id: string;
  verified: boolean;
  evidence_hash: string;
  recomputed_evidence_hash: string;
  manifest_hash: string;
  checks: Record<CheckId, CheckResult>;
  metrics: PerformanceMetrics | null;
  anchor: Record<string, unknown>;
  proof_card: ProofArtifact;
}

export interface LeaderboardRow {
  rank: number;
  agent_id: string;
  runs: number;
  avg_clv_bps: number | null;
  total_clv_bps: number;
  sim_pnl: number;
  brier: number | null;
  max_drawdown: number;
  action_count: number;
  valid_pct: number;
  proof_mode: string;
  eligibility_badge: string;
  anchor_status: string;
  source_mode: string;
  // WD-7 CLV confidence (display-only — NEVER a rank input, SEC-005):
  valid_count: number;
  clv_confidence: string;
  low_sample: boolean;
}
export interface LeaderboardResponse {
  rows: LeaderboardRow[];
}

// GET /competitions/{id} — the contract's `CockpitState`, renamed here to avoid a
// clash with the frontend view-model `CockpitState` in lib/contracts.ts.
export interface CompetitionStateResponse {
  competition_id: string;
  status: string;
  config: Record<string, unknown>;
  roster: Record<string, unknown>[];
  leaderboard: Record<string, unknown>[];
  latest_seq: number;
  anchor_status: string;
  run_id: string | null;
  proof_card: ProofArtifact | null;
  execution: Record<string, unknown> | null;
}

// GET /competitions — the unfiltered list summary (CompetitionSummaryResponse). NO auth; optional
// `status` query only. Carries the raw config dict + lifecycle status + run_id (null until start).
export interface CompetitionSummaryWire {
  competition_id: string;
  status: string;
  config: Record<string, unknown>;
  run_id: string | null;
}

export interface InspectorRecord {
  run_id: string;
  agent_id: string;
  tick_seq: number;
  market_state: Record<string, unknown>;
  agent_action: Record<string, unknown>;
  recompute: Record<string, unknown>;
  clv_bps: number | string;
  // "NOT AN INPUT TO SCORE" (SEC-007):
  untrusted_llm_metadata: Record<string, unknown>;
}

export interface FeedHealth {
  // GET /feed/health — read-only telemetry (NOT scored).
  source_mode: string;
  events_per_min: number | null;
  ws_live: boolean;
  last_tick_ts: number | null;
  anchor_status: string;
  // WD-4 staleness view (additive — ws_live mirrors connected):
  txline_configured: boolean;
  connected: boolean;
  ticks_seen: number;
  fixture_id: number | null;
  staleness_s: number | null;
  stale: boolean;
}

export type RuntimeEventType =
  | 'run_started' | 'status_changed' | 'action_emitted' | 'schema_validation'
  | 'run_completed' | 'run_failed' | 'model_call_started' | 'model_call_completed'
  | 'token_usage' | 'latency' | 'tool_call' | 'retry' | 'error' | 'trace_link';

export interface RuntimeEvent {
  type: RuntimeEventType;
  agent_id: string;
  run_id: string | null;
  session_id: string | null;
  ts: number;
  channel: 'OPS';
  payload: Record<string, unknown>;
}
export interface RuntimeEventsResponse {
  events: RuntimeEvent[];
}

// ---- MAKER LANE (maker_arena_result.v1) ----
// The sealed MAKER envelope (GET /maker/arena-result). SEC-005 at the boundary: these are a
// SEPARATE, `Maker*`-prefixed type family — they MUST NOT reuse LeaderboardRow/LeaderboardResponse/
// ProofArtifact. The maker lane ranks on `avg_toxicity_loss_bps` (lower is better, `asc`), NOT on
// any directional CLV. `real_executable_edge_bps` is ALWAYS `null` (no fill/PnL claim — honesty).

export interface MakerFalsificationWire {
  delta_bps: number;
  ci_low_bps: number;
  ci_high_bps: number;
  verdict: string;
  headline: string;
}

export interface MakerWindowClvAnalogWire {
  window_markout_bps: number;
  window_action_count: number;
  note: string;
}

// One agent's quote-quality row. `maker_rank` (NOT `rank`) is the maker-lane placement.
// `real_executable_edge_bps` is typed `null` — never a number (no fill/PnL claim, SEC-005).
export interface MakerLeaderboardRowWire {
  agent_id: string;
  avg_markout_bps: number;       // diagnostic, NOT the rank axis
  avg_toxicity_loss_bps: number; // THE rank axis (asc — lower is better)
  quote_count: number;
  scored: number;
  abstained: number;
  excluded: Record<string, unknown>;
  real_executable_edge_bps: null;
  maker_rank: number;
}

// Per-agent aggregate row (same quote-quality fields, but NO `maker_rank` — ranking is applied
// only in `maker_leaderboard`).
export interface MakerPerAgentWire {
  agent_id: string;
  avg_markout_bps: number;
  avg_toxicity_loss_bps: number;
  quote_count: number;
  scored: number;
  abstained: number;
  excluded: Record<string, unknown>;
  real_executable_edge_bps: null;
}

export interface MakerArenaResultWire {
  protocol_id: string;
  config_hash: string;
  rung: string; // e.g. "MM-R1"
  fixtures: number[];
  per_agent: MakerPerAgentWire[];
  maker_leaderboard: MakerLeaderboardRowWire[];
  falsification: MakerFalsificationWire;
  trade_aware_diagnostic: Record<string, unknown> | null;
  markout_adverse_decomposition: Record<string, unknown> | null;
  event_gate_timeline: Record<string, unknown> | null;
  window_clv_analog: MakerWindowClvAnalogWire;
  real_executable_edge_bps: null; // top-level: always null (no fill/PnL claim)
  fixture_universe_n: number;
  small_n_flag: boolean;
  excluded_by_reason: Record<string, unknown>;
  r2_bracket: Record<string, unknown> | null;
}

export interface MakerProofCardWire {
  rung: string;
  uncalibrated: boolean;
  headline: string;
  window_clv_analog: MakerWindowClvAnalogWire;
  falsification: MakerFalsificationWire;
  n_fixtures: number;
  small_n_note: string;
  trades_not_fills_caveat: string | null;
  trade_aware_diagnostic_note: string | null;
  r2_overlay_label: string | null;
}

export interface MakerDiagnosticsWire {
  avg_markout_bps_label: string;       // "diagnostic_not_rank_axis"
  avg_toxicity_loss_bps_label: string; // "rank_axis_lower_is_better"
  real_executable_edge_bps_label: string; // "always_null_no_fill_or_pnl_claim"
}

// GET /maker/arena-result — the frozen `maker_arena_result.v1` envelope.
export interface MakerArenaResultResponseWire {
  schema_version: string; // "maker_arena_result.v1"
  lane: string;           // "maker"
  source_mode: string;    // "replay"
  rank_axis: string;      // "avg_toxicity_loss_bps"
  rank_axis_direction: string; // "asc"
  result: MakerArenaResultWire;
  proof_card: MakerProofCardWire;
  diagnostics: MakerDiagnosticsWire;
}

export interface ReplayPackFixtureMetaWire {
  fixture_id: number;
  home_team: string | null;
  away_team: string | null;
  kickoff_ts: number | null;
  label_source: string; // "captured" | "unavailable"
}

export interface ReplayPackInfoWire {
  pack_id: string;
  content_hash: string;
  provenance: string;
  is_genuine: boolean;
  fixtures: number[];
  fixture_metadata: ReplayPackFixtureMetaWire[];
}

export interface ReplayPackListResponseWire {
  packs: ReplayPackInfoWire[];
}

// ---- SIGNAL TRIALS ----
// The eight frontend-consumed models, frozen at veridex/api/signal_trials_schemas.py @ 2d303d4
// (SCHEMA_FREEZE: 8 models, 60 fields, ZERO defaults, extracted by Pydantic runtime introspection).
//
// EVERY NULLABLE FIELD BELOW IS REQUIRED WITH NO DEFAULT, and that is the whole point of the freeze:
// a frontend that renders `0` for `null` publishes a fabricated result on a public leaderboard,
// because a zero markout is a real FLAT outcome and a zero Brier is a PERFECT score. `| null` here
// is therefore load-bearing — it is never `| null | undefined` and never optional (`?`), so a
// missing key is a contract violation rather than a silently-tolerated absence.

// `pending` and `UNSCORED` carry IDENTICAL null metrics; only this label separates them (C26).
export type TrialStatusWire = 'pending' | 'settled' | 'UNSCORED';
export type TrialActionWire = 'FOLLOW' | 'FADE' | 'ABSTAIN';
// The verify map is THREE-valued — there is no `not_applicable` here (distinct from `CheckStatus`
// above, which belongs to the unrelated Plan-A proof checks).
export type SignalTrialsCheckStatusWire = 'pass' | 'fail' | 'pending';
// The typed season model admits THREE states; `PublishedState` has FOUR. `not_built` is excluded
// from the season document by design, so it only ever arrives on /signal-trials/health.
export type SignalTrialsSeasonStatusWire = 'qualified' | 'exploratory' | 'no_season';
export type SeasonStateWire = 'not_built' | 'qualified' | 'exploratory' | 'no_season';

// GET /signal-trials/health — UNTYPED on the backend (`dict[str, Any]`, no response_model) with
// EXACTLY these two fields. `read_state`'s `detail` is projected away at the route and never
// reaches a client, so it is deliberately NOT modelled here.
export interface SignalTrialsHealthWire {
  ok: boolean;
  season_state: SeasonStateWire;
}

export interface SignalTrialsRowWire {
  agent_id: string;
  qualified: boolean;
  avg_brier: number | null;              // null until something settles
  capped_avg_markout_bps: number | null; // null until something settles
  active_decisions: number;
  active_coverage: number;
  unscored: number;
  is_control: boolean;
}

// GET /signal-trials/season — 404 `no_season_published` under BOTH `not_built` and `no_season`.
export interface SignalTrialsSeasonWire {
  season_id: string;
  season_status: SignalTrialsSeasonStatusWire;
  combo: Record<string, unknown>;
  sample_size: number;
  rows: SignalTrialsRowWire[];
}

// The complete decision-time payload returned by CanonicalSignal.model_dump(). These are the only
// evidence fields agents receive; post-decision liquidity and soldRatioPercent are structurally
// excluded by the backend model.
export interface CanonicalSignalWire {
  t0_ms: number;
  chain_index: string;
  token_address: string;
  symbol: string;
  name: string;
  market_cap_usd: number;
  holders: number;
  top10_holder_percent: number;
  trigger_price: number;
  wallet_type: string;
  trigger_wallet_count: number;
  trigger_wallet_address: string;
  amount_usd: number;
}

// GET /signal-trials/open-trial — 404 `no_open_trial` when none is open.
export interface OpenTrialWire {
  trial_id: string;
  trial_mode: 'live';
  t0_ms: number;
  commit_deadline_ms: number;
  evidence: CanonicalSignalWire;
  evidence_hash: string;
}

// Embedded in TrialResponse. `status` is the ONLY thing separating a `pending` row from an
// `UNSCORED` one — every metric below is null on both.
export interface TrialOutcomeWire {
  trial_id: string;
  status: TrialStatusWire;
  entry: number;                       // NOT nullable
  future: number | null;
  close_ts_ms: number | null;
  observation_lag_ms: number | null;
  follow_markout_bps: number | null;
  fade_markout_bps: number | null;
  follow_profitable: boolean | null;
}

// GET /signal-trials/trials/{id} — 404 `trial_not_found`. SEVEN fields: there is NO participants
// list on this model and no route enumerates a trial's receipts. `ParticipantSettlement` does not
// exist at the frozen head — it is a design note, not a field.
export interface TrialWire {
  trial_id: string;
  trial_mode: 'live';
  t0_ms: number;
  commit_deadline_ms: number;
  evidence: CanonicalSignalWire;
  evidence_hash: string;
  // `null` means NOTHING was computed at all — a WEAKER statement than a recorded `pending`.
  outcome: TrialOutcomeWire | null;
}

export interface CommitReceiptWire {
  receipt_id: string;
  trial_id: string;
  payer: string;
  p_follow_profitable: number;
  methodology_version: string | null;
  action: TrialActionWire;
  status: TrialStatusWire;
  brier: number | null;              // null unless status === 'settled'
  chosen_markout_bps: number | null; // null unless status === 'settled'
  committed_at_ms: number;
  commit_deadline_ms: number | null;
  trial_mode: string | null;         // UNCONSTRAINED `str` here, unlike the trial models' 'live'
  body_hash: string;
  payment_tx_hash: string;
}

// GET /signal-trials/agents/{payer} — 404 `agent_not_found` on zero finalized commits, because an
// all-zero record would assert that this payer participated and scored nothing.
export interface AgentRecordWire {
  payer: string;
  commits: number;
  settled: number;
  pending: number;
  unscored: number;
  avg_brier: number | null;
  capped_avg_markout_bps: number | null;
  qualified: boolean; // §8.4 — always false on live records
}

// GET /signal-trials/receipts/{id}/verify — 404 `receipt_not_found`. A failed check is a 200
// carrying a `fail`, NEVER a 500. `checks` keys are published as UNCONSTRAINED `str` (CF-5), which
// is why the eight names are declared once in lib/signal-trials-api.ts.
export interface VerifyReceiptWire {
  receipt_id: string;
  checks: Record<string, SignalTrialsCheckStatusWire>;
  // null ONLY when the row's bytes are unreadable; that case still answers 200 with eight verdicts.
  receipt: CommitReceiptWire | null;
}
