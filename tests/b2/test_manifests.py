from chessgrandmaster.b2.json_codec import canonical_json_sha256
from chessgrandmaster.b2.manifests import (
    AnalysisContract,
    JobInput,
    JobSpec,
    TournamentManifest,
    default_b1_v3_analysis_contract,
)
from chessgrandmaster.production_pipeline import PIPELINE_VERSION


def test_default_contract_matches_frozen_b1_v3_policy():
    contract = default_b1_v3_analysis_contract()

    assert contract.pipeline_version == 3
    assert contract.pipeline_version == PIPELINE_VERSION
    assert contract.engine == "stockfish19"
    assert contract.depth == 19
    assert contract.time_sec == 0
    assert contract.multipv == 1
    assert contract.snapshot_depths == (12, 14, 16, 18, 19)
    assert contract.scheduler == "game_affinity_lpt"
    assert contract.uci_archive is True


def test_analysis_contract_has_no_runtime_resource_fields():
    payload = default_b1_v3_analysis_contract().to_dict()
    forbidden = {
        "provider",
        "workers",
        "threads",
        "hash_mb",
        "Hash",
        "binary",
        "engine_path",
        "credentials",
    }
    assert forbidden.isdisjoint(payload)


def test_manifest_and_job_canonical_bytes_are_stable_and_provider_neutral():
    analysis = default_b1_v3_analysis_contract()
    job_input = JobInput(
        key="tournaments/example/revisions/0001/shards/0000/input.pgn",
        sha256="a" * 64,
        games=2,
        plies=8,
        canonical_game_fingerprints=("f" * 64, "e" * 64),
    )
    jobs = [
        JobSpec(
            schema_version="cgm-job-1",
            job_id="example-r0001-s0000",
            tournament_id="example",
            tournament_revision=1,
            shard_index=0,
            input=job_input,
            analysis=analysis,
            config_hash=canonical_json_sha256(analysis.to_dict()),
        ),
        JobSpec(
            schema_version="cgm-job-1",
            job_id="example-r0001-s0000",
            tournament_id="example",
            tournament_revision=1,
            shard_index=0,
            input=job_input,
            analysis=analysis,
            config_hash=analysis.config_hash(),
        ),
    ]
    assert jobs[0].to_canonical_json() == jobs[1].to_canonical_json()
    assert jobs[0].canonical_sha256() == jobs[1].canonical_sha256()
    assert jobs[0].config_hash == canonical_json_sha256(analysis.to_dict())

    manifest_a = TournamentManifest(
        schema_version="cgm-tournament-1",
        tournament_id="example",
        revision=1,
        name="Example",
        sources=(
            {"provider": "fixture", "external_id": "one"},
        ),
        canonical_pgn={
            "key": "tournaments/example/revisions/0001/canonical/tournament.pgn",
            "sha256": "b" * 64,
            "games": 2,
            "plies": 8,
        },
        properties={"is_otb": True},
        created_at="2026-09-21T00:00:00Z",
        canonicalization_policy="canonical_pgn_v1",
        sharding_policy="whole_game_plies_v1",
    )
    manifest_b = TournamentManifest(
        schema_version="cgm-tournament-1",
        tournament_id="example",
        revision=1,
        name="Example",
        sources=({"external_id": "one", "provider": "fixture"},),
        canonical_pgn={
            "plies": 8,
            "games": 2,
            "sha256": "b" * 64,
            "key": "tournaments/example/revisions/0001/canonical/tournament.pgn",
        },
        properties={"is_otb": True},
        created_at="2026-09-21T00:00:00Z",
        canonicalization_policy="canonical_pgn_v1",
        sharding_policy="whole_game_plies_v1",
    )
    assert manifest_a.to_canonical_json() == manifest_b.to_canonical_json()
    assert manifest_a.canonical_sha256() == manifest_b.canonical_sha256()


def test_job_spec_excludes_runtime_paths_and_transport_credentials():
    analysis = AnalysisContract(
        pipeline_version=3,
        engine="stockfish19",
        depth=19,
        time_sec=0,
        multipv=1,
        snapshot_depths=(12, 14, 16, 18, 19),
        scheduler="game_affinity_lpt",
        uci_archive=True,
    )
    job = JobSpec(
        schema_version="cgm-job-1",
        job_id="fixture-r0001-s0000",
        tournament_id="fixture",
        tournament_revision=1,
        shard_index=0,
        input=JobInput("input.pgn", "c" * 64, 1, 2, ("d" * 64,)),
        analysis=analysis,
        config_hash=analysis.config_hash(),
    )

    payload = job.to_dict()
    encoded = job.to_canonical_json()
    forbidden = {"provider", "workers", "threads", "Hash", "credentials", "engine_path"}
    assert forbidden.isdisjoint(payload)
    assert all(value not in encoded for value in ("C:/", "/tmp/", "access_key", "secret"))
