"""Signal-trials API surface: free reads, the published-season repository, the honest commit stub.

The mandated RED block from the frozen plan is reproduced byte-identically below under
its own banner (`PKT-DEC-C8` part 2: frozen test content is exempt from lint and type
gates, and the mandated text wins where tooling disagrees). Everything else in this file
is ordinary lane-authored test content.
"""

import base64
import json

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from veridex.api.router import create_app
from veridex.api.server import create_server_app
from veridex.api.signal_trials_router import register_signal_trials_routes
from veridex.api.signal_trials_schemas import (
    CommitRequest,
    OpenTrialResponse,
    SignalTrialsRowModel,
    SignalTrialsSeasonResponse,
)
from veridex.config import Settings
from veridex.signal_trials.payments import (
    X_LAYER_MAINNET,
    FakeFacilitator,
    build_commit_price,
    load_x402_settings,
)
from veridex.signal_trials.published import (
    PUBLISHED_STATES,
    read_season,
    read_state,
    write_season,
    write_state,
)

# Two DISTINCT season documents. A single fixture document cannot distinguish a real
# reader from one that returns a hard-coded season, so every round-trip below is run
# against both and asserted equal to the one that was written.
SEASON_A = {
    "season_id": "season-alpha",
    "season_status": "qualified",
    "combo": {"chain_index": "501", "bar": "1m"},
    "sample_size": 61,
    "rows": [],
}
SEASON_B = {
    "season_id": "season-beta",
    "season_status": "exploratory",
    "combo": {"chain_index": "1", "bar": "1H"},
    "sample_size": 24,
    "rows": [
        {
            "agent_id": "agent-b",
            "qualified": False,
            "avg_brier": 0.21,
            "capped_avg_markout_bps": -13,
            "active_decisions": 7,
            "active_coverage": 0.35,
            "unscored": 2,
            "is_control": False,
        }
    ],
}

# Distinct open trials, for the same reason the two seasons exist.
TRIAL_A = {
    "trial_id": "trial-aaa",
    "trial_mode": "live",
    "t0_ms": 1_700_000_000_000,
    "commit_deadline_ms": 1_700_000_300_000,
    "evidence": {"symbol": "AAA", "market_cap_usd": 1234.5},
    "evidence_hash": "a" * 64,
}
TRIAL_B = {
    "trial_id": "trial-bbb",
    "trial_mode": "live",
    "t0_ms": 1_800_000_000_000,
    "commit_deadline_ms": 1_800_000_300_000,
    "evidence": {"symbol": "BBB", "holders": 42},
    "evidence_hash": "b" * 64,
}

_DATA_DIR_ENV = "SIGNAL_TRIALS_DATA_DIR"


@pytest.fixture(autouse=True)
def _unpublished_by_default(monkeypatch):
    """Keep every test hermetic against a developer's real data directory.

    Without this, a machine that happens to export ``SIGNAL_TRIALS_DATA_DIR`` would make
    the honest-404 tests read someone's published season. Tests that want a data dir set
    it themselves, which overrides this.
    """
    monkeypatch.delenv(_DATA_DIR_ENV, raising=False)


def _routed_app(**kwargs) -> FastAPI:
    """A bare app carrying ONLY the signal-trials routes, for surface-level assertions."""
    app = FastAPI()
    register_signal_trials_routes(app, **kwargs)
    return app


