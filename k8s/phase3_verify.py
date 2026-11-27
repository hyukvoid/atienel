"""Exercise the authenticated Phase 3 MCP tools over real Streamable HTTP."""

import asyncio
import base64
import json
import ssl
import sys
from datetime import UTC, datetime
from uuid import uuid4

import httpx
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from phase2_verify import container_status, http, kube, pod_for, port_forward, require, wait_for
from src.schemas import K8sInspectorOutput, RunbookRagOutput

PROTOCOL_VERSION = "2026-07-28"


def oom_target():
    def terminated():
        current = pod_for("chaos-oom-fixture")
        term = container_status(current).get("lastState", {}).get("terminated", {})
        if term.get("reason") == "OOMKilled" and term.get("exitCode") == 137:
            age = datetime.now(UTC) - datetime.fromisoformat(term["finishedAt"])
            if age.total_seconds() < 900:
                return current
        return None

    pod = terminated()
    if not pod:
        with port_forward("target-apps", "svc/chaos-app", 8080) as chaos:
            status, _, _ = http(f"{chaos}/faults/oom", method="POST")
            require(status == 202, "OOM fixture rejected trigger")
        pod = wait_for(terminated, "OOM fixture termination")
    return {
        "namespace": "target-apps",
        "pod_name": pod["metadata"]["name"],
        "pod_uid": pod["metadata"]["uid"],
        "container": "chaos-oom-fixture",
    }


async def call(client, name, request):
    response = await client.call_tool(name, {"request": request})
    require(isinstance(response.structured_content, dict), f"{name} returned no structured result")
    model = K8sInspectorOutput if name == "k8s_inspector" else RunbookRagOutput
    return model.model_validate_json(json.dumps(response.structured_content)).model_dump(
        mode="json"
    )


async def verify(url, tls, token, target, fresh_target):
    transport = StreamableHttpTransport(url, auth=token, verify=tls)
    async with httpx.AsyncClient(verify=tls, timeout=10, trust_env=False) as wire:
        headers = {"Accept": "application/json, text/event-stream"}
        for authorization in ({}, {"Authorization": f"Bearer invalid-{uuid4()}"}):
            reply = await wire.post(
                url,
                headers={**headers, **authorization},
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            )
            require(reply.status_code == 401, f"Expected HTTP 401, got {reply.status_code}")

    async with Client(transport, timeout=25) as client:
        tools = await client.list_tools()
        require(
            client.protocol_version == PROTOCOL_VERSION,
            f"Expected MCP {PROTOCOL_VERSION}, got {client.protocol_version}",
        )
        require(
            {tool.name for tool in tools} == {"k8s_inspector", "runbook_rag"},
            "Unexpected tool discovery result",
        )
        for tool in tools:
            require(
                "request" in tool.input_schema["properties"]
                and tool.output_schema
                and tool.annotations
                and tool.annotations.read_only_hint
                and tool.annotations.destructive_hint is False,
                f"Missing schemas or read-only annotations for {tool.name}",
            )
        print(
            f"TLS and Bearer authentication, MCP {PROTOCOL_VERSION} and both tool schemas verified"
        )

        evidence = await call(client, "k8s_inspector", target)
        require(evidence["pod_uid"] == target["pod_uid"], "Pod UID differs from request")
        container = evidence["containers"][0]
        require(
            container["termination"]["reason"] == "OOMKilled"
            and container["termination"]["exit_code"] == 137
            and container["limits"]["memory"] == "128Mi",
            "OOM termination or memory limit missing",
        )
        require(
            any(log["instance"] == "current" for log in evidence["logs"]),
            "Current log evidence missing",
        )
        missing = await call(client, "k8s_inspector", {**target, "pod_uid": str(uuid4())})
        require(
            missing["status"] == "not_found"
            and missing["logs"] == []
            and missing["gaps"][0]["code"] == "pod_uid_mismatch",
            "UID check failed",
        )
        gone = await call(client, "k8s_inspector", {**target, "pod_name": "phase3-gone-fixture"})
        require(
            gone["status"] == "not_found" and gone["gaps"][0]["code"] == "pod_not_found",
            "Missing Pod not distinguished",
        )
        empty = await call(client, "k8s_inspector", {"namespace": "target-apps"})
        require(empty["status"] == "unavailable", "Missing identity not distinguished")
        if fresh_target:
            fresh = await call(client, "k8s_inspector", fresh_target)
            require(
                any(
                    gap["scope"] == "logs_previous" and gap["code"] == "not_applicable"
                    for gap in fresh["gaps"]
                ),
                "Previous logs without restarts were not marked not applicable",
            )
        print("k8s_inspector returned logs, OOMKilled/137 and 128Mi; absence and UID checks passed")

        for query, document in [
            ("OOMKilled exit code 137 memory limit", "oom.md"),
            ("CrashLoopBackOff 시작 설정 오류", "crashloop.md"),
            ("HTTP 5xx 카운터 급증", "http-5xx.md"),
        ]:
            result = await call(client, "runbook_rag", {"query": query, "top_k": 10})
            require(result["status"] == "ok", f"Missing runbook matches for {document}")
            matches = result["matches"]
            require(
                any(row["source_uri"].endswith("/" + document) for row in matches)
                and all(
                    row["section"]
                    and row["excerpt"]
                    and row["source_revision"]
                    and 0 <= row["cosine_distance"] <= 0.35
                    for row in matches
                )
                and matches == sorted(matches, key=lambda row: row["cosine_distance"]),
                f"Expected citable ranked results for {document}",
            )
        for name, argument in [
            ("k8s_inspector", {**target, "namespace": "aiops-core"}),
            ("k8s_inspector", {**target, "pod_uid": "invalid"}),
            ("runbook_rag", {"query": "OOM", "top_k": "2"}),
            ("runbook_rag", {"query": "OOM", "extra": True}),
            ("runbook_rag", {"query": " "}),
        ]:
            rejected = await client.call_tool(name, {"request": argument}, raise_on_error=False)
            require(rejected.is_error, f"{name} accepted an invalid input")
        print("runbook_rag returned citations for OOM, CrashLoop and 5xx; strict inputs verified")


