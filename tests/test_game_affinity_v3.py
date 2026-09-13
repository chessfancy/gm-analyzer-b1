from chessgrandmaster.parallel_runner import (
    assign_games_to_workers,
    group_jobs_by_game,
)
from chessgrandmaster import analyzer as analyzer_module


def _jobs(game_id, count):
    return [
        {
            "job_id": f"{game_id}-{ply}",
            "game_id": game_id,
            "source_game_index": game_id,
            "ply": ply,
        }
        for ply in range(1, count + 1)
    ]


def test_group_jobs_sorts_each_game_by_ply():
    jobs = [
        {"job_id": "a3", "game_id": 1, "ply": 3},
        {"job_id": "a1", "game_id": 1, "ply": 1},
        {"job_id": "a2", "game_id": 1, "ply": 2},
    ]

    groups = group_jobs_by_game(jobs)

    assert [[job["ply"] for job in group] for group in groups.values()] == [[1, 2, 3]]


def test_lpt_assigns_whole_games_deterministically_and_balances_load():
    jobs = _jobs("a", 5) + _jobs("b", 4) + _jobs("c", 3) + _jobs("d", 2)

    assignments = assign_games_to_workers(jobs, workers=2)

    assert [
        list(dict.fromkeys(job["game_id"] for job in worker_jobs))
        for worker_jobs in assignments
    ] == [["a", "d"], ["b", "c"]]
    locations = {}
    for worker_id, worker_jobs in enumerate(assignments):
        for job in worker_jobs:
            locations.setdefault(job["game_id"], set()).add(worker_id)
        for game_id in {job["game_id"] for job in worker_jobs}:
            plies = [
                job["ply"]
                for job in worker_jobs
                if job["game_id"] == game_id
            ]
            assert plies == sorted(plies)

    assert locations == {
        "a": {0},
        "b": {1},
        "c": {1},
        "d": {0},
    }


def test_lpt_result_is_repeatable_for_same_input():
    jobs = _jobs("a", 5) + _jobs("b", 4) + _jobs("c", 3)

    first = assign_games_to_workers(jobs, workers=3)
    second = assign_games_to_workers(jobs, workers=3)

    assert first == second


def test_analyzer_dispatches_all_pending_jobs_once_not_per_checkpoint_batch(
    monkeypatch,
    tmp_path,
):
    jobs = _jobs("a", 5) + _jobs("b", 2)
    dispatches = []
    writes = []

    class FakeRunner:
        archive_manifests = []

        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def iter_analyze(self, pending):
            pending = list(pending)
            dispatches.append(pending)
            yield from ({"ok": True, "job_id": job["job_id"]} for job in pending)

    analyzer = analyzer_module.TournamentAnalyzer(
        tmp_path / "analysis.sqlite",
        tmp_path / "stockfish",
        batch_size=2,
    )
    monkeypatch.setattr(analyzer, "_load_jobs", lambda *args, **kwargs: jobs)
    monkeypatch.setattr(analyzer, "_write_results", lambda run_id, result: writes.append(result) or (len(result), 0))
    monkeypatch.setattr(analyzer, "_sample_resources", lambda *args, **kwargs: None)
    monkeypatch.setattr(analyzer_module, "ParallelLucasRunner", FakeRunner)

    result = analyzer.analyze(7)

    assert len(dispatches) == 1
    assert dispatches[0] == jobs
    assert [len(chunk) for chunk in writes] == [2, 2, 2, 1]
    assert result["completed"] == len(jobs)
