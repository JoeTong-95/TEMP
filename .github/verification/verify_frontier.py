#!/usr/bin/env python3
"""Ephemeral issue-256 live frontier verifier with redacted evidence."""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import http.client
import json
import os
from pathlib import Path
import shutil
import sys
import types
from typing import Any, Iterator, Mapping
from urllib.parse import urlsplit

REPOSITORY = "JoeTong-95/TEMP"
REPOSITORY_URL = "https://github.com/JoeTong-95/TEMP"
EXPECTED_REVISION = "ea3c780741c5e2e70470f5389d130a4495517464"
PROJECT_ID = "issue-256-live-frontier"
PARENT_ISSUE = 1
TOKEN_IDENTITY = "github-actions-job"
MAX_RESPONSE_BYTES = 512 * 1024
API_ORIGIN = "api.github.com"
WORKFLOW_PATH = ".github/workflows/issue-256-live.yml"
JOB_PERMISSIONS = {
    "actions": "read",
    "contents": "read",
    "issues": "read",
    "metadata": "read",
    "pull-requests": "read",
}
REAL_HTTPS_CONNECTION = http.client.HTTPSConnection


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def git_blob_sha(data: bytes) -> str:
    return hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()


def category(path: str) -> str:
    parsed = urlsplit(path)
    if parsed.path == "/rate_limit":
        return "credential_preflight"
    if parsed.path == f"/repos/{REPOSITORY}/issues":
        return "issue_page"
    if parsed.path.endswith("/dependencies/blocked_by"):
        return "relationship_blocked_by"
    if parsed.path.endswith("/sub_issues"):
        return "relationship_sub_issues"
    return "other_get"


class SanitizedResponse:
    def __init__(self, body: bytes, status: int, record: dict[str, Any]) -> None:
        self.body, self.status, self.record = body, status, record

    def read(self, amount: int = -1) -> bytes:
        if amount is None or amount < 0:
            data, self.body = self.body, b""
        else:
            data, self.body = self.body[:amount], self.body[amount:]
        self.record["read_bytes"] += len(data)
        return data

    def getheader(self, _name: str, default: Any = None) -> Any:
        return default