async def call_replacement(url, tls, token, target):
    transport = StreamableHttpTransport(url, auth=token, verify=tls)
    # Pin the already negotiated version and call directly without discovery or session affinity.
    async with Client(transport, timeout=25, mode=PROTOCOL_VERSION) as client:
        result = await call(client, "k8s_inspector", target)
        require(
            result["pod_uid"] == target["pod_uid"] and result["containers"],
            "Replacement failed to serve the stateless call",
        )
    print("A replacement MCP Pod served a direct tool call without a server session")


def mcp_pod_uid():
    pods = json.loads(
        kube(
            "aiops-core",
            "get",
            "pods",
            "-l",
            "app.kubernetes.io/name=mcp-server",
            "-o",
            "json",
        )
    )["items"]
    return next(
        pod["metadata"]["uid"] for pod in pods if "deletionTimestamp" not in pod["metadata"]
    )


def main():
    access = json.loads(kube("aiops-core", "get", "secret", "mcp-access", "-o", "json"))["data"]
    tls = ssl.create_default_context(cadata=base64.b64decode(access["ca.crt"]).decode())
    token = base64.b64decode(access["token"]).decode()
    target = oom_target()
    pod = pod_for("chaos-app")
    fresh_target = None
    if container_status(pod).get("restartCount") == 0:
        fresh_target = {
            "namespace": "target-apps",
            "pod_name": pod["metadata"]["name"],
            "pod_uid": pod["metadata"]["uid"],
        }
    with port_forward("aiops-core", "svc/mcp-server", 8000) as address:
        url = address.replace("http:", "https:") + "/mcp"
        asyncio.run(verify(url, tls, token, target, fresh_target))
    if "--replace-pod" in sys.argv[3:]:
        original = mcp_pod_uid()
        kube("aiops-core", "rollout", "restart", "deployment/mcp-server", capture=False)
        kube(
            "aiops-core",
            "rollout",
            "status",
            "deployment/mcp-server",
            "--timeout=150s",
            capture=False,
        )
        require(original != mcp_pod_uid(), "MCP Pod did not change")
        with port_forward("aiops-core", "svc/mcp-server", 8000) as address:
            asyncio.run(
                call_replacement(address.replace("http:", "https:") + "/mcp", tls, token, target)
            )


if __name__ == "__main__":
    main()
