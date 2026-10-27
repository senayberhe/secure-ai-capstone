"""LangGraph research workflow, with an explicitly labeled offline fallback.

Human approval happens in durable database state AFTER the graph returns; a model
cannot approve its own work. LangGraph execution is required when installed and
CAPSTONE_GRAPH=langgraph; no silent fallback after a LangGraph error.
"""
from typing import TypedDict
from .retrieval import retrieve, extract_report, check_citations


class ResearchState(TypedDict, total=False):
    question: str
    plan: list[str]
    evidence: list[dict]
    report: str
    citations: list[str]
    verified: bool


def build_functions(session, principal, group=None, writer=None):
    def plan(state: ResearchState):
        return {"plan": ["Search authorized chunks", "Build evidence extract",
                         "Verify source IDs", "Request independent review"]}

    def search(state: ResearchState):
        return {"evidence": retrieve(session, principal, state["question"], group=group)}

    def draft(state: ResearchState):
        report, citations = extract_report(state["question"], state.get("evidence", []))
        if writer is not None and citations:
            report = writer.draft(state["question"], state["evidence"])
        return {"report": report, "citations": citations}

    def verify(state: ResearchState):
        e = state.get("evidence", [])
        c = state.get("citations", [])
        return {"verified": check_citations(state.get("report", ""), c, e) if e else True}
    return plan, search, draft, verify


def research(session, principal, question: str, engine: str = "offline", group=None, writer=None) -> ResearchState:
    nodes = build_functions(session, principal, group, writer)
    initial: ResearchState = {"question": question}
    if engine == "offline":
        state = dict(initial)
        for node in nodes:
            state.update(node(state))
        return state
    if engine != "langgraph":
        raise ValueError("Unknown research engine")
    from langgraph.graph import StateGraph, START, END
    builder = StateGraph(ResearchState)
    for name, fn in zip(("plan", "retrieve", "draft", "verify"), nodes):
        builder.add_node(name, fn)
    builder.add_edge(START, "plan")
    builder.add_edge("plan", "retrieve")
    builder.add_edge("retrieve", "draft")
    builder.add_edge("draft", "verify")
    builder.add_edge("verify", END)
    return builder.compile().invoke(initial)


# -- Durable, interruptible research graph -------------------------------------------------------

def thread_config(tenant: str, job_id: str) -> dict:
    # Tenant-namespaced thread IDs: a lookup can never load another tenant's checkpoint.
    return {"configurable": {"thread_id": f"{tenant}:{job_id}"}}


class DurableState(ResearchState, total=False):
    tenant: str
    owner: str
    groups: list[str]
    group: str | None
    decision: str
    reviewed_by: str
    final_status: str


def build_durable_graph(session_factory, checkpointer, writer=None):
    """plan -> retrieve -> draft -> verify -> review (interrupt) -> finalize, checkpointed per thread.

    Nodes after the interrupt are pure (no external side effects), so replaying a resume after a
    crash is safe. Approval authority stays in the database; the review node re-checks it anyway.
    """
    from langgraph.graph import StateGraph, START, END
    from langgraph.types import interrupt
    from .security import Principal

    def principal(state):
        # Built only from server-written state (job record), never from client input.
        return Principal(state["tenant"], state["owner"], frozenset(state["groups"]), "reader")

    def with_session(node):
        def run(state):
            with session_factory() as session:
                plan, search, draft, verify = build_functions(session, principal(state), state.get("group"), writer)
                return {"plan": plan, "search": search, "draft": draft, "verify": verify}[node](state)
        return run

    def review(state):
        decision = interrupt({"report": state.get("report", ""), "citations": state.get("citations", [])})
        if (not isinstance(decision, dict) or not isinstance(decision.get("approved"), bool)
                or decision.get("tenant") != state["tenant"] or decision.get("reviewer") in (None, state["owner"])):
            raise PermissionError("Resume payload is not an independent same-tenant review")
        return {"decision": "approved" if decision["approved"] else "rejected", "reviewed_by": decision["reviewer"]}

    def finalize(state):
        return {"final_status": state["decision"]}

    builder = StateGraph(DurableState)
    for name in ("plan", "search", "draft", "verify"):
        builder.add_node(name, with_session(name))
    builder.add_node("review", review)
    builder.add_node("finalize", finalize)
    builder.add_edge(START, "plan")
    builder.add_edge("plan", "search")
    builder.add_edge("search", "draft")
    builder.add_edge("draft", "verify")
    builder.add_conditional_edges("verify", lambda s: "review" if s.get("verified") else END,
                                  {"review": "review", END: END})
    builder.add_edge("review", "finalize")
    builder.add_edge("finalize", END)
    return builder.compile(checkpointer=checkpointer)


def run_until_review(graph, job: dict) -> dict:
    """Run a job's thread to its review interrupt. Idempotent: an already-interrupted thread is reused."""
    config = thread_config(job["tenant"], job["id"])
    snapshot = graph.get_state(config)
    if snapshot.next != ("review",):
        graph.invoke({"question": job["question"], "tenant": job["tenant"], "owner": job["owner"],
                      "groups": sorted(job["groups"]), "group": job.get("group")}, config)
        snapshot = graph.get_state(config)
    return {**snapshot.values, "interrupted": snapshot.next == ("review",)}


def resume_review(graph, tenant: str, job_id: str, *, approved: bool, reviewer: str, reviewer_tenant: str) -> str | None:
    """Resume an interrupted thread with an already-authorized decision. No-op if already finished."""
    from langgraph.types import Command
    config = thread_config(tenant, job_id)
    if graph.get_state(config).next != ("review",):
        return graph.get_state(config).values.get("final_status")
    graph.invoke(Command(resume={"approved": approved, "reviewer": reviewer, "tenant": reviewer_tenant}), config)
    return graph.get_state(config).values.get("final_status")
