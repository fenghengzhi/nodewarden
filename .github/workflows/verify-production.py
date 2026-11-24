#!/usr/bin/env python3
"""Verify a committed NodeWarden production tree before any npm scripts run.

Uses public Git reads only. Does not patch the checkout or call any REST API.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

FORK = "https://github.com/fenghengzhi/nodewarden.git"
UPSTREAM = "https://github.com/shuaiplus/nodewarden.git"
BASE = "29c2de852411f3aa66de0c8a2ea16e7f87fa932c"
BOOTSTRAP = "d928f8945a9be575e7c2f899d827f20961653dc1"
OVERLAY = "da2bf6f062fe00c66c3ab601815a6ab0b52748a4"
SHA = re.compile(r"[0-9a-f]{40}")
SEMVER = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def git(*args, data=None, index=None):
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    if index is not None:
        env["GIT_INDEX_FILE"] = str(index)
    result = subprocess.run(["git", *args], input=data, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    require(result.returncode == 0, "Git verification failed; refusing deployment.")
    return result.stdout


def remote_tag(repository, ref, annotated):
    raw = git("ls-remote", "--exit-code", repository, ref, ref + "^{}")
    records = {}
    for line in raw.decode("ascii").splitlines():
        fields = line.split("\t")
        require(len(fields) == 2 and SHA.fullmatch(fields[0]), "Malformed remote tag response.")
        sha, name = fields
        require(name in (ref, ref + "^{}") and name not in records, "Unexpected remote tag response.")
        records[name] = sha
    require(ref in records, "Required release/approval tag is missing.")
    if annotated:
        return records.get(ref + "^{}", records[ref])
    require(ref + "^{}" not in records, "Approval tag must point directly to the production commit.")
    return records[ref]


def expected_tree(parent, overlay):
    # Only the reviewed config/password delta is reconstructed, in a private index.
    # The actual worktree and HEAD remain untouched throughout verification.
    patch = git("diff", "--binary", BASE, overlay, "--", "wrangler.toml", "webapp")
    require(patch, "The reviewed configuration delta is empty.")
    with tempfile.TemporaryDirectory(prefix="nodewarden-verify-") as directory:
        index = Path(directory) / "index"
        git("read-tree", parent, index=index)
        git("apply", "--cached", "--check", "-", data=patch, index=index)
        git("apply", "--cached", "-", data=patch, index=index)
        return git("write-tree", index=index).decode().strip()


def verify_production(fork=FORK, upstream=UPSTREAM):
    commit = os.environ.get("WORKERS_CI_COMMIT_SHA", "")
    require(SHA.fullmatch(commit), "WORKERS_CI_COMMIT_SHA must contain the full production commit.")
    require(os.environ.get("WORKERS_CI_BRANCH") == "production", "Only production may build.")
    require(git("rev-parse", "HEAD").decode().strip() == commit, "Cloudflare SHA differs from checkout HEAD.")
    require(not git("status", "--porcelain", "--untracked-files=no").strip(), "Tracked checkout must be pristine.")
    require(Path.cwd().resolve() == Path(git("rev-parse", "--show-toplevel").decode().strip()).resolve(),
            "Run verification from the repository root.")
    if git("rev-parse", "--is-shallow-repository").strip() == b"true":
        # Fetch this exact commit, never the moving production branch.
        git("fetch", "--quiet", "--no-tags", "--depth=2", fork, commit)
    header = git("cat-file", "-p", commit).split(b"\n\n", 1)[0]
    parents = [line[7:].decode("ascii") for line in header.splitlines() if line.startswith(b"parent ")]
    require(len(parents) == 1 and SHA.fullmatch(parents[0]), "Production commit must have exactly one official parent.")
    parent = parents[0]
    version = json.loads(git("show", parent + ":package.json"))["version"]
    require(isinstance(version, str) and SEMVER.fullmatch(version), "Official parent version must be stable semver.")
    bootstrap = commit == BOOTSTRAP
    if bootstrap:
        require(parent == BASE, "Bootstrap parent differs from the deployed baseline.")
    else:
        require(remote_tag(upstream, "refs/tags/v" + version, annotated=True) == parent,
                "The only parent is not the official release tag commit.")
        require(remote_tag(fork, "refs/tags/nodewarden-deploy/v" + version, annotated=False) == commit,
                "Production SHA lacks its exact approved deployment tag.")
    git("fetch", "--quiet", "--no-tags", "--depth=1", fork, BASE, OVERLAY)
    require(git("show", parent + ":wrangler.toml") == git("show", BASE + ":wrangler.toml"),
            "Upstream Wrangler policy changed; review bindings, migrations, cron and build settings.")
    overlay = BOOTSTRAP if bootstrap else OVERLAY
    wanted = expected_tree(parent, overlay)
    require(git("rev-parse", commit + "^{tree}").decode().strip() == wanted,
            "Production tree differs from the official tree plus the reviewed configuration/password delta.")
    require(not git("status", "--porcelain", "--untracked-files=no").strip(), "Verification must not modify tracked files.")
    print("Verified production commit: " + commit, flush=True)
    print("Only parent (official source): " + parent, flush=True)
    print("Bootstrap: dry-run deployment only." if bootstrap else "Approved deployment tag and exact customized tree verified.", flush=True)
    return {"commit": commit, "parent": parent, "version": version, "bootstrap": bootstrap}


if __name__ == "__main__":
    try:
        verify_production()
    except Exception as error:
        # Never echo Git/network output or environment values on failures.
        message = str(error) if isinstance(error, RuntimeError) else "Production verification failed."
        print(message, file=sys.stderr)
        raise SystemExit(1)
