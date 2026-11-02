"""Command-line installer for local Spack environments."""

from __future__ import annotations

import argparse
import contextlib
import gc
import glob
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Mapping, Optional, Sequence
from unittest import mock

DEFAULT_SPACK_URL = "https://github.com/spack/spack"
DEFAULT_SPACK_REF = "develop"
SPACK_URL_PATTERN = re.compile(r"^#.*\sspack-url: ([^\s]+)", re.MULTILINE)
SPACK_REF_PATTERN = re.compile(r"^#.*\sspack-ref: ([^\s]+)", re.MULTILINE)
SYSTEM_HOSTNAME_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "ursa": (re.compile(r"^ufe.*"),),
}


def determine_system(fqdn: Optional[str] = None) -> str:
    """Return the configured system name for a supported fully qualified hostname."""
    hostname = socket.getfqdn() if fqdn is None else fqdn
    for system, patterns in SYSTEM_HOSTNAME_PATTERNS.items():
        if any(pattern.match(hostname) for pattern in patterns):
            return system
    raise ValueError(
        f"unsupported host {hostname!r}"
    )


def find_manifest(cwd: Optional[Path] = None) -> Path:
    """Find spack.yaml in the working directory or its spack/ subdirectory."""
    working_directory = Path.cwd() if cwd is None else Path(cwd)
    candidates = (
        working_directory / "spack.yaml",
        working_directory / "spack" / "spack.yaml",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise FileNotFoundError(
        f"could not find spack.yaml in {working_directory} or {working_directory / 'spack'}"
    )


def read_spack_source(manifest: Path) -> tuple[str, str]:
    """Read the optional Spack URL and ref comments from a manifest."""
    content = manifest.read_text(encoding="utf-8")
    url_match = SPACK_URL_PATTERN.search(content)
    ref_match = SPACK_REF_PATTERN.search(content)
    return (
        url_match.group(1) if url_match else DEFAULT_SPACK_URL,
        ref_match.group(1) if ref_match else DEFAULT_SPACK_REF,
    )


def complete_path(text: str, state: int) -> Optional[str]:
    """Return a readline completion candidate for an installation path."""
    expanded = os.path.expanduser(text)
    directory, partial = os.path.split(expanded)
    search_directory = directory or "."
    matches = sorted(glob.glob(os.path.join(search_directory, partial) + "*"))
    if state >= len(matches):
        return None

    match = matches[state]
    if match.startswith("./"):
        match = match[2:]
    if os.path.isdir(match):
        match += os.sep
    return match


def resolve_install_root(value: str) -> Path:
    """Resolve and validate an installation root."""
    path = Path(value).expanduser().resolve()
    if path == path.parent:
        raise ValueError(
            "the filesystem root cannot be used as the installation directory"
        )
    return path


def prompt_install_root() -> Path:
    """Prompt for the shared install-tree and view root with path tab completion."""
    try:
        import readline
    except ImportError:
        readline = None

    if readline is not None:
        readline.set_completer(complete_path)
        readline.set_completer_delims("\t\n")
        readline.parse_and_bind("tab: complete")

    while True:
        entered_path = input("Installation base directory: ").strip()
        if entered_path:
            return resolve_install_root(entered_path)
        print("Please enter a directory path.", file=sys.stderr)


def prompt_compiler() -> str:
    """Prompt for the compiler name to export as $COMPILER; empty leaves it unset."""
    return input(
        "Compiler (e.g., 'gcc', 'intel', 'oneapi', 'oneapi@2025.3.1'); "
        "see common.yaml for available options; leave empty to let Spack auto-config: "
    ).strip()


class UserAbort(Exception):
    """Raised when the user declines to continue."""


class ReportedError(Exception):
    """Raised after a failure has already been reported to the user."""

    def __init__(self, returncode: int) -> None:
        super().__init__(returncode)
        self.returncode = returncode


EXPECTED_ERRORS = (
    FileNotFoundError,
    OSError,
    ValueError,
    subprocess.CalledProcessError,
    RuntimeError,
)


def report_error(error: BaseException, debug: bool = False) -> int:
    """Print a failure to stderr and return the process exit code."""
    if isinstance(error, UserAbort):
        print(f"nwstackify: {error}", file=sys.stderr)
        return 1
    if isinstance(error, KeyboardInterrupt):
        print("nwstackify: interrupted", file=sys.stderr)
        return 130
    if isinstance(error, EXPECTED_ERRORS):
        print(f"nwstackify: error: {error}", file=sys.stderr)
        if debug:
            traceback.print_exception(error)
    else:
        print(f"nwstackify: unexpected error: {error!r}", file=sys.stderr)
        traceback.print_exception(error)
    return 1


def finish_temporary_directory(path: Path, succeeded: bool, cleanup: bool) -> None:
    """Delete or keep the temporary directory.

    Deleted without asking if `cleanup` is set. Otherwise the user is asked when
    interactive; the default is to delete after success and to keep after failure.
    """
    delete = succeeded
    if cleanup:
        delete = True
    elif sys.stdin.isatty():
        suffix = "[Y/n]" if succeeded else "[y/N]"
        try:
            answer = input(f"Delete temporary directory {path}? {suffix}: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer:
            delete = answer in ("y", "yes")
    if delete:
        shutil.rmtree(path, ignore_errors=True)
    else:
        print(f"Temporary directory kept: {path}", file=sys.stderr)


def confirm_continue() -> bool:
    """Ask whether to proceed with the installation; anything but yes declines."""
    try:
        answer = input("Proceed with installation? [y/N]: ").strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")


def run_git(spack_root: Path, *args: str, debug: bool = False) -> None:
    """Run git in the checkout, hiding its output unless debugging or it fails."""
    command = ["git", "-C", str(spack_root), *args]
    if debug:
        subprocess.run(command, check=True)
        return
    result = subprocess.run(
        command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )
    if result.returncode:
        sys.stderr.write(result.stdout)
        raise subprocess.CalledProcessError(result.returncode, command)


def fetch_spack(spack_root: Path, url: str, ref: str, debug: bool = False) -> None:
    """Fetch the requested Spack ref into a fresh checkout directory."""
    spack_root.mkdir()
    run_git(spack_root, "init", "--quiet", debug=debug)
    run_git(spack_root, "remote", "add", "origin", url, debug=debug)
    run_git(spack_root, "fetch", "--quiet", "--depth=1", "origin", ref, debug=debug)
    run_git(
        spack_root,
        "-c",
        "advice.detachedHead=false",
        "checkout",
        "--quiet",
        "--detach",
        "FETCH_HEAD",
        debug=debug,
    )


def run_environment(
    spack_root: Path,
    manifest: Path,
    install_root: Path,
    env: Optional[Mapping[str, str]] = None,
    confirm: bool = False,
    debug: bool = False,
    spack_debug: bool = False,
) -> None:
    """Run Spack with `env` added to the process environment, restoring it afterwards."""
    # Keep the user cache, user config and bootstrap store inside the checkout so the
    # Spack instance is self-contained. These variables are read when Spack is
    # imported, which happens inside the patched environment below.
    self_contained = {
        "SPACK_USER_CACHE_PATH": str(spack_root / ".user-cache"),
        "SPACK_USER_CONFIG_PATH": str(spack_root / ".user-config"),
        **(env or {}),
    }
    # Spack runs in-process, so its environment is this process's environment.
    with mock.patch.dict(os.environ, self_contained):
        _run_environment(spack_root, manifest, install_root, confirm, debug, spack_debug)


def _run_environment(
    spack_root: Path,
    manifest: Path,
    install_root: Path,
    confirm: bool = False,
    debug: bool = False,
    spack_debug: bool = False,
) -> None:
    """Concretize and install the manifest in place with temporary overrides."""
    spack_lib = spack_root / "lib" / "spack"
    sys.path.insert(0, str(spack_lib))
    try:
        import spack.main
    except ImportError as error:
        raise RuntimeError(f"could not import Spack from {spack_root}") from error

    # The environment lives in the source directory itself, so relative paths in the
    # manifest (develop paths, model-packages) resolve against the real tree.
    environment_dir = manifest.parent

    print(f"Environment directory: {environment_dir}")

    # The environment's own scope outranks CONFIG.override(), so the view
    # location (e.g. the manifest's "view: true") is forced via the command-line scope.
    view_config = {"default": {"root": str(install_root / "view"), "link_type": "symlink"}}
    modules_root = install_root / "modules"
    core_compilers = (
        "gcc,oneapi,intel,intel-oneapi-compilers,aocc,llvm,nvhpc,apple-clang"
    )
    # Passed on every command line (not via CONFIG.override) so that install, view and
    # module commands all see the same install tree.
    config_overrides = (
        f"config:install_tree:root:{install_root / 'pkgs'}",
        'config:install_tree:projections:all:"{name}-{version}-{hash}"',
        f"view:{json.dumps(view_config)}",
        f"bootstrap:root:{spack_root / '.bootstrap'}",
    )
    module_overrides = (
        "modules:default:lmod:hierarchy::[]",
        f"modules:default:lmod:core_compilers::[{core_compilers}]",
        f"modules:default:roots:lmod:{modules_root}",
        "modules:default:arch_folder:false",
    )

    def config_args(overrides: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(arg for value in overrides for arg in ("--config", value))

    def run_spack(
        *args: str,
        global_args: tuple[str, ...] = (),
        overrides: tuple[str, ...] = (),
    ) -> None:
        # spack.main.main streams to the real stdout/stdin (unlike SpackCommand,
        # which captures output), keeping the interactive install keys working.
        debug_args = ("--debug",) if spack_debug else ()
        argv = [
            *debug_args,
            *config_args(config_overrides + overrides),
            *global_args,
            "-e",
            str(environment_dir),
            *args,
        ]
        if debug:
            # stderr, since stdout may be redirected to the receipt.
            print(
                f"[nwstackify debug] spack {shlex.join(argv)}\n"
                f"[nwstackify debug]   cwd: {os.getcwd()}\n"
                f"[nwstackify debug]   SITE={os.environ.get('SITE')!r} "
                f"COMPILER={os.environ.get('COMPILER')!r}",
                file=sys.stderr,
            )
        # spack.main.main() multiplies the GC threshold on every call, which overflows
        # after a few calls in one process, so restore it afterwards.
        gc_threshold = gc.get_threshold()
        try:
            rc = spack.main.main(argv)
        finally:
            gc.set_threshold(*gc_threshold)
        if rc:
            raise RuntimeError(f"spack {args[0]} failed with exit code {rc}")

    def write_config_receipt() -> None:
        receipt = install_root / "spack-config.txt"
        with receipt.open("w", encoding="utf-8") as handle:
            with contextlib.redirect_stdout(handle):
                run_spack("config", "blame", overrides=module_overrides)
        print(f"Wrote configuration receipt: {receipt}")

    write_config_receipt()
    print("Concretizing environment...")
    run_spack("concretize")
    if confirm and not confirm_continue():
        raise UserAbort("installation cancelled")
    print("Installing packages...")
    run_spack("install")
    print("Generating Lmod modules...")
    run_spack(
        "module", "lmod", "refresh", "--upstream-modules", "--yes-to-all",
        overrides=module_overrides,
    )


def install(
    cwd: Optional[Path] = None,
    install_root: Optional[Path] = None,
    compiler: Optional[str] = None,
    site: Optional[str] = None,
    debug: bool = False,
    confirm: bool = False,
    spack_debug: bool = False,
    cleanup: bool = False,
) -> None:
    """Run the complete installation workflow."""
    system: Optional[str] = site or None
    if system is None:
        try:
            system = determine_system()
        except ValueError as error:
            print(
                f"nwstackify: warning: {error}; $SITE will not be set "
                "(use --site to specify it)",
                file=sys.stderr,
            )
    manifest = find_manifest(cwd)
    url, ref = read_spack_source(manifest)
    if install_root is None:
        install_root = prompt_install_root()
    if compiler is None:
        compiler = prompt_compiler()
    if not compiler:
        print("Compiler not set; $COMPILER will not be defined")
    install_root.mkdir(parents=True, exist_ok=True)
    if system:
        print(f"Using system: {system}")
    print(f"Using Spack {ref} from {url}")

    env = {"SITE": system} if system else {}
    if compiler:
        env["COMPILER"] = compiler

    temporary_directory = Path(tempfile.mkdtemp(prefix="nwstackify-"))
    try:
        fetch_spack(temporary_directory / "spack", url, ref, debug=debug)
        run_environment(
            temporary_directory / "spack",
            manifest,
            install_root,
            env=env,
            confirm=confirm,
            debug=debug,
            spack_debug=spack_debug,
        )
    except BaseException as error:
        # Report before the cleanup prompt so the error stays visible above it.
        returncode = report_error(error, debug)
        finish_temporary_directory(temporary_directory, succeeded=False, cleanup=cleanup)
        raise ReportedError(returncode) from error
    finish_temporary_directory(temporary_directory, succeeded=True, cleanup=cleanup)


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    """Parse command-line arguments; omitted values are prompted for interactively."""
    parser = argparse.ArgumentParser(
        prog="nwstackify",
        description="Install a local Spack environment from spack.yaml.",
    )
    parser.add_argument(
        "--install-root",
        metavar="DIR",
        help="installation base directory (prompted if omitted)",
    )
    parser.add_argument(
        "-d",
        "--debug",
        action="store_true",
        help="show git output and print the exact Spack commands run",
    )
    parser.add_argument(
        "--spack-debug",
        action="store_true",
        help="also pass --debug to Spack",
    )
    parser.add_argument(
        "-c",
        "--confirm",
        action="store_true",
        help="ask whether to continue after concretization, before installing",
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="always delete the temporary directory when finished, even after a "
        "failure, without asking",
    )
    parser.add_argument(
        "--site",
        help="site name exported as $SITE (default: inferred from the hostname)",
    )
    parser.add_argument(
        "--compiler",
        help="compiler exported as $COMPILER, e.g. 'gcc' or 'oneapi@2025.3.1' "
        "(prompted if omitted; pass --compiler '' to leave it unset)",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the command-line application."""
    args = parse_args(argv)
    try:
        install_root = (
            resolve_install_root(args.install_root) if args.install_root else None
        )
        install(
            install_root=install_root,
            compiler=args.compiler,
            site=args.site or None,
            debug=args.debug,
            spack_debug=args.spack_debug,
            confirm=args.confirm,
            cleanup=args.cleanup,
        )
    except ReportedError as error:
        return error.returncode
    except (KeyboardInterrupt, *EXPECTED_ERRORS) as error:
        return report_error(error, args.debug)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
