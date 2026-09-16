"""Build or inspect final images and write an OSS evidence pack without pushing.

Requires Docker and Syft. Image config IDs and registry manifest digests are
recorded separately.
"""

import argparse
import csv
import hashlib
import json
from pathlib import Path, PurePosixPath
import posixpath
import re
import subprocess
import tarfile
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
IMAGES = {"mendix-deploy-controller": "Controller", "mendix-admin-ui": "Admin UI", "mendix-base": "Mendix Base Image"}


def run(args, timeout=900):
    return subprocess.check_output([str(x) for x in args], cwd=ROOT, text=True, encoding="utf-8", timeout=timeout).strip()


def save_json(path, data):
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def notice_path(name):
    return (name in {"opt/java/openjdk/release", "etc/os-release", "usr/lib/os-release"}
            or name.startswith(("licenses/", "usr/share/common-licenses/", "opt/java/openjdk/legal/"))
            or bool(re.search(r"license|licence|notice|copying|copyright", PurePosixPath(name).name, re.I)))


def extract_notices(archive, destination):
    """Copy notice contents only. Never extract archive paths or symlinks."""
    records = []
    with tarfile.open(archive) as bundle:
        members = {m.name.removeprefix("./").lstrip("/"): m for m in bundle.getmembers()}
        for name, member in sorted(members.items()):
            if not notice_path(name) or member.isdir():
                continue
            parts = PurePosixPath(name).parts
            if ".." in parts or any(":" in p or "\\" in p for p in parts):
                raise ValueError(f"Unsafe notice path: {name}")
            source = member
            visited = set()
            while source.issym() or source.islnk():
                if source.name in visited:
                    raise ValueError(f"Cyclic notice link: {name}")
                visited.add(source.name)
                target = source.linkname
                if source.issym() and not target.startswith("/"):
                    target = posixpath.join(posixpath.dirname(source.name), target)
                target = posixpath.normpath(target).lstrip("/")
                if target not in members:
                    raise ValueError(f"Broken notice link: {name} -> {target}")
                source = members[target]
            if not source.isfile():
                continue
            target = destination.joinpath(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            data = bundle.extractfile(source).read()
            target.write_bytes(data)
            records.append({"path": name, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)})
    return records


def inventory(sbom, destination):
    rows = []
    for item in sbom.get("artifacts", []):
        licenses = sorted({x.get("spdxExpression") or x.get("value") or "UNKNOWN" for x in item.get("licenses", [])})
        md = item.get("metadata", {})
        rows.append({"name": item["name"], "version": item.get("version", ""), "type": item.get("type", ""),
                     "licenses": " | ".join(licenses) or "UNKNOWN", "purl": item.get("purl", ""),
                     "source_package": md.get("source", ""), "source_version": md.get("sourceVersion", ""),
                     "evidence": item.get("metadataType", "")})
    copied_path = destination / "notices/licenses/copied-debian-packages.json"
    if copied_path.exists():
        for package in json.loads(copied_path.read_text(encoding="utf-8")):
            name = package["name"].split(":")[0]
            copyright_path = destination / "notices/licenses/copied-debian" / name / "copyright"
            copyright_text = copyright_path.read_text(encoding="utf-8", errors="replace")
            licenses = sorted(set(re.findall(r"^License:\s*(.+)$", copyright_text, re.M)))
            rows.append({"name": name, "version": package["version"], "type": "deb-copied",
                         "licenses": " | ".join(licenses) or "See copyright text", "purl": "",
                         "source_package": package["source"], "source_version": package["source_version"],
                         "evidence": str(copyright_path.relative_to(destination)).replace("\\", "/")})
    rows.sort(key=lambda r: (r["type"], r["name"], r["version"]))
    with (destination / "inventory.csv").open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=["name", "version", "type", "licenses", "purl", "source_package", "source_version", "evidence"])
        writer.writeheader()
        writer.writerows(rows)
    return rows


