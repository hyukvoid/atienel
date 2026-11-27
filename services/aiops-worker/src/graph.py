from typing import TypedDict
from uuid import UUID

from fastmcp import Client
from langgraph.graph import StateGraph

from .agent import Diagnosis, diagnose, report_from
from .job_repository import complete
from .patch_proposals import FIXTURES


class State(TypedDict, total=False):
    incident: dict
    job_id: UUID
    token: UUID
    pod: dict
    matches: list[dict]
    diagnosis: Diagnosis
    report: dict
    committed: bool


async def mcp_call(transport, tool, request):
    async with Client(transport, timeout=18) as client:
        result = await client.call_tool(tool, {"request": request})
    if not isinstance(result.structured_content, dict):
        raise ValueError("Missing MCP structured output")
    return result.structured_content


def build_graph(pool, transport, model):
    async def inspect(state: State):
        incident = state["incident"]
        request = {"namespace": incident["namespace"]}
        try:
            uid = str(UUID(incident["pod_uid"]))
        except ValueError:
            uid = None
        if uid:
            request.update(
                pod_name=incident["pod_name"],
                pod_uid=uid,
                container=incident["container_name"],
            )
        return {"pod": await mcp_call(transport, "k8s_inspector", request)}

    async def retrieve(state: State):
        pod = state["pod"]
        incident = state["incident"]
        terms = [incident["incident_type"], incident["signal"].get("summary", "")]
        for item in pod["containers"]:
            terms.extend(
                [
                    item.get("waiting_reason") or "",
                    (item.get("termination") or {}).get("reason") or "",
                ]
            )
        fixture = FIXTURES.get(incident["incident_type"])
        local_fixture = fixture and (pod.get("workload") or {}).get("name") == fixture[0]
        if local_fixture:
            terms.extend([fixture[0], fixture[2]])
        result = await mcp_call(
            transport,
            "runbook_rag",
            {"query": " ".join(terms)[:2000], "top_k": 10 if local_fixture else 5},
        )
        return {"matches": result["matches"]}

    async def generate(state: State):
        response = await diagnose(state["incident"], state["pod"], state["matches"], model)
        return {"diagnosis": response}

    async def validate(state: State):
        return {
            "report": report_from(
                state["incident"], state["pod"], state["matches"], state["diagnosis"]
            )
        }

    async def save(state: State):
        committed = await complete(pool, state["job_id"], state["token"], state["report"])
        return {"committed": committed}

    graph = StateGraph(State)
    graph.add_sequence([inspect, retrieve, generate, validate, save])
    graph.set_entry_point("inspect")
    graph.set_finish_point("save")
    return graph.compile()
