"""Deterministic npm tgz from a fixed Git commit and package.files allowlist."""
import gzip
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile


def git(*args):
    return subprocess.check_output(["git", *args])


def build(commit, output):
    commit = git("rev-parse", f"{commit}^{{commit}}").decode().strip()
    manifest = json.loads(git("show", f"{commit}:package.json"))
    prefixes = manifest["files"]
    names = git("ls-tree", "-r", "--name-only", commit).decode().splitlines()
    selected = [name for name in names if name == "package.json" or
                any(name == item or name.startswith(item + "/") for item in prefixes)]
    for item in prefixes:
        if not any(name == item or name.startswith(item + "/") for name in selected):
            raise ValueError(f"missing_pack_input:{item}")
    provenance = {"package": manifest["name"], "version": manifest["version"],
                  "tag": "dsh/v0.1.0-alpha.1", "commit": commit,
                  "sourceFiles": selected, "build": "git-allowlist-gzip-mtime-zero"}
    entries = {name: git("show", f"{commit}:{name}") for name in selected}
    entries["dsh-release.json"] = (json.dumps(provenance, indent=2) + "\n").encode()
    destination = Path(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT) as archive:
                for name, content in sorted(entries.items()):
                    member = tarfile.TarInfo("package/" + name)
                    member.size = len(content)
                    member.mode = 0o644
                    member.mtime = 0
                    archive.addfile(member, io.BytesIO(content))
    print(json.dumps({"path": str(destination.resolve()), "commit": commit,
                      "bytes": destination.stat().st_size,
                      "sha256": hashlib.sha256(destination.read_bytes()).hexdigest()}))


if __name__ == "__main__":
    build(sys.argv[1], sys.argv[2])
