"""Seeded randomized exploration of the real DealSieve state machine."""

from __future__ import annotations

import os
import random
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from tests.model.world import World, random_action

pytestmark = pytest.mark.usefixtures("scripted_backend")
_marker_config = getattr(pytest.mark, "_config", None)
if _marker_config is not None:
    _marker_config.addinivalue_line("markers", "slow: runs the 2000-seed model trace corpus")
slow = pytest.mark.slow


def _world(tmp_path: Path, fixtures_dir: Path, policy, seed: int | str) -> World:
    return World(db_path=tmp_path / f"trace-{seed}.db", fixtures_dir=fixtures_dir, policy=policy, seed=seed)


def _run(world: World, actions: list[tuple[str, str | int | None]]) -> list[str]:
    for action, argument in actions:
        world.execute(action, argument)
    world.assert_liveness()
    return world.confirmed_defects


def _xfail_if_confirmed(findings: list[str]) -> None:
    if findings:
        pytest.xfail("confirmed product defect:\n" + "\n".join(findings))


def test_canonical_e2e_trace(fixtures_dir, policy, tmp_path) -> None:
    world = _world(tmp_path, fixtures_dir, policy, "canonical")
    findings = _run(
        world,
        [
            ("inject", "01_initial_offer"),
            ("inject", "02_price_drop"),
            ("approve", None),
            ("inject", "05_inspection_report"),
            ("tick", 4),
            ("tick", 4),
            ("tick", 4),
            ("tick", 0),
        ],
    )
    _xfail_if_confirmed(findings)


def test_random_traces(fixtures_dir, policy, tmp_path) -> None:
    """Run all 150 independent seeded worlds concurrently to keep the exhaustive test fast."""

    def run_seed(seed: int) -> list[str]:
        rng = random.Random(seed)
        world = _world(tmp_path, fixtures_dir, policy, seed)
        actions = [random_action(rng) for _ in range(rng.randint(8, 14))]
        return _run(world, actions)

    findings: list[str] = []
    with ThreadPoolExecutor(max_workers=16) as pool:
        for result in pool.map(run_seed, range(150)):
            findings.extend(result)
    _xfail_if_confirmed(findings)


@pytest.mark.parametrize(
    ("name", "actions"),
    [
        (
            "inspection_before_price_drop",
            [("inject", "05_inspection_report"), ("inject", "02_price_drop")],
        ),
        (
            "inspection_before_initial_offer_confirmed_defect",
            [("inject", "05_inspection_report"), ("inject", "01_initial_offer")],
        ),
        (
            "completed_message_replay_confirmed_defect",
            [("inject", "03_structural_single_tenant"), ("duplicate", None)],
        ),
        ("price_drop_twice", [("inject", "02_price_drop"), ("inject", "02_price_drop")]),
        (
            "approve_then_reject_same",
            [
                ("inject", "01_initial_offer"),
                ("inject", "02_price_drop"),
                ("approve", None),
                ("reject", None),
            ],
        ),
        (
            "reject_information_then_tick",
            [
                ("inject", "01_initial_offer"),
                ("inject", "02_price_drop"),
                ("reject", None),
                ("tick", 10),
            ],
        ),
        (
            "concurrent_approve",
            [
                ("inject", "01_initial_offer"),
                ("inject", "02_price_drop"),
                ("concurrent_approve", None),
            ],
        ),
        (
            "concurrent_ticks",
            [
                ("inject", "01_initial_offer"),
                ("inject", "02_price_drop"),
                ("approve", None),
                ("tick", 4),
                ("concurrent_ticks", None),
            ],
        ),
        (
            "notifier_retry",
            [
                ("inject", "01_initial_offer"),
                ("notifier_fail_next", None),
                ("inject", "02_price_drop"),
                ("retry_last", None),
            ],
        ),
        (
            "outbox_retry",
            [
                ("inject", "01_initial_offer"),
                ("inject", "02_price_drop"),
                ("outbox_fail_next", None),
                ("approve", None),
                ("approve", None),
            ],
        ),
    ],
)
def test_adversarial_traces(name, actions, fixtures_dir, policy, tmp_path) -> None:
    world = _world(tmp_path, fixtures_dir, policy, name)
    _xfail_if_confirmed(_run(world, actions))


@slow
@pytest.mark.skipif(
    os.environ.get("DEALSIEVE_RUN_SLOW_MODEL_TRACES") != "1",
    reason="set DEALSIEVE_RUN_SLOW_MODEL_TRACES=1 to run 2000 model traces",
)
def test_random_traces_2000(fixtures_dir, policy, tmp_path) -> None:
    def run_seed(seed: int) -> list[str]:
        rng = random.Random(seed)
        world = World(
            db_path=tmp_path / f"slow-trace-{seed}.db",
            fixtures_dir=fixtures_dir,
            policy=policy,
            seed=seed,
        )
        return _run(world, [random_action(rng) for _ in range(rng.randint(8, 14))])

    findings: list[str] = []
    with ThreadPoolExecutor(max_workers=16) as pool:
        for result in pool.map(run_seed, range(2000)):
            findings.extend(result)
    _xfail_if_confirmed(findings)
