import argparse
import hashlib
from importlib.resources import files
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import tomllib


def load_engine_manifest():
    resource = files("chessgrandmaster").joinpath("engine.toml")

    with resource.open("rb") as f:
        return tomllib.load(f)


def configured_engine():
    return dict(
        load_engine_manifest()["engine"]
    )


def platform_key(
    system=None,
    machine=None,
):
    system = (
        system
        or platform.system()
    ).lower()

    machine = (
        machine
        or platform.machine()
    ).lower()

    if system != "linux":
        raise RuntimeError(
            f"Unsupported Stockfish install platform: "
            f"{system}/{machine}"
        )

    if machine in {
        "x86_64",
        "amd64",
    }:
        return "linux_x86_64"

    if machine in {
        "aarch64",
        "arm64",
    }:
        return "linux_arm64"

    raise RuntimeError(
        f"Unsupported Stockfish architecture: {machine}"
    )


def platform_spec(
    system=None,
    machine=None,
):
    data = load_engine_manifest()

    key = platform_key(
        system=system,
        machine=machine,
    )

    try:
        return dict(
            data["platform"][key]
        )
    except KeyError as exc:
        raise RuntimeError(
            f"No Stockfish package configured for {key}"
        ) from exc


def sha256_file(path):
    path = Path(path)

    h = hashlib.sha256()

    with path.open("rb") as f:
        for block in iter(
            lambda: f.read(1024 * 1024),
            b"",
        ):
            h.update(block)

    return h.hexdigest()


def default_engine_install_path():
    explicit = os.environ.get(
        "CGM_ENGINE_INSTALL_PATH"
    )

    if explicit:
        return Path(
            explicit
        ).expanduser().resolve()

    cgm_home = os.environ.get(
        "CGM_HOME"
    )

    if cgm_home:
        return (
            Path(cgm_home)
            .expanduser()
            .resolve()
            / "bin"
            / "stockfish"
        )

    return (
        Path.home()
        / ".local"
        / "share"
        / "chessgrandmaster"
        / "bin"
        / "stockfish"
    ).resolve()


def resolve_installed_engine(value=None):
    candidates = []

    if value:
        candidates.append(
            str(value)
        )

    env_value = os.environ.get(
        "CGM_STOCKFISH"
    )

    if env_value:
        candidates.append(
            env_value
        )

    candidates.append(
        str(default_engine_install_path())
    )
    candidates.append("stockfish")

    for candidate in candidates:
        path = Path(candidate).expanduser()

        if path.is_file():
            return path.resolve()

        found = shutil.which(candidate)
        if found:
            return Path(found).resolve()

    raise FileNotFoundError(
        "Stockfish binary not found. "
        "Pass an explicit path, set CGM_STOCKFISH, "
        "run scripts/install_stockfish.sh, or install "
        "stockfish in PATH."
    )


def probe_uci_name(
    engine_path,
    timeout=10,
):
    engine_path = Path(
        engine_path
    ).expanduser().resolve()

    try:
        result = subprocess.run(
            [str(engine_path)],
            input="uci\nquit\n",
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except Exception as exc:
        raise RuntimeError(
            f"Unable to start engine: {engine_path}"
        ) from exc

    output = (
        result.stdout
        + "\n"
        + result.stderr
    )

    for line in output.splitlines():
        if line.startswith("id name "):
            return line[len("id name "):].strip()

    raise RuntimeError(
        "Engine did not return a UCI id name: "
        f"{engine_path}"
    )


def verify_engine_binary(
    engine_path,
):
    path = Path(
        engine_path
    ).expanduser().resolve()

    engine = configured_engine()

    actual_name = probe_uci_name(
        path
    )

    expected_name = engine[
        "uci_name"
    ]

    if actual_name != expected_name:
        raise RuntimeError(
            "Wrong chess engine version. "
            f"Expected {expected_name!r}, "
            f"got {actual_name!r} "
            f"from {path}"
        )

    digest = sha256_file(
        path
    )

    result = dict(
        engine
    )

    result.update({
        "path": str(path),
        "uci_name": actual_name,
        "binary_sha256": digest,
    })

    return result


def download_url():
    engine = configured_engine()
    spec = platform_spec()

    return (
        "https://github.com/"
        f"{engine['repository']}/releases/download/"
        f"{engine['tag']}/{spec['asset']}"
    )


def print_shell_install_spec():
    engine = configured_engine()
    spec = platform_spec()

    values = {
        "CGM_ENGINE_NAME":
            engine["name"],

        "CGM_ENGINE_VERSION":
            engine["version"],

        "CGM_ENGINE_LABEL":
            engine["label"],

        "CGM_ENGINE_TAG":
            engine["tag"],

        "CGM_ENGINE_OUTPUT_TAG":
            engine["output_tag"],

        "CGM_ENGINE_ASSET":
            spec["asset"],

        "CGM_ENGINE_BINARY":
            spec["binary"],

        "CGM_ENGINE_ARCHIVE_SHA256":
            spec["archive_sha256"],

        "CGM_ENGINE_URL":
            download_url(),
    }

    for key, value in values.items():
        print(
            f"{key}={shlex.quote(str(value))}"
        )


def cmd_info():
    engine = configured_engine()

    print(
        "Configured engine :",
        engine["label"],
    )

    print(
        "Release tag       :",
        engine["tag"],
    )

    print(
        "Output tag        :",
        engine["output_tag"],
    )

    try:
        spec = platform_spec()

        print(
            "Release asset     :",
            spec["asset"],
        )

        print(
            "Archive SHA256    :",
            spec["archive_sha256"],
        )
    except RuntimeError as exc:
        print(
            "Release asset     :",
            exc,
        )

    try:
        path = resolve_installed_engine()
        actual = probe_uci_name(
            path
        )

        print(
            "Installed path    :",
            path,
        )

        print(
            "Installed engine  :",
            actual,
        )
    except Exception:
        print(
            "Installed engine  : not found"
        )

    return 0


def cmd_verify(
    path=None,
):
    resolved = resolve_installed_engine(
        path
    )

    info = verify_engine_binary(
        resolved,
    )

    print(
        "Engine            :",
        info["uci_name"],
    )

    print(
        "Path              :",
        info["path"],
    )

    print(
        "SHA256            :",
        info["binary_sha256"],
    )

    print(
        "Status            : OK"
    )

    return 0


def cmd_install_path():
    print(default_engine_install_path())
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog="cgm-engine",
        description=(
            "Inspect and verify the configured "
            "ChessGrandmaster analysis engine."
        ),
    )

    sub = parser.add_subparsers(
        dest="command",
        required=True,
    )

    sub.add_parser(
        "info",
        help="Show configured and installed engine information.",
    )

    verify = sub.add_parser(
        "verify",
        help="Verify the installed engine.",
    )

    verify.add_argument(
        "path",
        nargs="?",
        help="Engine path; defaults to CGM_STOCKFISH/PATH.",
    )

    sub.add_parser(
        "install-path",
        help=(
            "Print the managed rootless engine "
            "installation path."
        ),
    )

    sub.add_parser(
        "shell-install-spec",
        help=argparse.SUPPRESS,
    )

    return parser


def main(argv=None):
    args = build_parser().parse_args(
        argv
    )

    if args.command == "info":
        return cmd_info()

    if args.command == "verify":
        return cmd_verify(
            path=args.path,
        )

    if args.command == "install-path":
        return cmd_install_path()

    if args.command == "shell-install-spec":
        print_shell_install_spec()
        return 0

    raise RuntimeError(
        f"Unknown command: {args.command}"
    )


if __name__ == "__main__":
    raise SystemExit(main())