def _client_for(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://sig")


# --- the published-season repository: the artifact -> API contract ---


def test_unwritten_data_dir_reads_as_not_built(tmp_path):
    """Absence is reported as ``not_built`` — the honest state before any season is built."""
    assert read_state(tmp_path) == {"state": "not_built", "detail": {}}
    assert read_season(tmp_path) is None


def test_unconfigured_data_dir_reads_as_not_built():
    """No configured data dir at all is still an honest ``not_built``, never a crash."""
    assert read_state(None) == {"state": "not_built", "detail": {}}
    assert read_season(None) is None


@pytest.mark.parametrize("state", sorted(PUBLISHED_STATES))
def test_every_state_round_trips_with_its_own_detail(tmp_path, state):
    """All four states persist and read back — including a DISTINCT detail per state.

    The detail is varied with the state so that a writer which persists the state but
    drops (or hard-codes) the detail cannot pass.
    """
    detail = {"reason": f"detail-for-{state}", "checked": len(state)}
    write_state(tmp_path, state, detail)
    assert read_state(tmp_path) == {"state": state, "detail": detail}


def test_state_layout_is_published_state_json(tmp_path):
    written = {"probe": 1}
    write_state(tmp_path, "qualified", written)
    on_disk = json.loads((tmp_path / "published" / "state.json").read_text())
    assert on_disk == {"state": "qualified", "detail": written}


@pytest.mark.parametrize("season", [SEASON_A, SEASON_B])
def test_season_round_trips_exactly(tmp_path, season):
    """Two distinct documents: a reader returning a fixed season fails on the other one."""
    write_season(tmp_path, season)
    assert read_season(tmp_path) == season
    assert json.loads((tmp_path / "published" / "season.json").read_text()) == season


def test_rewriting_replaces_rather_than_accumulates(tmp_path):
    """Last write wins, for both files — a season is republished, not appended to."""
    write_season(tmp_path, SEASON_A)
    write_state(tmp_path, "qualified", {"n": 1})
    write_season(tmp_path, SEASON_B)
    write_state(tmp_path, "exploratory", {"n": 2})
    assert read_season(tmp_path) == SEASON_B
    assert read_state(tmp_path) == {"state": "exploratory", "detail": {"n": 2}}


def test_state_and_season_are_independent_files(tmp_path):
    """Writing one must not disturb the other: /health and /season answer separately."""
    write_season(tmp_path, SEASON_A)
    assert read_state(tmp_path) == {"state": "not_built", "detail": {}}
    write_state(tmp_path, "no_season", {"why": "retention too shallow"})
    assert read_season(tmp_path) == SEASON_A


def test_writes_leave_no_temporary_files_behind(tmp_path):
    """Atomic tmp+rename must rename, not litter: a stray .tmp is a half-published season."""
    write_state(tmp_path, "qualified", {"a": 1})
    write_season(tmp_path, SEASON_A)
    names = sorted(p.name for p in (tmp_path / "published").iterdir())
    assert names == ["season.json", "state.json"]


def test_writer_refuses_a_state_outside_the_frozen_set(tmp_path):
    """The four states are a closed set; the writers are where a bad one must stop."""
    with pytest.raises(ValueError, match="state"):
        write_state(tmp_path, "totally_made_up", {})
    assert not (tmp_path / "published").exists() or not (tmp_path / "published" / "state.json").exists()


def test_reader_refuses_a_state_outside_the_frozen_set(tmp_path):
    """A hand-edited artifact must not be echoed into the API as a season state."""
    published = tmp_path / "published"
    published.mkdir(parents=True)
    (published / "state.json").write_text(json.dumps({"state": "winning", "detail": {}}))
    with pytest.raises(ValueError, match="state"):
        read_state(tmp_path)


@pytest.mark.parametrize("reader", [read_state, read_season])
def test_reader_refuses_a_corrupt_artifact(tmp_path, reader):
    """Unreadable is not the same as absent — reporting ``not_built`` here would be a lie."""
    published = tmp_path / "published"
    published.mkdir(parents=True)
    (published / "state.json").write_text("{not json")
    (published / "season.json").write_text("{not json")
    with pytest.raises(ValueError):
        reader(tmp_path)


def test_data_dir_is_created_on_demand(tmp_path):
    """The persistent volume starts empty; publishing must not require a pre-made tree."""
    nested = tmp_path / "deep" / "not" / "there"
    write_state(nested, "qualified", {})
    assert read_state(nested)["state"] == "qualified"


def test_data_dir_accepts_a_string_path(tmp_path):
    """Callers read the location out of the environment, so a plain string must work."""
    write_season(str(tmp_path), SEASON_B)
    assert read_season(str(tmp_path)) == SEASON_B


# =============================================================================
# BEGIN FROZEN MANDATED RED BLOCK — implementation-plan.md H1.2 Step 1.
# Byte-identical to the mandate; exempt from lint/format/type gates per PKT-DEC-C8
# part 2 and PKT-DEC-C18 ruling 2. The two import lines that head the mandated
# block are NOT frozen (C8 part 3) and have been merged into this file's sorted
# import header. DO NOT reformat anything between these banners.
# =============================================================================

async def _client():
    return AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://t")

async def test_health_route_exists():
    async with await _client() as c:
        r = await c.get("/signal-trials/health")
        assert r.status_code == 200 and r.json()["ok"] is True and r.json()["season_state"] == "not_built"

async def test_season_honest_empty():
    async with await _client() as c:
        r = await c.get("/signal-trials/season")
        assert r.status_code == 404 and r.json()["error"] == "no_season_published"

async def test_open_trial_honest_404_when_none():
    async with await _client() as c:
        r = await c.get("/signal-trials/open-trial")
        assert r.status_code == 404 and r.json()["error"] == "no_open_trial"

async def test_commit_stub_never_free_success():
    async with await _client() as c:
        r = await c.post("/signal-trials/commit", json={"trial_id": "t1", "p_follow_profitable": 0.6})
        assert r.status_code in (402, 503)

def test_server_composition_requires_cors(monkeypatch):
    import pytest
    from veridex.api.server import create_server_app
    monkeypatch.delenv("CORS_ORIGINS", raising=False)
    with pytest.raises(ValueError, match="CORS_ORIGINS"):
        create_server_app()

# =============================================================================
# END FROZEN MANDATED RED BLOCK
# =============================================================================


# --- the remaining free reads, each 404ing with its OWN code ---
# A route that is simply not registered also yields 404, but with FastAPI's
# ``{"detail": "Not Found"}``. Asserting a distinct per-route error code is what
# separates "registered and honestly empty" from "never mounted at all", and a
# single hard-coded error code cannot satisfy all of them.


@pytest.mark.parametrize(
    ("path", "code"),
    [
        ("/signal-trials/season", "no_season_published"),
        ("/signal-trials/open-trial", "no_open_trial"),
        ("/signal-trials/trials/trial-unknown", "trial_not_found"),
        ("/signal-trials/agents/0xpayer", "agent_not_found"),
        ("/signal-trials/receipts/rcpt-unknown/verify", "receipt_not_found"),
    ],
)
async def test_free_reads_are_registered_and_honestly_empty(path, code):
    async with _client_for(_routed_app()) as c:
        r = await c.get(path)
        assert r.status_code == 404
        assert r.json() == {"error": code}


async def test_health_reports_ok_and_a_state_from_the_frozen_set():
    async with _client_for(_routed_app()) as c:
        r = await c.get("/signal-trials/health")
        assert r.status_code == 200
        assert r.json()["ok"] is True
        assert r.json()["season_state"] in PUBLISHED_STATES


# --- the commit stub: honest 503, never a free success ---


@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_commit_stub_is_503_on_both_methods(method):
    """Both gated methods answer; neither is a success and neither is a bare 404."""
    async with _client_for(_routed_app()) as c:
        r = await c.request(method, "/signal-trials/commit", json={"trial_id": "t", "p_follow_profitable": 0.5})
        assert r.status_code == 503
        assert r.json() == {"error": "trials_not_open"}


@pytest.mark.parametrize(
    "body",
    [
        None,
        {},
        {"trial_id": "t"},
        {"trial_id": "t", "p_follow_profitable": 0.0},
        {"trial_id": "t", "p_follow_profitable": 1.0},
        {"trial_id": "t", "p_follow_profitable": 0.5, "methodology_version": "v7"},
        {"trial_id": "t", "p_follow_profitable": -0.01},
        {"trial_id": "t", "p_follow_profitable": 1.01},
        {"trial_id": "t", "p_follow_profitable": "not-a-number"},
    ],
)
async def test_commit_never_succeeds_for_any_body(body):
    """No body — valid, invalid, absent, or boundary — buys a success while trials are closed."""
    async with _client_for(_routed_app()) as c:
        r = await c.post("/signal-trials/commit", json=body)
        assert r.status_code >= 400
        assert r.status_code != 402  # no payment layer is mounted on this app; 402 would be a fiction


# --- the published season, served from the repository ---


@pytest.mark.parametrize(
    ("state", "expect_season"),
    [("not_built", False), ("qualified", True), ("exploratory", True), ("no_season", False)],
)
async def test_persisted_state_survives_an_app_rebuild(tmp_path, monkeypatch, state, expect_season):
    """The mandated restart-state integration test, over all four persisted states.

    An intentional ``no_season`` stays distinguishable from ``not_built`` via health,
    while both honestly 404 on ``/season``.
    """
    write_state(tmp_path, state, {"note": state})
    if expect_season:
        write_season(tmp_path, SEASON_A)
    monkeypatch.setenv(_DATA_DIR_ENV, str(tmp_path))

    async with _client_for(create_app()) as c:  # rebuilt app over the SAME data dir
        health = await c.get("/signal-trials/health")
        assert health.status_code == 200
        assert health.json()["season_state"] == state

        season = await c.get("/signal-trials/season")
        if expect_season:
            assert season.status_code == 200
        else:
            assert season.status_code == 404
            assert season.json() == {"error": "no_season_published"}


@pytest.mark.parametrize("season", [SEASON_A, SEASON_B])
async def test_season_route_serves_the_published_document(tmp_path, season):
    """The served body is the PUBLISHED one, not a fabricated or hard-coded season.

    Run against two documents that differ in every field, so an implementation that
    returns a constant season fails on the second.
    """
    write_season(tmp_path, season)
    async with _client_for(_routed_app(data_dir=tmp_path)) as c:
        r = await c.get("/signal-trials/season")
        assert r.status_code == 200
        body = r.json()
        assert body["season_id"] == season["season_id"]
        assert body["season_status"] == season["season_status"]
        assert body["combo"] == season["combo"]
        assert body["sample_size"] == season["sample_size"]
        assert len(body["rows"]) == len(season["rows"])


async def test_a_season_published_after_startup_is_served_without_a_rebuild(tmp_path):
    """The scorer publishes into a running API; a start-up snapshot would serve a stale 404."""
    app = _routed_app(data_dir=tmp_path)
    async with _client_for(app) as c:
        assert (await c.get("/signal-trials/season")).status_code == 404
        assert (await c.get("/signal-trials/health")).json()["season_state"] == "not_built"

        write_season(tmp_path, SEASON_A)
        write_state(tmp_path, "qualified", {})

        assert (await c.get("/signal-trials/season")).status_code == 200
        assert (await c.get("/signal-trials/health")).json()["season_state"] == "qualified"


async def test_a_corrupt_season_artifact_is_never_served_as_an_empty_season(tmp_path):
    """Corruption must not degrade into a plausible-looking 404 or a fabricated body."""
    published = tmp_path / "published"
    published.mkdir(parents=True)
    (published / "season.json").write_text("{not json")
    async with _client_for(_routed_app(data_dir=tmp_path)) as c:
        with pytest.raises(ValueError):
            await c.get("/signal-trials/season")


# --- the open-trial discovery seam (H4.1 supplies the real provider) ---


@pytest.mark.parametrize("trial", [TRIAL_A, TRIAL_B])
async def test_open_trial_serves_exactly_what_the_provider_yields(trial):
    """Two distinct trials: a route that fabricates or caches a trial fails on the second."""
    app = _routed_app(open_trial_provider=lambda: OpenTrialResponse(**trial))
    async with _client_for(app) as c:
        r = await c.get("/signal-trials/open-trial")
        assert r.status_code == 200
        assert r.json() == trial


async def test_open_trial_404s_when_the_provider_has_nothing_open():
    app = _routed_app(open_trial_provider=lambda: None)
    async with _client_for(app) as c:
        r = await c.get("/signal-trials/open-trial")
        assert r.status_code == 404 and r.json() == {"error": "no_open_trial"}


# --- frozen schemas: the boundaries that keep the record truthful ---


@pytest.mark.parametrize("p", [0.0, 0.25, 0.5, 1.0])
def test_commit_request_accepts_the_whole_probability_range(p):
    assert CommitRequest(trial_id="t", p_follow_profitable=p).p_follow_profitable == p


@pytest.mark.parametrize("p", [-0.001, 1.001, -1, 2, float("nan"), float("inf"), float("-inf")])
def test_commit_request_refuses_a_probability_outside_zero_to_one(p):
    """NaN and the infinities included: the SDK's float trap has an analogue here.

    ``ge``/``le`` reject them because every comparison against NaN is False, but that
    is a property worth pinning rather than assuming — a probability of NaN would
    score as a real commitment.
    """
    with pytest.raises(ValidationError):
        CommitRequest(trial_id="t", p_follow_profitable=p)


def test_commit_request_methodology_version_is_optional():
    assert CommitRequest(trial_id="t", p_follow_profitable=0.5).methodology_version is None
    assert CommitRequest(trial_id="t", p_follow_profitable=0.5, methodology_version="v2").methodology_version == "v2"


@pytest.mark.parametrize("mode", ["replay", "historical", "paper", "LIVE", ""])
def test_open_trial_refuses_any_mode_but_live(mode):
    """Frozen §11: paid external commits are live-only, so discovery may only advertise live."""
    with pytest.raises(ValidationError):
        OpenTrialResponse(**{**TRIAL_A, "trial_mode": mode})


@pytest.mark.parametrize("status", ["qualified", "exploratory", "no_season"])
def test_season_response_accepts_the_three_published_statuses(status):
    season = SignalTrialsSeasonResponse(season_id="s", season_status=status, combo={}, sample_size=0, rows=[])
    assert season.season_status == status


def test_season_response_refuses_not_built():
    """``not_built`` is a health state, never a published season's status."""
    with pytest.raises(ValidationError):
        SignalTrialsSeasonResponse(season_id="s", season_status="not_built", combo={}, sample_size=0, rows=[])


def test_row_model_represents_unscored_honestly():
    """An agent with nothing scored yet carries nulls, never a fabricated zero."""
    row = SignalTrialsRowModel(
        agent_id="a",
        qualified=False,
        avg_brier=None,
        capped_avg_markout_bps=None,
        active_decisions=0,
        active_coverage=0.0,
        unscored=3,
        is_control=False,
    )
    assert row.avg_brier is None and row.capped_avg_markout_bps is None


# --- composition: the new lane must not disturb the existing app ---


async def test_registering_signal_trials_routes_leaves_existing_routes_alone():
    async with _client_for(create_app()) as c:
        assert (await c.get("/healthz")).status_code == 200


# =============================================================================
# The temporary stock 402 layer, mounted by ``server.py`` when X402_ENABLED=true.
# Replaced by SignalTrialsPaymentASGI at H4.1; until then this is what makes the
# deployed commit route answer with a genuine challenge instead of a free 200.
# =============================================================================

# DISTINCT payout addresses. ``pay_to`` decides where the money goes, and a suite that
# exercises it at a single literal cannot tell a correct implementation apart from one
# that hard-codes an address (PKT-TASK-H1-2-A1, R3-MINOR-1). Neither of these is
# H1.1's ``0x"a"*40``, so a survivor of that suite dies here.
PAYOUT_ALPHA = "0x" + "f0" * 20
PAYOUT_BETA = "0x" + "9c" * 20

_X402_ENV = {
    "CORS_ORIGINS": "https://proofarena.xyz",
    "APP_ENV": "development",
    "X402_ENABLED": "true",
    "PAY_TO_ADDRESS": PAYOUT_ALPHA,
}
_PROD_ENV = {**_X402_ENV, "APP_ENV": "production"}


def _dev_settings() -> Settings:
    return Settings(APP_ENV="development", AUTH_MODE="dev")


def _prod_settings() -> Settings:
    return Settings(APP_ENV="production", AUTH_MODE="privy", PRIVY_APP_ID="app", PRIVY_VERIFICATION_KEY="key")


def _served(env=None, *, settings=None, facilitator=None):
    """Build the SERVED app (the guard) with the stock 402 layer wired for tests."""
    return create_server_app(
        env=_X402_ENV if env is None else env,
        settings=_dev_settings() if settings is None else settings,
        x402_facilitator=FakeFacilitator() if facilitator is None else facilitator,
    )


def _challenge(response) -> dict:
    """Decode the base64 x402 challenge carried in the ``payment-required`` header."""
    return json.loads(base64.b64decode(response.headers["payment-required"]))


@pytest.fixture
async def x402_stub_app():
    async with _client_for(_served()) as client:
        yield client


# --- BEGIN FROZEN MANDATED TEST — implementation-plan.md H1.2, exempt per PKT-DEC-C8 ---

async def test_stock_402_layer_emits_challenge_on_both_methods(x402_stub_app):
    for method in ("GET", "POST"):
        r = await x402_stub_app.request(method, "/signal-trials/commit")
        assert r.status_code == 402 and "payment-required" in {k.lower() for k in r.headers}

# --- END FROZEN MANDATED TEST ---


# --- the money path: where the payment is actually routed ---


@pytest.mark.parametrize("payout", [PAYOUT_ALPHA, PAYOUT_BETA])
def test_build_commit_price_routes_to_the_configured_payout_address(payout):
    """The payout address must come FROM the configuration, not from a literal in the code.

    H1.1's suite pins ``pay_to`` at one accepted value, so an implementation returning a
    hard-coded address satisfies it. Two distinct addresses is the smallest change that
    makes that mutant observable.
    """
    settings = load_x402_settings({**_X402_ENV, "PAY_TO_ADDRESS": payout})
    assert build_commit_price(settings).pay_to == payout


@pytest.mark.parametrize("payout", [PAYOUT_ALPHA, PAYOUT_BETA])
async def test_the_emitted_challenge_names_the_configured_payout_address(payout):
    """End-to-end: the address a paying agent is told to send funds to is the configured one.

    Stronger than the unit assertion above — this is the value that actually reaches a
    payer, after the price has travelled through the resource server and the middleware.
    """
    async with _client_for(_served({**_X402_ENV, "PAY_TO_ADDRESS": payout})) as c:
        r = await c.get("/signal-trials/commit")
        assert r.status_code == 402
        assert _challenge(r)["accepts"][0]["payTo"] == payout


@pytest.mark.parametrize(("price", "amount"), [("$0.01", "10000"), ("$0.25", "250000")])
async def test_the_emitted_challenge_charges_the_configured_price(price, amount):
    """The charged amount tracks the configured price — the other half of the money path."""
    async with _client_for(_served({**_X402_ENV, "SIGNAL_TRIALS_COMMIT_PRICE": price})) as c:
        r = await c.get("/signal-trials/commit")
        assert _challenge(r)["accepts"][0]["amount"] == amount


async def test_the_emitted_challenge_settles_on_x_layer_mainnet(x402_stub_app):
    accepts = _challenge(await x402_stub_app.get("/signal-trials/commit"))["accepts"][0]
    assert accepts["network"] == X_LAYER_MAINNET and accepts["scheme"] == "exact"


# --- the paywall must cover the commit route and NOTHING else ---


@pytest.mark.parametrize(
    "path",
    [
        "/signal-trials/health",
        "/signal-trials/season",
        "/signal-trials/open-trial",
        "/signal-trials/trials/t1",
        "/signal-trials/agents/0xa",
        "/signal-trials/receipts/r1/verify",
    ],
)
async def test_free_reads_stay_free_behind_the_402_layer(x402_stub_app, path):
    """Discovery and verification are free (frozen §11). A paywall leaking onto them
    would put the trust surface behind the paid one."""
    r = await x402_stub_app.get(path)
    assert r.status_code in (200, 404)
    assert "payment-required" not in {k.lower() for k in r.headers}


async def test_the_402_layer_does_not_gate_the_rest_of_the_app(x402_stub_app):
    assert (await x402_stub_app.get("/healthz")).status_code == 200


# --- disabled, and the fail-closed refusals ---


async def test_commit_is_an_honest_503_when_x402_is_disabled():
    """No gate mounted means no challenge — but still never a free success.

    No facilitator is injected either: a disabled configuration must not need one.
    """
    guard = create_server_app(env={**_X402_ENV, "X402_ENABLED": "false"}, settings=_dev_settings())
    async with _client_for(guard) as c:
        r = await c.get("/signal-trials/commit")
        assert r.status_code == 503 and r.json() == {"error": "trials_not_open"}
        assert "payment-required" not in {k.lower() for k in r.headers}


def test_enabled_x402_without_any_facilitator_refuses_to_build():
    """A mounted gate with no facilitator raises RouteConfigurationError on the first
    commit — a 500, not a 402. Refusing at startup is the honest failure."""
    with pytest.raises(ValueError, match="facilitator"):
        create_server_app(env=_X402_ENV, settings=_dev_settings())


@pytest.mark.parametrize(
    ("env", "settings_factory"),
    [
        (_PROD_ENV, _prod_settings),
        (_PROD_ENV, _dev_settings),  # env says production, Settings does not
        (_X402_ENV, _prod_settings),  # Settings says production, env does not
    ],
)
def test_a_production_signal_from_either_source_refuses_the_fake(env, settings_factory):
    """Production-ness is decided fail-closed from BOTH sources.

    ``X402Settings`` cannot carry ``APP_ENV``, so it is derived at this call site. If it
    were read from only one of the two, a disagreement would let a fake facilitator back
    a configuration carrying a real payout address.
    """
    with pytest.raises(ValueError, match="FakeFacilitator"):
        create_server_app(env=env, settings=settings_factory(), x402_facilitator=FakeFacilitator())


@pytest.mark.parametrize("disabled", [None, "false", "0", "", "off"])
def test_production_refuses_to_start_with_payments_disabled(disabled):
    """A production deploy that never enabled x402 must refuse, not boot an ungated route.

    The refusal itself lives in ``load_x402_settings``, which means production has to be
    routed through that loader even when ``X402_ENABLED`` reads as absent or false —
    otherwise the check is simply never reached and the fail-closed rule is inert. Every
    spelling of "not enabled" is covered, since the cheap pre-check that decides whether
    to import the SDK reads them all as "no gate wanted".
    """
    env = {k: v for k, v in _PROD_ENV.items() if k != "X402_ENABLED"}
    if disabled is not None:
        env["X402_ENABLED"] = disabled
    with pytest.raises(ValueError, match="X402"):
        create_server_app(env=env, settings=_prod_settings())


def test_a_disabled_non_production_config_still_boots_without_the_sdk_extra():
    """The mirror image: no gate wanted outside production must not require the x402 SDK.

    ``okxweb3-app-x402`` is an optional extra, so the import has to stay behind this
    branch or an install without it cannot start at all.
    """
    guard = create_server_app(env={**_X402_ENV, "X402_ENABLED": "false"}, settings=_dev_settings())
    assert guard is not None


def test_production_without_okx_credentials_refuses_to_start():
    """The real facilitator is required in production, and it needs real credentials.

    Starting anyway would deploy a commit route that 500s on every payment attempt.
    """
    with pytest.raises(ValueError, match="OKX"):
        create_server_app(env=_PROD_ENV, settings=_prod_settings())


def test_the_refusal_never_echoes_the_configured_payout_address():
    """The fail-closed path must not copy a possibly-mispasted secret into a crash log."""
    secret_shaped = "0x" + "d" * 64
    with pytest.raises(ValueError) as exc:
        create_server_app(env={**_PROD_ENV, "PAY_TO_ADDRESS": secret_shaped}, settings=_prod_settings())
    assert secret_shaped not in str(exc.value) and "d" * 8 not in str(exc.value)
