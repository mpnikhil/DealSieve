"""R10: deal_number and opportunity_event seq allocation must stay correct across two Repo
instances (two separate sqlite3 connections) writing to the same file concurrently, not just
within one Repo's own thread-safe lock."""

from __future__ import annotations

import threading

import pytest

from dealsieve.persistence import Repo
from dealsieve.schemas import EventType, Opportunity, OpportunityEvent, Property

N_THREADS = 8
N_PER_THREAD = 15


@pytest.fixture
def two_repos(tmp_path):
    path = tmp_path / "concurrent.db"
    repo_a = Repo(path)
    repo_a.init_schema()
    repo_b = Repo(path)
    return repo_a, repo_b


def test_two_repo_instances_allocate_deal_numbers_without_duplicates_or_errors(two_repos):
    repo_a, repo_b = two_repos
    prop = repo_a.upsert_property(Property(canonical_address="1 Test St", normalized_address="1 TEST ST"))

    deal_numbers: list[int] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker(repo: Repo) -> None:
        try:
            for _ in range(N_PER_THREAD):
                opp = repo.create_opportunity(Opportunity(property_id=prop.property_id, display_name="deal"))
                with lock:
                    deal_numbers.append(opp.deal_number)
        except BaseException as exc:  # noqa: BLE001 - we want to see and fail on anything
            with lock:
                errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(repo_a if i % 2 == 0 else repo_b,)) for i in range(N_THREADS)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert len(deal_numbers) == N_THREADS * N_PER_THREAD
    assert len(set(deal_numbers)) == len(deal_numbers), "duplicate deal_number allocated across Repo instances"

    # Every opportunity actually made it to disk with a unique deal_number: no unique-constraint
    # failure silently dropped a row.
    all_opps = repo_a.list_opportunities()
    assert len(all_opps) == N_THREADS * N_PER_THREAD


def test_two_repo_instances_allocate_event_seq_without_duplicates_or_errors(two_repos):
    repo_a, repo_b = two_repos
    prop = repo_a.upsert_property(Property(canonical_address="1 Test St", normalized_address="1 TEST ST"))
    opp = repo_a.create_opportunity(Opportunity(property_id=prop.property_id, display_name="deal"))

    seqs: list[int] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker(repo: Repo) -> None:
        try:
            for _ in range(N_PER_THREAD):
                event = repo.append_event(
                    OpportunityEvent(
                        opportunity_id=opp.opportunity_id, type=EventType.NOTE, summary="concurrent note"
                    )
                )
                with lock:
                    seqs.append(event.seq)
        except BaseException as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(repo_a if i % 2 == 0 else repo_b,)) for i in range(N_THREADS)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    total = N_THREADS * N_PER_THREAD
    assert len(seqs) == total
    assert sorted(seqs) == list(range(1, total + 1)), "event seq must be a gapless, duplicate-free 1..N sequence"
