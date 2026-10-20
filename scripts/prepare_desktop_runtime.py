"""Build-time only: verify and install a pinned CPython with the local Pico package."""

import argparse
import hashlib
import json
import platform
import shutil
import subprocess
import tarfile
from pathlib import Path


def prepare(root, archive=None):
    desktop = root / "desktop"
    spec = json.loads((desktop / "python-runtime.json").read_text())
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise ValueError("This runtime currently targets macOS arm64")
    runtime = desktop / "runtime"
    runtime.mkdir(exist_ok=True)
    archive = Path(archive) if archive else runtime / "python.tar.gz"
    if not archive.exists():
        subprocess.run(["curl", "--fail", "--location", "--retry", "2", "--proto", "=https",
                        "--tlsv1.2", "--output", str(archive), spec["url"]], check=True)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != spec["sha256"]:
        raise ValueError("CPython checksum mismatch; remove the archive and download again")
    backend = runtime / "backend"
    if backend.exists():
        shutil.rmtree(backend)
    backend.mkdir()
    with tarfile.open(archive) as bundle:
        bundle.extractall(backend, filter="data")
    python = backend / "python/bin/python3"
    site = backend / "python/lib/python3.11/site-packages"
    shutil.copytree(root / "pico", site / "pico", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    certificates = site / "pip/_vendor/certifi/cacert.pem"
    if not certificates.exists():
        raise ValueError("Pinned Python must include its certificate bundle")
    shutil.copy2(certificates, backend / "cacert.pem")
    (backend / "runtime.json").write_text(json.dumps(spec, indent=2) + "\n")
    subprocess.run([str(python), "-I", "-c", "import pico,sqlite3,ssl; print('Pico runtime ready')"],
                   cwd=backend, check=True)
    return backend


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, help="Previously downloaded pinned archive")
    args = parser.parse_args()
    prepare(Path(__file__).resolve().parent.parent, args.archive)


if __name__ == "__main__":
    main()
