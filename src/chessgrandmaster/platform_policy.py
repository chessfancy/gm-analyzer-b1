"""Explicit resource policies for supported analysis platforms."""

from copy import deepcopy


GENERIC_POLICY = {
    "workers": 2,
    "threads": 1,
    "hash_mb": 256,
}

PLATFORM_PROFILES = {
    "deepnote": {
        "workers": 2,
        "threads": 1,
        "hash_mb": 768,
    },
    "codespaces": {
        "workers": 2,
        "threads": 1,
        "hash_mb": 1024,
    },
    "kaggle": {
        "workers": 4,
        "threads": 1,
        "hash_mb": 1536,
    },
    "molab": {
        "workers": 4,
        "threads": 1,
        "hash_mb": 1536,
    },
}


def resolve_platform_policy(
    profile=None,
    *,
    workers=None,
    threads=None,
    hash_mb=None,
):
    """Resolve a named profile, then apply explicit caller overrides."""
    if profile is None:
        policy = deepcopy(GENERIC_POLICY)
    else:
        key = str(profile).lower()
        if key not in PLATFORM_PROFILES:
            known = ", ".join(sorted(PLATFORM_PROFILES))
            raise ValueError(
                f"Unknown platform profile {profile!r}; choose one of {known}"
            )
        policy = deepcopy(PLATFORM_PROFILES[key])

    if workers is not None:
        policy["workers"] = workers
    if threads is not None:
        policy["threads"] = threads
    if hash_mb is not None:
        policy["hash_mb"] = hash_mb

    return policy


get_platform_policy = resolve_platform_policy