class RecordingConnection:
    """Sanitized transport wrapper around the production HTTPS client."""

    def __init__(
        self,
        host: str,
        port: int | None = None,
        *,
        timeout: float | None = None,
        records: list[dict[str, Any]],
    ) -> None:
        self.inner = REAL_HTTPS_CONNECTION(
            host, port or 443, timeout=timeout if timeout is not None else 10.0
        )
        self.records = records
        self.record: dict[str, Any] | None = None

    def request(
        self,
        method: str,
        path: str,
        body: Any = None,
        headers: Mapping[str, str] | None = None,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        self.record = {
            "method": method,
            "path": path,
            "category": category(path),
            "status": None,
            "response_bytes": None,
            "read_bytes": 0,
        }
        self.records.append(self.record)
        self.inner.request(
            method, path, body=body, headers=headers, *args, **kwargs
        )

    def getresponse(self) -> SanitizedResponse:
        if self.record is None:
            raise RuntimeError("production response had no request record")
        response = self.inner.getresponse()
        body = response.read()
        self.record["status"] = response.status
        self.record["response_bytes"] = len(body)
        try:
            parsed: Any = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            parsed = None
        if self.record["category"] == "issue_page" and isinstance(parsed, list):
            numbers = [
                item.get("number")
                for item in parsed
                if isinstance(item, Mapping) and type(item.get("number")) is int
            ]
            self.record.update(
                row_count=len(parsed),
                min_issue_number=min(numbers) if numbers else None,
                max_issue_number=max(numbers) if numbers else None,
            )
        elif self.record["category"].startswith("relationship_") and isinstance(parsed, list):
            self.record["row_count"] = len(parsed)
        return SanitizedResponse(body, response.status, self.record)

    def close(self) -> None:
        self.inner.close()


@contextmanager
def recording_transport(records: list[dict[str, Any]]) -> Iterator[None]:
    class BoundRecordingConnection(RecordingConnection):
        def __init__(
            self,
            host: str,
            port: int | None = None,
            timeout: float | None = None,
            *args: Any,
            **kwargs: Any,
        ) -> None:
            del args, kwargs
            super().__init__(host, port, timeout=timeout, records=records)

    class ClientProxy:
        HTTPSConnection = BoundRecordingConnection

        def __getattr__(self, name: str) -> Any:
            return getattr(http.client, name)

    import management.management_service as management_service_module

    original_http = management_service_module.http
    management_service_module.http = types.SimpleNamespace(client=ClientProxy())
    try:
        yield
    finally:
        management_service_module.http = original_http


def api_call(
    token: str,
    method: str,
    path: str,
    payload: Mapping[str, Any] | None = None,
    *,
    timeout: float = 20.0,
) -> tuple[int, bytes]:
    body = None if payload is None else canon(dict(payload)).encode("utf-8")
    headers = {
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "agent-infrastructure-issue-256-verifier/1",
    }
    if body is not None:
        headers["Content-Type"] = "application/json"
    connection = REAL_HTTPS_CONNECTION(API_ORIGIN, 443, timeout=timeout)
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        return response.status, response.read(256 * 1024 + 1)
    finally:
        connection.close()


def prepare_project(project_db: Path) -> Any:
    from management.project_control import ProjectControl

    projects = ProjectControl(
        project_db, validate_configuration=lambda _project, _payload: True
    )
    payload = {
        "prd_reference": REPOSITORY_URL + "/issues/1",
        "agents": [
            {
                "id": "frontier-verifier",
                "role": "project_orchestrator",
                "capability_bindings": ["management"],
            }
        ],
        "resource_requested_envelope": {"cpu_cores": 1, "memory_bytes": 1},
        "forge_binding": {
            "provider": "github",
            "repository_url": REPOSITORY_URL,
            "identity": TOKEN_IDENTITY,
        },
        "github_frontier": {"parent_issue_number": PARENT_ISSUE},
    }
    draft = projects.propose(
        PROJECT_ID,
        payload,
        actor_role="human",
        actor_id="issue-256-verification",
        idempotency_key="issue-256-verification-propose",
    )
    projects.confirm(
        PROJECT_ID,
        draft["revision"],
        actor_role="human",
        actor_id="issue-256-human-confirmation",
    )
    return projects


def stable(value: Mapping[str, Any]) -> dict[str, Any]:
    copy = json.loads(canon(dict(value)))
    copy.pop("freshness", None)
    copy.pop("status", None)
    return copy


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    pages = [r for r in records if r["category"] == "issue_page"]
    relationships = [
        r for r in records if r["category"].startswith("relationship_")
    ]
    methods = [r["method"] for r in records]
    return {
        "request_count": len(records),
        "by_category": dict(Counter(r["category"] for r in records)),
        "all_methods_get": all(method == "GET" for method in methods),
        "methods_observed": sorted(set(methods)),
        "issue_pages": pages,
        "relationship_gets": len(relationships),
        "max_response_bytes_configured": MAX_RESPONSE_BYTES,
        "all_responses_within_bound": all(
            isinstance(r.get("response_bytes"), int)
            and r["response_bytes"] <= MAX_RESPONSE_BYTES
            for r in records
        ),
    }


def validate_source(source_root: Path, bundle: Path) -> dict[str, Any]:
    expected = os.environ.get("EXPECTED_SOURCE_BUNDLE_SHA256", "")
    actual = hashlib.sha256(bundle.read_bytes()).hexdigest()
    if actual != expected:
        raise RuntimeError("source bundle digest mismatch")
    marker = (source_root / "CANDIDATE_REVISION.txt").read_text(
        encoding="utf-8-sig"
    ).strip()
    if marker != EXPECTED_REVISION:
        raise RuntimeError("candidate revision marker mismatch")
    lines = (source_root / "SOURCE_BLOB_MANIFEST.txt").read_text(
        encoding="utf-8-sig"
    ).splitlines()
    count = 0
    for line in lines:
        if not line.strip():
            continue
        expected_blob, relative = line.split(" ", 1)
        path = source_root / relative
        if not path.is_file() or git_blob_sha(path.read_bytes()) != expected_blob:
            raise RuntimeError("candidate source blob mismatch")
        count += 1
    if count < 1:
        raise RuntimeError("candidate source manifest is empty")
    return {
        "candidate_revision": EXPECTED_REVISION,
        "source_bundle_sha256": actual,
        "source_blob_count": count,
        "source_manifest_verified": True,
    }


def main() -> int:
    started = now()
    token = os.environ.get("EPHEMERAL_JOB_TOKEN", "")
    broad_absent = not (
        os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    )
    boundary = {
        "github_actions": os.environ.get("GITHUB_ACTIONS") == "true",
        "repository": os.environ.get("GITHUB_REPOSITORY"),
        "event": os.environ.get("GITHUB_EVENT_NAME"),
        "run_id_present": bool(os.environ.get("GITHUB_RUN_ID")),
        "token_env_name": "EPHEMERAL_JOB_TOKEN",
        "broad_token_env_absent": broad_absent,
        "job_permissions": JOB_PERMISSIONS,
    }
    evidence: dict[str, Any] = {
        "schema": "agent-infrastructure.verification.github-frontier.v1",
        "status": "fail",
        "attempt": "v11-issue-256-verification-r8",
        "issue_number": 256,
        "started_at": started,
        "credential": {
            "source": "GitHub Actions github.token context",
            "token_value_recorded": False,
            "authorization_headers_recorded": False,
            "response_bodies_recorded": False,
            "authority_boundary": boundary,
        },
        "production_composition": {
            "entrypoint": "management.management_service.build_github_frontier_service",
            "path": [
                "build_github_frontier_service",
                "GitHubFrontierService.refresh",
                "GitHubFrontier.refresh_with_recovery",
                "HttpGitHubFrontierAdapter.pages",
                "GitHubFrontierStore",
            ],
            "candidate_revision": EXPECTED_REVISION,
            "repository": REPOSITORY_URL,
            "project_id": PROJECT_ID,
            "parent_issue_number": PARENT_ISSUE,
        },
        "source": {},
        "frontier": {},
        "write_denials": [],
        "cleanup": {},
    }
    workspace = Path(os.environ.get("VERIFICATION_WORKSPACE", ".verify")).resolve()
    state = workspace / "state"
    token_file = state / "ephemeral-job.token"
    source_root = workspace / "source"
    bundle = Path(
        os.environ.get(
            "SOURCE_BUNDLE",
            ".github/verification/agent-infrastructure-source.tar.gz",
        )
    ).resolve()
    failure: dict[str, str] | None = None
    stage = "boundary"
    try:
        if (
            not token
            or not boundary["github_actions"]
            or boundary["repository"] != REPOSITORY
            or not boundary["event"]
            or not boundary["run_id_present"]
            or not broad_absent
        ):
            raise RuntimeError("explicit ephemeral GitHub Actions boundary missing")
        stage = "source"
        source_root.mkdir(parents=True, exist_ok=True)
        evidence["source"] = validate_source(source_root, bundle)
        stage = "credential_preflight"
        state.mkdir(parents=True, exist_ok=True)
        token_file.write_text(token + "\n", encoding="utf-8")
        token_file.chmod(0o600)
        rate_status, rate_body = api_call(
            token, "GET", "/rate_limit", timeout=5.0
        )
        try:
            rate_doc: Any = json.loads(rate_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            rate_doc = None
        if rate_status != 200:
            raise RuntimeError("credential preflight did not succeed")
        rate = rate_doc.get("rate", {}) if isinstance(rate_doc, Mapping) else {}
        evidence["credential"]["preflight"] = {
            "method": "GET",
            "path": "/rate_limit",
            "status": rate_status,
            "response_bytes_read": len(rate_body),
            "remaining": rate.get("remaining") if isinstance(rate, Mapping) else None,
            "limit": rate.get("limit") if isinstance(rate, Mapping) else None,
        }
        stage = "authority_reads"
        repo_status, repo_body = api_call(
            token, "GET", f"/repos/{REPOSITORY}", timeout=10.0
        )
        workflows_status, workflows_body = api_call(
            token,
            "GET",
            f"/repos/{REPOSITORY}/actions/workflows",
            timeout=10.0,
        )
        if repo_status != 200 or workflows_status != 200:
            raise RuntimeError("credential authority read did not succeed")
        try:
            repo_doc: Any = json.loads(repo_body.decode("utf-8"))
            workflows_doc: Any = json.loads(workflows_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise RuntimeError("credential authority response was invalid")
        workflows = (
            workflows_doc.get("workflows", [])
            if isinstance(workflows_doc, Mapping)
            else []
        )
        workflow_id = next(
            (
                item.get("id")
                for item in workflows
                if isinstance(item, Mapping)
                and item.get("path") == WORKFLOW_PATH
                and type(item.get("id")) is int
            ),
            None,
        )
        if workflow_id is None:
            raise RuntimeError("verification workflow was not visible to job token")
        evidence["credential"]["authority_reads"] = {
            "repository_get_status": repo_status,
            "workflow_list_status": workflows_status,
            "workflow_id_present": True,
            "permission_projection": (
                repo_doc.get("permissions")
                if isinstance(repo_doc, Mapping)
                and isinstance(repo_doc.get("permissions"), Mapping)
                else None
            ),
        }
        stage = "write_denials"
        description = (
            repo_doc.get("description") if isinstance(repo_doc, Mapping) else None
        )
        probes = [
            (
                "contents",
                "PUT",
                f"/repos/{REPOSITORY}/contents/.github/verification/"
                "permission-probe-contents.txt",
                {
                    "message": "permission probe must be denied",
                    "content": "cHJvYmU=",
                    "branch": "main",
                },
            ),
            (
                "workflows",
                "POST",
                f"/repos/{REPOSITORY}/actions/workflows/"
                f"{workflow_id}/dispatches",
                {"ref": "__codex_permission_probe_missing_ref__"},
            ),
            (
                "issues",
                "POST",
                f"/repos/{REPOSITORY}/issues",
                {
                    "title": "permission probe must be denied",
                    "body": "This issue must not be created.",
                },
            ),
            (
                "pull_requests",
                "POST",
                f"/repos/{REPOSITORY}/pulls",
                {
                    "title": "permission probe must be denied",
                    "head": "__codex_permission_probe_missing_head__",
                    "base": "main",
                    "body": "This pull request must not be created.",
                },
            ),
            (
                "administration",
                "PATCH",
                f"/repos/{REPOSITORY}",
                {"description": description},
            ),
        ]
        for name, method, path, payload in probes:
            status, body = api_call(token, method, path, payload, timeout=10.0)
            evidence["write_denials"].append(
                {
                    "name": name,
                    "method": method,
                    "path": path,
                    "status": status,
                    "response_bytes_read": len(body),
                    "denied": status == 403,
                }
            )
        if any(item["status"] != 403 for item in evidence["write_denials"]):
            raise RuntimeError("one or more write probes were not denied")
        stage = "production_frontier_replay"
        sys.path.insert(0, str(source_root / "src"))
        from management.management_service import (
            GitHubFrontierConfiguration,
            build_github_frontier_service,
        )

        projects = prepare_project(state / "projects.sqlite3")
        configuration = GitHubFrontierConfiguration(
            database=state / "github-frontier.sqlite3",
            bearer_token_files={TOKEN_IDENTITY: token_file},
            timeout_seconds=10.0,
            max_response_bytes=MAX_RESPONSE_BYTES,
        )
        service = build_github_frontier_service(projects, configuration)
        records: list[dict[str, Any]] = []
        with recording_transport(records):
            refreshed = service.refresh(PROJECT_ID)
            read_back = service.view(PROJECT_ID)
        request_summary = summarize(records)
        freshness = refreshed.get("freshness", {})
        actionable = refreshed.get("actionable", [])
        actionable_numbers = [
            item.get("number") for item in actionable if isinstance(item, Mapping)
        ]
        pages = request_summary["issue_pages"]
        pagination = (
            len(pages) >= 2
            and pages[0].get("row_count") == 100
            and (pages[1].get("row_count") or 0) >= 1
        )
        agreement = stable(refreshed) == stable(read_back)
        if (
            refreshed.get("status") != "available"
            or not isinstance(freshness, Mapping)
            or freshness.get("state") != "fresh"
            or refreshed.get("project_revision") != 1
            or refreshed.get("repository") != REPOSITORY_URL
            or refreshed.get("parent_issue_number") != PARENT_ISSUE
            or actionable_numbers != [2]
            or PARENT_ISSUE in actionable_numbers
            or not request_summary["all_methods_get"]
            or not request_summary["all_responses_within_bound"]
            or not pagination
            or not request_summary["relationship_gets"]
            or not agreement
        ):
            raise RuntimeError("production frontier replay did not satisfy its contract")
        evidence["frontier"] = {
            "status": refreshed.get("status"),
            "freshness": freshness,
            "refresh_read_agreement": agreement,
            "project_revision": refreshed.get("project_revision"),
            "repository": refreshed.get("repository"),
            "parent_issue_number": refreshed.get("parent_issue_number"),
            "actionable_numbers": actionable_numbers,
            "specification_excluded_from_actionable": PARENT_ISSUE not in actionable_numbers,
            "pagination_preserved": pagination,
            "request_accounting": request_summary,
            "redaction_checks": {
                "token_value_recorded": False,
                "authorization_headers_recorded": False,
                "response_bodies_recorded": False,
            },
        }
        evidence["credential"]["write_denial_all_403"] = True
    except Exception as error:
        failure = {"stage": stage, "error_type": type(error).__name__}
    finally:
        cleanup = {
            "token_file_removed": False,
            "sqlite_state_removed": False,
            "workspace_state_removed": False,
        }
        try:
            if token_file.exists():
                token_file.unlink()
            cleanup["token_file_removed"] = not token_file.exists()
        except OSError:
            pass
        try:
            if state.exists():
                shutil.rmtree(state)
            cleanup["sqlite_state_removed"] = not any(
                workspace.glob("**/*.sqlite3*")
            ) if workspace.exists() else True
        except OSError:
            pass
        try:
            if workspace.exists():
                shutil.rmtree(workspace)
            cleanup["workspace_state_removed"] = not workspace.exists()
        except OSError:
            pass
        evidence["cleanup"] = cleanup
    evidence["finished_at"] = now()
    evidence["status"] = (
        "pass"
        if failure is None
        and evidence["cleanup"].get("token_file_removed") is True
        and evidence["cleanup"].get("sqlite_state_removed") is True
        and evidence["cleanup"].get("workspace_state_removed") is True
        else "fail"
    )
    if failure is not None:
        evidence["failure"] = failure
    serialized = canon(evidence)
    if token in serialized or "Bearer " in serialized or '"Authorization"' in serialized:
        return_code = 1
    else:
        return_code = 0 if evidence["status"] == "pass" else 1
    print(json.dumps(evidence, sort_keys=True))
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
