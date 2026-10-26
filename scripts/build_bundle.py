"""Build the offline Windows install bundles:

    release/mongo-mcp-simple-windows-x64.zip   (64-bit Python)
    release/mongo-mcp-simple-windows-x86.zip   (32-bit Python)

    pip install uv && python scripts/build_bundle.py [x64|x86]

Each zip contains the mongo-mcp-simple wheel, every dependency wheel for its architecture and
CPython 3.10-3.14 (so installation works without internet), install.cmd, the agent install guide
and VS Code templates.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from packaging.tags import compatible_tags, cpython_tags
from packaging.utils import canonicalize_name, parse_wheel_filename

ROOT = Path(__file__).resolve().parent.parent
PYTHON_VERSIONS = ("3.10", "3.11", "3.12", "3.13", "3.14")
TOP = "mongo-mcp-simple"

# Packages that must be in every bundle (the Windows-only ones are the easy ones to lose).
REQUIRED = ("mongo-mcp-simple", "mcp", "pymongo", "dnspython", "pywin32", "winkerberos", "click", "uvicorn",
            "pydantic-core")

# arch -> (uv target triple, pip platform tag, extras to install)
ARCHES = {
    "x64": ("x86_64-pc-windows-msvc", "win_amd64", []),
    "x86": ("i686-pc-windows-msvc", "win32", []),
}


def _windows_requirements(arch: str, version: str, out: Path) -> Path:
    """Pin the full dependency tree as resolved ON WINDOWS for this architecture and Python.

    pip evaluates markers such as ``sys_platform == "win32"`` against the build machine, so a plain
    ``pip download --platform ...`` silently drops Windows-only packages (e.g. pywin32). uv resolves
    for the target, and ``--only-binary`` keeps it to versions that publish wheels for that target
    (e.g. the newest cryptography has no win32 wheel).
    """
    triple, _, extras = ARCHES[arch]
    req = out / f"requirements-{arch}-{version}.txt"
    cmd = ["uv", "pip", "compile", str(ROOT / "pyproject.toml"), "--python-platform", triple,
           "--python-version", version, "--only-binary", ":all:", "--no-header", "--no-annotate",
           "-q", "-o", str(req)]
    for extra in extras:
        cmd += ["--extra", extra]
    subprocess.run(cmd, check=True)
    return req


def _verify(wheelhouse: Path, req: Path, arch: str, version: str) -> None:
    """Every pinned package must have a wheel installable on this Python version + architecture.

    Never skip silently: report the exact package and the wheel tag that would be needed.
    """
    _, platform_tag, _ = ARCHES[arch]
    py = tuple(int(x) for x in version.split("."))
    target = set(cpython_tags(py, platforms=[platform_tag])) | set(compatible_tags(py, platforms=[platform_tag]))
    have: dict[str, set[str]] = {}
    for whl in wheelhouse.glob("*.whl"):
        name, ver, _, tags = parse_wheel_filename(whl.name)
        if tags & target:
            have.setdefault(canonicalize_name(name), set()).add(str(ver))
    pins = {}
    for line in req.read_text(encoding="utf-8").splitlines():
        line = line.split(";")[0].split("#")[0].strip()
        if "==" in line:
            name, ver = line.split("==", 1)
            pins[canonicalize_name(name.split("[")[0])] = ver.strip()
    missing = [f"{n}=={v}" for n, v in pins.items() if v not in have.get(n, set())]
    missing += [n for n in REQUIRED if canonicalize_name(n) not in have]
    if missing:
        tag = f"cp{py[0]}{py[1]}-cp{py[0]}{py[1]}-{platform_tag} (or py3-none-any / abi3)"
        raise SystemExit(f"[{arch} / Python {version}] no compatible wheel for: {', '.join(missing)} "
                         f"- needed tag: {tag}")


def _pip(*args: str) -> None:
    subprocess.run([sys.executable, "-m", "pip", *args, "--disable-pip-version-check", "-q"], check=True)


def build(arch: str) -> Path:
    _, platform_tag, _ = ARCHES[arch]
    out = ROOT / "release" / f"mongo-mcp-simple-windows-{arch}.zip"
    with tempfile.TemporaryDirectory() as tmp:
        wheelhouse = Path(tmp) / "wheelhouse"
        wheelhouse.mkdir()
        _pip("wheel", str(ROOT), "--no-deps", "-w", str(wheelhouse))
        for version in PYTHON_VERSIONS:
            req = _windows_requirements(arch, version, Path(tmp))
            _pip("download", "-r", str(req), "--no-deps", "--platform", platform_tag, "--python-version",
                 version, "--only-binary=:all:", "-d", str(wheelhouse))
        for version in PYTHON_VERSIONS:
            _verify(wheelhouse, Path(tmp) / f"requirements-{arch}-{version}.txt", arch, version)

        out.parent.mkdir(exist_ok=True)
        if out.exists():
            out.unlink()
        files: list[tuple[Path, str]] = [
            (ROOT / "bundle" / "KURULUM_REHBERI.md", "KURULUM_REHBERI.md"),
            (ROOT / "bundle" / "install.cmd", "install.cmd"),
            (ROOT / "README.md", "README.md"),
            (ROOT / "LICENSE", "LICENSE"),
        ]
        files += [(p, f"templates/{p.name}") for p in sorted((ROOT / "bundle" / "templates").iterdir())]
        files += [(p, f"wheelhouse/{p.name}") for p in sorted(wheelhouse.iterdir())]
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(f"{TOP}/bundle_arch.txt", arch)  # read by install.cmd
            for src, arc in files:
                zf.write(src, f"{TOP}/{arc}")
    return out


if __name__ == "__main__":
    for name in sys.argv[1:] or list(ARCHES):
        if name not in ARCHES:
            sys.exit(f"unknown architecture {name!r}; use one of {', '.join(ARCHES)}")
        path = build(name)
        print(f"{path} ({path.stat().st_size / 1_048_576:.1f} MB)")
