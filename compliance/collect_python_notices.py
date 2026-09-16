"""Collect installed package notices and Debian notices for copied runtime files.

Runs in the Python builder. It uses installed metadata and dpkg ownership, never
substitutes a project's MIT license for a dependency's license.
"""

import argparse
import hashlib
import importlib.metadata as metadata
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sysconfig


def notice_name(path):
    return bool(re.search(r"license|licence|notice|copying|copyright", Path(path).name, re.I))


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def collect(output, runtime_libs, supplemental):
    output.mkdir(parents=True, exist_ok=True)
    packages = []
    for dist in sorted(metadata.distributions(), key=lambda d: d.metadata["Name"].lower()):
        name, version = dist.metadata["Name"], dist.version
        notices = []
        upstream = supplemental / f"{name.lower()}-{version}"
        if upstream.exists():
            provenance = json.loads((upstream / "provenance.json").read_text())
            if (provenance["component"], provenance["version"]) != (name.lower(), version):
                raise RuntimeError(f"Supplemental notice version mismatch for {name}")
            for item in provenance["files"]:
                source = (upstream / item["path"]).resolve()
                if not source.is_relative_to(upstream.resolve()):
                    raise RuntimeError("Supplemental notice path escapes its directory")
                if hashlib.sha256(source.read_bytes()).hexdigest() != item["sha256"]:
                    raise RuntimeError(f"Supplemental notice checksum mismatch: {source}")
            target = output / "upstream" / upstream.name
            shutil.copytree(upstream, target)
            notices.extend(str(p.relative_to(output)) for p in target.rglob("*") if p.is_file())
        for item in dist.files or []:
            if not notice_name(str(item)):
                continue
            source = Path(dist.locate_file(item)).resolve()
            if not source.is_file():
                continue
            # Preserve the full path to avoid collisions between bundled notices.
            relative = Path(*source.parts[1:])
            target = output / "python" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            notices.append(str(target.relative_to(output)))
        packages.append({
            "name": name, "version": version,
            "license_expression": dist.metadata.get("License-Expression"),
            "license_metadata": dist.metadata.get("License"),
            "license_classifiers": [x for x in dist.metadata.get_all("Classifier", []) if x.startswith("License ::")],
            "project_urls": dist.metadata.get_all("Project-URL", []),
            "notices": sorted(set(notices)),
            "review": "notice files collected" if notices else "check metadata or upstream for license text",
        })
    write_json(output / "python-packages.json", packages)
    missing = [p["name"] for p in packages if not p["notices"]]
    if missing:
        raise RuntimeError(f"License files missing for installed packages: {missing}. Add verified upstream notices.")
    python_license = Path(sysconfig.get_path("stdlib")) / "LICENSE.txt"
    if not python_license.is_file():
        raise RuntimeError("CPython LICENSE.txt is missing from the builder")
    shutil.copyfile(python_license, output / "CPython-LICENSE.txt")
    write_json(output / "cpython-source.json", {
        "version": os.environ.get("PYTHON_VERSION"),
        "sha256": os.environ.get("PYTHON_SHA256"),
        "url": f"https://www.python.org/ftp/python/{os.environ['PYTHON_VERSION']}/Python-{os.environ['PYTHON_VERSION']}.tar.xz",
    })

    # dpkg-query accepts paths as patterns. Query the original library names,
    # including their resolved targets, to handle Debian's merged /usr layout.
    owners = {"ca-certificates"}
    copied = []
    for library in sorted(runtime_libs.iterdir()):
        if not library.is_file():
            continue
        original = Path("/usr/lib/x86_64-linux-gnu") / library.name
        result = subprocess.run(["dpkg-query", "-S", str(original)], capture_output=True, text=True, timeout=30)
        if result.returncode:
            result = subprocess.run(["dpkg-query", "-S", "*/" + library.name], capture_output=True, text=True, timeout=30, check=True)
        matches = [line.rsplit(": ", 1)[0] for line in result.stdout.splitlines() if ": " in line]
        if not matches:
            raise RuntimeError(f"No Debian package owns {library}")
        owners.update(matches)
        copied.append({"file": library.name, "sha256": hashlib.sha256(library.read_bytes()).hexdigest(), "owners": matches})
    os_packages = []
    for owner in sorted(owners):
        details = subprocess.check_output([
            "dpkg-query", "-W", "-f=${binary:Package}\t${Version}\t${source:Package}\t${source:Version}\n", owner
        ], text=True, timeout=30).strip().split("\t")
        package = details[0].split(":")[0]
        copyright_file = Path("/usr/share/doc") / package / "copyright"
        if not copyright_file.is_file():
            raise RuntimeError(f"Missing Debian copyright file for {owner}")
        target = output / "copied-debian" / package / "copyright"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(copyright_file, target)
        os_packages.append(dict(zip(["name", "version", "source", "source_version"], details)))
    shutil.copytree("/usr/share/common-licenses", output / "common-licenses", dirs_exist_ok=True)
    write_json(output / "copied-debian-packages.json", os_packages)
    write_json(output / "copied-library-files.json", copied)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("/licenses"))
    parser.add_argument("--runtime-libs", type=Path, default=Path("/opt/runtime-libs"))
    parser.add_argument("--supplemental", type=Path, default=Path("/tmp/upstream-notices"))
    args = parser.parse_args()
    collect(args.output, args.runtime_libs, args.supplemental)
