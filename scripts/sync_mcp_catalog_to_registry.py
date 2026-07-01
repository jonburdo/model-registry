#!/usr/bin/env python3
"""Draft sync script for MCP catalog -> mcp-registry.

This script is intentionally conservative:

- It uses the live MCP catalog API as the primary source.
- It emits normalized registry payloads in dry-run mode by default.
- It supports override files for naming and version normalization.
- It only performs registry writes when --apply is passed.

Override file format (JSON):

{
  "names": {
    "rh_mcp_servers:openshift-mcp-server": "io.github.openshift/openshift-mcp-server"
  },
  "versions": {
    "rh_mcp_servers:openshift-mcp-server": "0.2.0"
  }
}
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_CATALOG_URL = "https://model-catalog.apps.rosa.jburdo2.53ms.p3.openshiftapps.com"
CATALOG_LIST_PATH = "/api/mcp_catalog/v1alpha1/mcp_servers"
REGISTRY_SERVERS_PATH = "/ajax-api/3.0/mlflow/mcp-servers/"
REGISTRY_VERSION_PATH_TEMPLATE = "/ajax-api/3.0/mlflow/mcp-servers/{name}/versions"
REGISTRY_BINDING_PATH_TEMPLATE = "/ajax-api/3.0/mlflow/mcp-servers/{name}/bindings"

SEMVER_RE = re.compile(
    r"^(?P<major>0|[1-9]\d*)"
    r"\.(?P<minor>0|[1-9]\d*)"
    r"\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<prerelease>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+(?P<build>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)

SHORT_SEMVER_RE = re.compile(
    r"^(?P<major>0|[1-9]\d*)"
    r"(?:\.(?P<minor>0|[1-9]\d*))?"
    r"(?:\.(?P<patch>0|[1-9]\d*))?$"
)


@dataclass
class SyncBundle:
    catalog_id: str
    catalog_name: str
    registry_name: str
    registry_version: str
    server_payload: dict[str, Any]
    version_payload: dict[str, Any]
    binding_payloads: list[dict[str, Any]]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog-url", default=os.environ.get("MODEL_CATALOG_URL", DEFAULT_CATALOG_URL))
    parser.add_argument("--catalog-token", default=os.environ.get("MODEL_CATALOG_TOKEN"))
    parser.add_argument("--registry-url", default=os.environ.get("MCP_REGISTRY_URL") or os.environ.get("MLFLOW_TRACKING_URI"))
    parser.add_argument("--registry-token", default=os.environ.get("MCP_REGISTRY_TOKEN"))
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--limit", type=int, default=0, help="Maximum number of catalog servers to process (0 = all)")
    parser.add_argument("--include-tools", action="store_true", default=True, help="Request tools from the catalog API")
    parser.add_argument("--source-label", action="append", default=[], help="Optional sourceLabel filter; can be passed multiple times")
    parser.add_argument("--namespace-prefix", default="com.redhat.catalog", help="Fallback prefix for synthesized registry names")
    parser.add_argument("--override-file", type=Path, help="JSON file with name/version overrides")
    parser.add_argument("--output", type=Path, help="Write dry-run/apply summary JSON here")
    parser.add_argument("--strict-semver", action="store_true", default=True, help="Skip entries whose version cannot be normalized to semver")
    parser.add_argument("--create-access-bindings", action="store_true", help="Create registry access binding payloads from remote endpoints")
    parser.add_argument("--apply", action="store_true", help="Actually POST to the registry")
    return parser.parse_args()


def load_overrides(path: Path | None) -> dict[str, dict[str, str]]:
    if not path:
        return {"names": {}, "versions": {}}
    data = json.loads(path.read_text())
    return {
        "names": data.get("names", {}),
        "versions": data.get("versions", {}),
    }


def key_for(server: dict[str, Any]) -> str:
    return f"{server.get('source_id', '')}:{server.get('name', '')}"


def get_oc_token() -> str | None:
    try:
        proc = subprocess.run(
            ["oc", "whoami", "-t"],
            check=True,
            capture_output=True,
            text=True,
        )
    except Exception:
        return None
    token = proc.stdout.strip()
    return token or None


def http_json(
    method: str,
    url: str,
    token: str | None = None,
    payload: dict[str, Any] | None = None,
) -> Any:
    data = None
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {url} failed with {exc.code}: {body}") from exc


def fetch_catalog_servers(args: argparse.Namespace) -> list[dict[str, Any]]:
    token = args.catalog_token or get_oc_token()
    next_page = ""
    items: list[dict[str, Any]] = []
    while True:
        query: dict[str, Any] = {
            "pageSize": str(args.page_size),
            "includeTools": "true" if args.include_tools else "false",
            "toolLimit": "100",
        }
        if next_page:
            query["nextPageToken"] = next_page
        if args.source_label:
            query["sourceLabel"] = ",".join(args.source_label)
        url = f"{args.catalog_url.rstrip('/')}{CATALOG_LIST_PATH}?{urllib.parse.urlencode(query)}"
        response = http_json("GET", url, token=token)
        items.extend(response.get("items", []))
        if args.limit and len(items) >= args.limit:
            return items[: args.limit]
        next_page = response.get("nextPageToken") or ""
        if not next_page:
            return items


def normalize_slug(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9._/-]+", "-", value)
    value = re.sub(r"-{2,}", "-", value)
    return value.strip("-")


def derive_registry_name(server: dict[str, Any], overrides: dict[str, dict[str, str]], namespace_prefix: str) -> str:
    override = overrides["names"].get(key_for(server))
    if override:
        return override

    source_code = server.get("sourceCode") or ""
    repo_url = server.get("repositoryUrl") or ""

    github_match = re.search(r"github\.com[:/]+([^/]+)/([^/]+?)(?:\.git)?$", repo_url)
    if github_match:
        owner = normalize_slug(github_match.group(1))
        repo = normalize_slug(github_match.group(2))
        return f"io.github.{owner}/{repo}"

    if source_code.count("/") == 1:
        owner, repo = source_code.split("/", 1)
        return f"io.github.{normalize_slug(owner)}/{normalize_slug(repo)}"

    return f"{normalize_slug(namespace_prefix)}/{normalize_slug(server['name'])}"


def looks_like_semver(value: str) -> bool:
    return bool(SEMVER_RE.match(value))


def coerce_short_semver(value: str) -> str | None:
    if looks_like_semver(value):
        return value
    stripped = value.strip().lstrip("v")
    match = SHORT_SEMVER_RE.match(stripped)
    if not match:
        return None
    major = match.group("major")
    minor = match.group("minor") or "0"
    patch = match.group("patch") or "0"
    return f"{major}.{minor}.{patch}"


def extract_artifact_tag(artifact_uri: str) -> str | None:
    if ":" not in artifact_uri:
        return None
    return artifact_uri.rsplit(":", 1)[-1]


def normalize_version(server: dict[str, Any], overrides: dict[str, dict[str, str]]) -> str | None:
    override = overrides["versions"].get(key_for(server))
    if override:
        return coerce_short_semver(override)

    raw = (server.get("version") or "").strip()
    if not raw:
        return None
    direct = coerce_short_semver(raw)
    if direct and raw.lower() != "latest":
        return direct

    for artifact in server.get("artifacts") or []:
        uri = artifact.get("uri")
        if not uri:
            continue
        tag = extract_artifact_tag(uri)
        if not tag:
            continue
        coerced = coerce_short_semver(tag)
        if coerced:
            return coerced

    return None


def choose_transport(server: dict[str, Any]) -> str | None:
    transports = server.get("transports") or []
    for candidate in ("http", "sse", "stdio"):
        if candidate in transports:
            return candidate
    return transports[0] if transports else None


def build_packages(server: dict[str, Any]) -> list[dict[str, Any]]:
    packages: list[dict[str, Any]] = []
    runtime = server.get("runtimeMetadata") or {}
    chosen_transport = choose_transport(server)
    for artifact in server.get("artifacts") or []:
        uri = artifact.get("uri")
        if not uri:
            continue
        package = {
            "registryType": "oci",
            "identifier": uri,
        }
        if chosen_transport:
            package["transport"] = chosen_transport
        if runtime.get("defaultArgs"):
            package["defaultArgs"] = runtime["defaultArgs"]
        if runtime.get("defaultPort") is not None:
            package["defaultPort"] = runtime["defaultPort"]
        if runtime.get("mcpPath"):
            package["mcpPath"] = runtime["mcpPath"]
        if runtime.get("prerequisites"):
            package["_meta"] = {"prerequisites": runtime["prerequisites"]}
        packages.append(package)
    return packages


def build_remotes(server: dict[str, Any]) -> list[dict[str, Any]]:
    remotes: list[dict[str, Any]] = []
    endpoints = server.get("endpoints") or {}
    if isinstance(endpoints, dict):
        http_url = endpoints.get("http")
        if http_url:
            remotes.append({"type": "streamable-http", "url": http_url})
        sse_url = endpoints.get("sse")
        if sse_url:
            remotes.append({"type": "sse", "url": sse_url})
    return remotes


def build_tool_payloads(server: dict[str, Any]) -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []
    for tool in server.get("tools") or []:
        properties: dict[str, Any] = {}
        required: list[str] = []
        for parameter in tool.get("parameters") or []:
            param_name = parameter.get("name")
            if not param_name:
                continue
            param_type = parameter.get("type") or "string"
            properties[param_name] = {
                "type": param_type,
            }
            if parameter.get("description"):
                properties[param_name]["description"] = parameter["description"]
            if parameter.get("required"):
                required.append(param_name)
        input_schema = {
            "type": "object",
            "properties": properties,
        }
        if required:
            input_schema["required"] = required
        tools.append(
            {
                "name": tool["name"],
                "description": tool.get("description"),
                "inputSchema": input_schema if properties else None,
                "annotations": {
                    "com.redhat.catalog": {
                        "accessType": tool.get("accessType"),
                    }
                },
            }
        )
    return tools


def build_icons(server: dict[str, Any]) -> list[dict[str, Any]] | None:
    logo = server.get("logo")
    if not logo:
        return None
    return [{"src": logo}]


def build_server_json(server: dict[str, Any], registry_name: str, registry_version: str) -> dict[str, Any]:
    repository = server.get("repositoryUrl")
    if not repository and server.get("sourceCode") and "/" in server["sourceCode"]:
        repository = f"https://github.com/{server['sourceCode']}"

    server_json: dict[str, Any] = {
        "name": registry_name,
        "version": registry_version,
        "title": server.get("name"),
        "description": server.get("description"),
        "repository": repository,
        "websiteUrl": server.get("documentationUrl"),
        "packages": build_packages(server) or None,
        "remotes": build_remotes(server) or None,
        "_meta": {
            "com.redhat.catalog": {
                "catalog_server_id": server.get("id"),
                "source_id": server.get("source_id"),
                "provider": server.get("provider"),
                "raw_name": server.get("name"),
                "raw_version": server.get("version"),
                "deploymentMode": server.get("deploymentMode"),
                "transports": server.get("transports"),
                "artifacts": server.get("artifacts"),
                "runtimeMetadata": server.get("runtimeMetadata"),
                "securityIndicators": server.get("securityIndicators"),
                "customProperties": server.get("customProperties"),
                "publishedDate": server.get("publishedDate"),
                "catalog_record": server,
            }
        },
    }
    return {key: value for key, value in server_json.items() if value is not None}


def build_binding_payloads(server_json: dict[str, Any], create_access_bindings: bool) -> list[dict[str, Any]]:
    if not create_access_bindings:
        return []
    bindings: list[dict[str, Any]] = []
    for remote in server_json.get("remotes") or []:
        remote_type = remote.get("type")
        if remote_type not in {"streamable-http", "sse"}:
            continue
        bindings.append(
            {
                "endpoint_url": remote["url"],
                "transport_type": remote_type,
                "server_version": server_json["version"],
            }
        )
    return bindings


def build_bundle(server: dict[str, Any], args: argparse.Namespace, overrides: dict[str, dict[str, str]]) -> SyncBundle | None:
    registry_name = derive_registry_name(server, overrides, args.namespace_prefix)
    registry_version = normalize_version(server, overrides)
    if not registry_version and args.strict_semver:
        return None
    if not registry_version:
        registry_version = server.get("version") or "0.0.0"

    server_json = build_server_json(server, registry_name, registry_version)
    version_tools = build_tool_payloads(server)
    server_payload = {
        "name": registry_name,
        "description": server.get("description"),
        "icons": build_icons(server),
    }
    version_payload = {
        "display_name": server.get("name"),
        "server_json": server_json,
        "source": server.get("repositoryUrl") or server.get("documentationUrl"),
        "status": "draft",
        "tools": version_tools or None,
    }
    server_payload = {k: v for k, v in server_payload.items() if v is not None}
    version_payload = {k: v for k, v in version_payload.items() if v is not None}
    binding_payloads = build_binding_payloads(server_json, args.create_access_bindings)
    return SyncBundle(
        catalog_id=str(server.get("id")),
        catalog_name=server["name"],
        registry_name=registry_name,
        registry_version=registry_version,
        server_payload=server_payload,
        version_payload=version_payload,
        binding_payloads=binding_payloads,
    )


def registry_post(base_url: str, path: str, payload: dict[str, Any], token: str | None) -> Any:
    url = f"{base_url.rstrip('/')}{path}"
    return http_json("POST", url, token=token, payload=payload)


def safe_create_server(base_url: str, bundle: SyncBundle, token: str | None) -> Any:
    try:
        return registry_post(base_url, REGISTRY_SERVERS_PATH, bundle.server_payload, token)
    except RuntimeError as exc:
        if "already exists" in str(exc).lower() or "409" in str(exc):
            return {"message": "server already exists"}
        raise


def apply_bundle(base_url: str, token: str | None, bundle: SyncBundle) -> dict[str, Any]:
    server_response = safe_create_server(base_url, bundle, token)
    version_path = REGISTRY_VERSION_PATH_TEMPLATE.format(
        name=urllib.parse.quote(bundle.registry_name, safe="")
    )
    version_response = registry_post(base_url, version_path, bundle.version_payload, token)
    binding_responses = []
    for payload in bundle.binding_payloads:
        binding_path = REGISTRY_BINDING_PATH_TEMPLATE.format(
            name=urllib.parse.quote(bundle.registry_name, safe="")
        )
        binding_responses.append(registry_post(base_url, binding_path, payload, token))
    return {
        "server": server_response,
        "version": version_response,
        "bindings": binding_responses,
    }


def main() -> int:
    args = parse_args()
    overrides = load_overrides(args.override_file)
    catalog_servers = fetch_catalog_servers(args)

    bundles: list[SyncBundle] = []
    skipped: list[dict[str, Any]] = []
    for server in catalog_servers:
        bundle = build_bundle(server, args, overrides)
        if bundle is None:
            skipped.append(
                {
                    "catalog_id": server.get("id"),
                    "catalog_name": server.get("name"),
                    "reason": "version could not be normalized to semver",
                    "raw_version": server.get("version"),
                }
            )
            continue
        bundles.append(bundle)

    summary: dict[str, Any] = {
        "catalog_url": args.catalog_url,
        "processed": len(bundles),
        "skipped": skipped,
        "bundles": [
            {
                "catalog_id": bundle.catalog_id,
                "catalog_name": bundle.catalog_name,
                "registry_name": bundle.registry_name,
                "registry_version": bundle.registry_version,
                "server_payload": bundle.server_payload,
                "version_payload": bundle.version_payload,
                "binding_payloads": bundle.binding_payloads,
            }
            for bundle in bundles
        ],
    }

    if args.apply:
        if not args.registry_url:
            raise SystemExit("--registry-url or MCP_REGISTRY_URL/MLFLOW_TRACKING_URI is required with --apply")
        apply_results = []
        for bundle in bundles:
            apply_results.append(
                {
                    "catalog_name": bundle.catalog_name,
                    "registry_name": bundle.registry_name,
                    "result": apply_bundle(args.registry_url, args.registry_token, bundle),
                }
            )
        summary["apply_results"] = apply_results

    encoded = json.dumps(summary, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n")
    else:
        print(encoded)
    return 0


if __name__ == "__main__":
    sys.exit(main())
