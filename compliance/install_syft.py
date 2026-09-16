"""Install a pinned Syft binary after verifying its published SHA-256 checksum."""

import argparse
import hashlib
import io
from pathlib import Path
import platform
import tarfile
import urllib.request
import zipfile

VERSION = "1.42.3"


def fetch(url):
    with urllib.request.urlopen(url, timeout=120) as response:
        return response.read()


def install(destination):
    os_name = {"Windows": "windows", "Linux": "linux"}[platform.system()]
    if platform.machine().lower() not in {"amd64", "x86_64"}:
        raise RuntimeError("This installer currently supports amd64 hosts only")
    suffix = "zip" if os_name == "windows" else "tar.gz"
    archive = f"syft_{VERSION}_{os_name}_amd64.{suffix}"
    base = f"https://github.com/anchore/syft/releases/download/v{VERSION}/"
    checksums = fetch(base + f"syft_{VERSION}_checksums.txt").decode()
    expected = next(line.split()[0] for line in checksums.splitlines() if line.split()[-1] == archive)
    data = fetch(base + archive)
    if hashlib.sha256(data).hexdigest() != expected:
        raise RuntimeError("Syft release checksum mismatch")
    binary = "syft.exe" if os_name == "windows" else "syft"
    if suffix == "zip":
        with zipfile.ZipFile(io.BytesIO(data)) as bundle:
            payload = bundle.read(binary)
    else:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as bundle:
            payload = bundle.extractfile(binary).read()
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / binary
    target.write_bytes(payload)
    target.chmod(0o755)
    print(target.resolve())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(".oss-tools"))
    install(parser.parse_args().output)
