#!/usr/bin/env python3
"""
Generate and check lock information for devcontainer features.
"""
from __future__ import annotations

import datetime as _dt
import io
import json
import os
import re
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Dict, List, Optional
from urllib.request import urlopen

SCRIPT_DIR = Path(__file__).resolve().parent
WORKSPACE_ROOT = SCRIPT_DIR.parent.parent
DEVCONTAINER_JSON = WORKSPACE_ROOT / ".devcontainer" / "devcontainer.json"
LOCK_PATH_DEFAULT = WORKSPACE_ROOT / ".devcontainer" / "features.lock"
CRANE_BIN = os.environ.get("CRANE", "crane")


class FeatureInfo(Dict[str, object]):
    """Typed dict alias for clarity."""


_FETCH_CACHE: Dict[str, str] = {}


def _fetch_url_text(url: str) -> str:
    cached = _FETCH_CACHE.get(url)
    if cached is not None:
        return cached
    with urlopen(url, timeout=10) as response:  # noqa: S310 (trusted URL set)
        data = response.read()
    text = data.decode("utf-8")
    _FETCH_CACHE[url] = text
    return text


def _git_ls_remote_tags(repo: str) -> List[str]:
    try:
        output = subprocess.run(
            ["git", "ls-remote", "--tags", repo],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except subprocess.CalledProcessError as exc:  # pragma: no cover - relay details to caller
        stderr = exc.stderr or ""
        raise RuntimeError(f"failed to query tags from {repo}: {stderr.strip()}") from exc
    refs: List[str] = []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        ref = parts[1]
        if ref.endswith("^{}"):
            ref = ref[:-3]
        refs.append(ref)
    return refs


def _resolve_latest_from_git_tags(
    repo: str,
    *,
    prefix: str,
    separator: str = ".",
    parts: int = 3,
) -> str:
    full_prefix = f"refs/{prefix}"
    pattern = re.compile(
        rf"^{re.escape(full_prefix)}(?P<version>\d+(?:{re.escape(separator)}\d+){{{parts - 1}}})$"
    )
    candidates: List[str] = []
    for ref in _git_ls_remote_tags(repo):
        match = pattern.match(ref)
        if not match:
            continue
        candidates.append(match.group("version"))
    if not candidates:
        raise RuntimeError(f"no version tags matching {full_prefix} found for {repo}")
    candidates.sort(key=lambda version: tuple(int(v) for v in version.split(separator)))
    return candidates[-1]


def _resolver_go(options: Dict[str, object]) -> Dict[str, str]:
    version = str(options.get("version", "latest"))
    if version in {"", "latest"}:
        try:
            resolved = _fetch_url_text("https://go.dev/VERSION?m=text").splitlines()[0].strip()
        except Exception as exc:  # pylint: disable=broad-exception-caught
            raise RuntimeError(f"failed to resolve Go latest version: {exc}") from exc
        return {"go": resolved}
    return {"go": version}


_NODE_DIST_URL = "https://nodejs.org/dist/index.json"


def _resolve_node_latest(kind: str) -> str:
    raw = _fetch_url_text(_NODE_DIST_URL)
    entries = json.loads(raw)
    if not isinstance(entries, list):
        raise RuntimeError("unexpected Node index.json format")
    if kind == "latest":
        for entry in entries:
            version = entry.get("version")
            if version:
                return version
    if kind == "lts":
        for entry in entries:
            if entry.get("lts"):
                version = entry.get("version")
                if version:
                    return version
    raise RuntimeError(f"unable to find Node version for kind={kind}")


def _resolver_node(options: Dict[str, object]) -> Dict[str, str]:
    version = str(options.get("version", "latest")).lower()
    if version in {"", "latest", "node", "current"}:
        resolved = _resolve_node_latest("latest")
    elif version in {"lts", "lts/*"}:
        resolved = _resolve_node_latest("lts")
    else:
        resolved = version
    result = {"node": resolved}
    pnpm_version = options.get("pnpmVersion")
    if pnpm_version:
        pnpm_version = str(pnpm_version)
        if pnpm_version.lower() == "latest":
            try:
                raw = _fetch_url_text("https://registry.npmjs.org/pnpm/latest")
                pnpm_info = json.loads(raw)
                resolved_pnpm = pnpm_info.get("version")
            except Exception as exc:  # pylint: disable=broad-exception-caught
                raise RuntimeError(f"failed to resolve pnpm version: {exc}") from exc
            if resolved_pnpm:
                result["pnpm"] = resolved_pnpm
        else:
            result["pnpm"] = pnpm_version
    return result


def _resolver_python(options: Dict[str, object]) -> Dict[str, str]:
    version = str(options.get("version", "latest")).lower()
    if version in {"", "latest", "current"}:
        resolved = _resolve_latest_from_git_tags(
            "https://github.com/python/cpython",
            prefix="tags/v",
            separator=".",
            parts=3,
        )
    else:
        resolved = version
    return {"python": resolved}


def _resolver_rust(options: Dict[str, object]) -> Dict[str, str]:
    version = str(options.get("version", "latest")).lower()
    if version in {"", "latest", "stable", "lts"}:
        resolved = _resolve_latest_from_git_tags(
            "https://github.com/rust-lang/rust",
            prefix="tags/",
            separator=".",
            parts=3,
        )
    else:
        resolved = version
    return {"rust": resolved}


FEATURE_CONFIGS: Dict[str, Dict[str, object]] = {
    "ghcr.io/devcontainers/features/go": {
        "commands": [("go", ["go", "version"])],
        "resolver": _resolver_go,
    },
    "ghcr.io/devcontainers/features/node": {
        "commands": [
            ("node", ["node", "--version"]),
            ("npm", ["npm", "--version"]),
            ("pnpm", ["pnpm", "--version"]),
        ],
        "resolver": _resolver_node,
    },
    "ghcr.io/devcontainers/features/python": {
        "commands": [
            ("python3", ["python3", "--version"]),
            ("pipx", ["pipx", "--version"]),
        ],
        "resolver": _resolver_python,
    },
    "ghcr.io/devcontainers/features/rust": {
        "commands": [
            ("rustc", ["rustc", "--version"]),
            ("cargo", ["cargo", "--version"]),
            ("rustup", ["rustup", "--version"]),
        ],
        "resolver": _resolver_rust,
    },
}


def _run_crane(args: List[str], *, text: bool = True, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run([CRANE_BIN, *args], check=check, capture_output=True, text=text)
    return proc


def _load_devcontainer_features() -> List[Dict[str, object]]:
    if not DEVCONTAINER_JSON.exists():
        raise SystemExit(f"devcontainer config not found: {DEVCONTAINER_JSON}")

    data = json.loads(DEVCONTAINER_JSON.read_text())
    features = data.get("features", {})
    entries: List[Dict[str, object]] = []
    for ref, options in features.items():
        if options is None:
            options = {}
        entries.append({
            "ref": ref,
            "options": options,
        })
    entries.sort(key=lambda item: item["ref"])
    return entries


def _extract_feature_metadata_from_manifest(ref: str) -> Dict[str, object]:
    manifest = _run_crane(["manifest", ref]).stdout
    manifest_json = json.loads(manifest)
    annotations = manifest_json.get("annotations", {})
    metadata_raw = annotations.get("dev.containers.metadata")
    if metadata_raw:
        try:
            return json.loads(metadata_raw)
        except json.JSONDecodeError:
            pass
    return {}


def _extract_feature_metadata_fallback(ref: str) -> Dict[str, object]:
    proc = _run_crane(["export", ref, "-"], text=False)
    with tarfile.open(fileobj=io.BytesIO(proc.stdout), mode="r:*") as tar:
        for member in tar.getmembers():
            name = member.name.lstrip("./")
            if name == "devcontainer-feature.json":
                extracted = tar.extractfile(member)
                if extracted is None:
                    break
                return json.loads(extracted.read().decode("utf-8"))
    return {}


def _collect_product_versions(options: Dict[str, object]) -> Dict[str, object]:
    product_versions = {}
    for key, value in options.items():
        if "version" in key.lower():
            product_versions[key] = value
    return product_versions


def _sort_dict(obj: Dict[str, object]) -> Dict[str, object]:
    return {key: obj[key] for key in sorted(obj.keys())}


def _get_feature_config(ref: str) -> Optional[Dict[str, object]]:
    for prefix, config in FEATURE_CONFIGS.items():
        if ref.startswith(prefix):
            return config
    return None


def _run_command(cmd: List[str]) -> Optional[str]:
    try:
        proc = subprocess.run(cmd, check=True, capture_output=True, text=True)
        output = proc.stdout.strip() or proc.stderr.strip()
        return output.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def _collect_runtime_versions(config: Optional[Dict[str, object]]) -> Dict[str, str]:
    results: Dict[str, str] = {}
    if not config:
        return results
    for entry in config.get("commands", []):
        name, command = entry
        output = _run_command(command)
        if output:
            results[name] = output
    return _sort_dict(results) if results else {}


def _collect_expected_versions(config: Optional[Dict[str, object]], options: Dict[str, object]) -> Dict[str, str]:
    if not config:
        return {}
    resolver = config.get("resolver")
    if not resolver:
        return {}
    try:
        expected = resolver(options)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        return {"__resolver_error": str(exc)}
    return _sort_dict(expected) if expected else {}


def _snapshot_features() -> List[FeatureInfo]:
    features = []
    for entry in _load_devcontainer_features():
        ref = entry["ref"]
        options = entry["options"] or {}
        options = _sort_dict(options)
        config = _get_feature_config(ref)

        digest = _run_crane(["digest", ref]).stdout.strip()
        metadata = _extract_feature_metadata_from_manifest(ref)
        if not metadata:
            metadata = _extract_feature_metadata_fallback(ref)

        feature_version = metadata.get("version", "")
        name = metadata.get("name")

        product_versions = _collect_product_versions(options)
        if product_versions:
            product_versions = _sort_dict(product_versions)

        runtime_versions = _collect_runtime_versions(config)
        expected_versions = _collect_expected_versions(config, options)

        features.append({
            "ref": ref,
            "name": name,
            "digest": digest,
            "featureVersion": feature_version,
            "options": options,
            "productVersions": product_versions,
            "runtimeVersions": runtime_versions,
            "expectedVersions": expected_versions,
        })
    return features


def _load_existing_lock(lock_path: Path) -> Dict[str, object]:
    return json.loads(lock_path.read_text())


def command_generate(lock_path: Path) -> int:
    features = _snapshot_features()
    lock_data = {
        "generatedAt": _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "features": features,
    }

    if lock_path.exists():
        current = _load_existing_lock(lock_path)
        current_copy = dict(current)
        current_copy.pop("generatedAt", None)
        new_copy = dict(lock_data)
        new_copy.pop("generatedAt", None)
        if current_copy == new_copy:
            print(f"features.lock is up-to-date at {lock_path}")
            return 0

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(json.dumps(lock_data, indent=2, sort_keys=True) + "\n")
    print(f"Wrote {lock_path}")
    return 0


def command_check(lock_path: Path) -> int:
    if not lock_path.exists():
        print(f"Missing lock file: {lock_path}", file=sys.stderr)
        return 2

    existing = _load_existing_lock(lock_path)
    new_features = _snapshot_features()

    existing_map = {item["ref"]: item for item in existing.get("features", [])}
    new_map = {item["ref"]: item for item in new_features}

    diffs: List[str] = []

    for ref in sorted(set(existing_map) | set(new_map)):
        before = existing_map.get(ref)
        after = new_map.get(ref)
        if before is None:
            diffs.append(f"NEW feature {ref} -> {after.get('digest')} (version {after.get('featureVersion') or 'unknown'})")
            continue
        if after is None:
            diffs.append(f"REMOVED feature {ref}")
            continue

        changes = []
        for key in ("digest", "featureVersion", "options", "productVersions", "runtimeVersions", "expectedVersions"):
            if before.get(key) != after.get(key):
                changes.append(f"{key}: {before.get(key)} -> {after.get(key)}")
        if changes:
            diffs.append(f"CHANGED {ref}: " + "; ".join(changes))

    if diffs:
        print("Feature updates detected:")
        for diff in diffs:
            print(f"  - {diff}")
        print("Run: .devcontainer/scripts/features-lock.sh generate", file=sys.stderr)
        return 1

    print("All devcontainer features match features.lock")
    return 0


def main(argv: List[str]) -> int:
    if len(argv) < 2 or argv[1] not in {"generate", "check"}:
        print("Usage: features_lock.py {generate|check} [lock_path]", file=sys.stderr)
        return 2

    lock_path = Path(argv[2]) if len(argv) > 2 else LOCK_PATH_DEFAULT
    command = argv[1]

    try:
        if command == "generate":
            return command_generate(lock_path)
        return command_check(lock_path)
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else exc.stderr
        print(f"crane command failed: {' '.join(exc.cmd)}", file=sys.stderr)
        if stderr:
            print(stderr, file=sys.stderr)
        return exc.returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