def inspect_image(name, reference, output, syft):
    destination = output / name
    destination.mkdir(parents=True, exist_ok=False)
    details = json.loads(run(["docker", "image", "inspect", reference], timeout=60))[0]
    image_id = details["Id"]
    identity = {"requested_reference": reference, "docker_image_id": image_id,
                "registry_manifest_references": details.get("RepoDigests", []),
                "os": details["Os"], "architecture": details["Architecture"]}
    if (identity["os"], identity["architecture"]) != ("linux", "amd64"):
        raise ValueError(f"Unexpected release platform: {identity}")
    save_json(destination / "image-identity.json", identity)
    image_archive = destination / "image.tar"
    rootfs = destination / "rootfs.tar"
    print(f"{name}: exporting exact image {image_id}", flush=True)
    run(["docker", "image", "save", "-o", image_archive, image_id])
    cid = run(["docker", "create", image_id], timeout=60)
    try:
        run(["docker", "export", "-o", rootfs, cid])
    finally:
        run(["docker", "rm", cid], timeout=60)
    notices = extract_notices(rootfs, destination / "notices")
    save_json(destination / "notice-index.json", notices)
    project_license = destination / "notices/licenses/project/LICENSE.txt"
    if not project_license.is_file() or project_license.read_bytes() != (ROOT / "LICENSE.txt").read_bytes():
        raise ValueError(f"Project license missing or stale in {name}")
    if name != "mendix-base":
        for required in ["python-packages.json", "CPython-LICENSE.txt", "copied-debian-packages.json"]:
            if not (destination / "notices/licenses" / required).is_file():
                raise ValueError(f"Missing {required} in {name}")
    print(f"{name}: scanning packages and licenses with Syft", flush=True)
    run([syft, "scan", "docker-archive:" + str(image_archive), "--config", ROOT / "compliance/syft.yaml",
         "-o", "syft-json=" + str(destination / "sbom.syft.json"),
         "-o", "spdx-json=" + str(destination / "sbom.spdx.json")], timeout=900)
    sbom = json.loads((destination / "sbom.syft.json").read_text(encoding="utf-8"))
    identity["image_config_digest"] = sbom["source"]["metadata"]["imageID"]
    identity["archive_manifest_digest"] = sbom["source"]["metadata"]["manifestDigest"]
    save_json(destination / "image-identity.json", identity)
    rows = inventory(sbom, destination)
    unknown = [r for r in rows if r["licenses"] == "UNKNOWN"]
    copyleft = [r for r in rows if re.search(r"(?<![A-Z])(?:A?GPL|LGPL|MPL|EPL|CDDL)", r["licenses"], re.I)]
    review = {"package_count": len(rows),
              "notice_file_count": len(notices), "unidentified_licenses": unknown, "copyleft_candidates": copyleft,
              "manual_checks": ["Bundled JavaScript and native library inventory completeness",
                                "Exact corresponding source, patches and build instructions for applicable copyleft components",
                                "Source delivery and modification/replacement rights for the actual distribution"]}
    save_json(destination / "review-items.json", review)
    # These are intermediate copies created only by this invocation.
    image_archive.unlink()
    rootfs.unlink()
    return {"image": name, **identity, "packages": len(rows), "notice_files": len(notices), "unknown_licenses": len(unknown)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--syft", default="syft")
    parser.add_argument("--build", action="store_true")
    parser.add_argument("--require-clean", action="store_true",
                        help="Fail unless tracked and untracked files match HEAD")
    parser.add_argument("--image", action="append", help="name=local-reference or name=registry-reference@sha256:digest")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    images = dict(x.split("=", 1) for x in args.image) if args.image else {name: name + ":oss-review" for name in IMAGES}
    if set(images) - set(IMAGES):
        parser.error("Unknown image name")
    working_tree_dirty = bool(run(["git", "status", "--porcelain", "--untracked-files=normal"], 30))
    if args.require_clean and working_tree_dirty:
        raise RuntimeError("OSS evidence requires a clean checkout")
    syft = str(Path(args.syft).resolve()) if Path(args.syft).is_file() else args.syft
    tool_version = run([syft, "version"], timeout=30)
    input_paths = [ROOT / "LICENSE.txt", ROOT / ".dockerignore"] + list((ROOT / "compliance").glob("*.py")) + [ROOT / "compliance/IMAGE-NOTICES.txt", ROOT / "compliance/syft.yaml"]
    input_paths.extend(p for p in (ROOT / "compliance/upstream").rglob("*") if p.is_file())
    for name in images:
        context = ROOT / IMAGES[name]
        input_paths.extend(p for p in context.rglob("*") if p.is_file() and (p.name in {"Dockerfile", "requirements.txt", "entrypoint.sh"} or "app" in p.relative_to(context).parts) and "__pycache__" not in p.parts)
    inputs = {p.relative_to(ROOT).as_posix(): sha256(p) for p in sorted(set(input_paths))}
    save_json(output / "build-inputs.json", inputs)
    manifest = {"created_utc": datetime.now(timezone.utc).isoformat(), "git_commit": run(["git", "rev-parse", "HEAD"], 30),
                "working_tree_dirty": working_tree_dirty,
                "syft_version": tool_version, "images": []}
    for name, reference in images.items():
        if args.build:
            run(["docker", "build", "--platform", "linux/amd64", "--provenance=false", "--build-context", "project=" + str(ROOT),
                 "-t", reference, ROOT / IMAGES[name]], timeout=1800)
        manifest["images"].append(inspect_image(name, reference, output, syft))
        save_json(output / "manifest.json", manifest)
    changed = [path for path, digest in inputs.items() if sha256(ROOT / path) != digest]
    if changed:
        raise RuntimeError(f"Build inputs changed during evidence capture: {changed}")
    hashes = {p.relative_to(output).as_posix(): sha256(p) for p in sorted(output.rglob("*")) if p.is_file() and p.name != "SHA256SUMS.json"}
    save_json(output / "SHA256SUMS.json", hashes)
    print(f"Evidence written to {output}.", flush=True)


if __name__ == "__main__":
    main()
