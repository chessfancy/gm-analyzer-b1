from chessgrandmaster.platform_policy import resolve_platform_policy


def test_production_platform_profiles_have_one_thread_per_worker():
    assert resolve_platform_policy("deepnote") == {
        "workers": 2,
        "threads": 1,
        "hash_mb": 768,
    }
    assert resolve_platform_policy("codespaces") == {
        "workers": 2,
        "threads": 1,
        "hash_mb": 1024,
    }
    assert resolve_platform_policy("kaggle") == {
        "workers": 4,
        "threads": 1,
        "hash_mb": 1536,
    }
    assert resolve_platform_policy("molab") == {
        "workers": 4,
        "threads": 1,
        "hash_mb": 1536,
    }


def test_explicit_platform_overrides_win():
    assert resolve_platform_policy(
        "molab",
        workers=2,
        threads=1,
        hash_mb=1024,
    ) == {
        "workers": 2,
        "threads": 1,
        "hash_mb": 1024,
    }


def test_generic_policy_does_not_infer_a_provider():
    assert resolve_platform_policy(None) == {
        "workers": 2,
        "threads": 1,
        "hash_mb": 256,
    }
