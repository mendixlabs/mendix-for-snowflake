"""Resolve exact upstream source references for an existing image evidence pack.

No package code is executed. References support review; they do not constitute
an offer to recipients or a complete corresponding-source delivery by themselves.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import re
import urllib.parse
import urllib.request


def get_json(url):
    request = urllib.request.Request(url, headers={"User-Agent": "mendix-oss-inventory"})
    with urllib.request.urlopen(request, timeout=45) as response:
        return json.load(response)


def add_to_checksums(evidence, path):
    """Add a generated evidence file to the pack's checksum manifest."""
    sums_path = evidence / "SHA256SUMS.json"
    if not sums_path.is_file():
        return
    sums = json.loads(sums_path.read_text(encoding="utf-8"))
    sums[path.relative_to(evidence).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    sums_path.write_text(json.dumps(sums, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def resolve_source(item):
    kind, name, version = item
    result = {"ecosystem": kind, "name": name, "version": version, "files": []}
    try:
        if kind == "pypi":
            url = f"https://pypi.org/pypi/{urllib.parse.quote(name)}/{urllib.parse.quote(version)}/json"
            data = get_json(url)
            result["metadata_url"] = url
            result["project_urls"] = data["info"].get("project_urls") or {}
            result["files"] = [{"url": f["url"], "filename": f["filename"], "sha256": f["digests"]["sha256"]}
                               for f in data["urls"] if f["packagetype"] == "sdist"]
            if name == "psycopg-binary" and not result["files"]:
                commit = get_json(f"https://api.github.com/repos/psycopg/psycopg/commits/{version}")["sha"]
                result["files"] = [{"url": f"https://codeload.github.com/psycopg/psycopg/tar.gz/{commit}", "filename": f"psycopg-{commit}.tar.gz"}]
                result["review_note"] = "Includes adapter code and build recipes. Match each bundled native library to its corresponding source separately."
        elif kind == "debian":
            url = f"https://snapshot.debian.org/mr/package/{urllib.parse.quote(name)}/{urllib.parse.quote(version, safe='')}/srcfiles"
            data = get_json(url)
            result["metadata_url"] = url
            result["files"] = [{"url": "https://snapshot.debian.org/file/" + f["hash"], "sha1": f["hash"]} for f in data["result"]]
        elif kind == "ubuntu":
            query = urllib.parse.urlencode({"ws.op": "getPublishedSources", "source_name": name, "version": version, "exact_match": "true"})
            url = "https://api.launchpad.net/1.0/ubuntu/+archive/primary?" + query
            data = get_json(url)
            result["metadata_url"] = url
            exact = [x for x in data.get("entries", []) if x.get("source_package_name") == name and x.get("source_package_version") == version]
            if exact:
                files = get_json(exact[0]["self_link"] + "?ws.op=sourceFileUrls")
                result["files"] = [{"url": f, "filename": f.rsplit("/", 1)[-1]} for f in files]
        elif kind == "temurin":
            tag = "jdk-" + version
            url = "https://api.github.com/repos/adoptium/temurin21-binaries/releases/tags/" + urllib.parse.quote(tag, safe="")
            data = get_json(url)
            result["metadata_url"] = url
            result["files"] = [{"url": f["browser_download_url"], "filename": f["name"], "digest": f.get("digest")}
                               for f in data["assets"] if "sources" in f["name"] and f["name"].endswith(".tar.gz")]
            result["build_instructions"] = "https://github.com/adoptium/temurin-build"
        result["status"] = "source references resolved" if result["files"] else "manual source lookup required"
    except Exception as error:
        result["status"] = "source lookup failed"
        result["error"] = str(error)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--reuse", type=Path, help="Reuse successful lookups from a previous source-references.json")
    args = parser.parse_args()
    sources = set()
    fixed_sources = []
    for path in args.evidence.glob("*/sbom.syft.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        for item in data["artifacts"]:
            md = item.get("metadata", {})
            if item["type"] == "python":
                sources.add(("pypi", item["name"], item["version"]))
            elif item.get("metadataType") == "dpkg-db-entry":
                kind = "ubuntu" if "ubuntu" in item.get("purl", "") else "debian"
                source = md.get("source") or item["name"]
                version = md.get("sourceVersion") or item["version"]
                sources.add((kind, source, version))
        copied = path.parent / "notices/licenses/copied-debian-packages.json"
        if copied.exists():
            for item in json.loads(copied.read_text(encoding="utf-8")):
                sources.add(("debian", item["source"], item["source_version"]))
        release = path.parent / "notices/opt/java/openjdk/release"
        if release.exists():
            match = re.search(r'IMPLEMENTOR_VERSION="Temurin-([^\"]+)"', release.read_text())
            if match:
                sources.add(("temurin", "OpenJDK", match[1]))
        cpython = path.parent / "notices/licenses/cpython-source.json"
        if cpython.exists():
            item = json.loads(cpython.read_text())
            record = {"ecosystem": "cpython", "name": "CPython", "version": item["version"], "status": "source reference from pinned builder",
                      "files": [{"url": item["url"], "sha256": item["sha256"]}]}
            if record not in fixed_sources:
                fixed_sources.append(record)
    cache = {}
    if args.reuse:
        cache = {(x["ecosystem"], x["name"], x["version"]): x for x in json.loads(args.reuse.read_text()) if x["files"]}
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda item: cache.get(item) or resolve_source(item), sorted(sources))) + fixed_sources
    references_path = args.evidence / "source-references.json"
    references_path.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    add_to_checksums(args.evidence, references_path)
    unresolved = [x for x in results if not x["files"]]
    print(f"Source references: {len(results) - len(unresolved)} resolved, {len(unresolved)} need review.")
    for item in unresolved:
        print(item["ecosystem"], item["name"], item["version"], item["status"])


if __name__ == "__main__":
    main()
