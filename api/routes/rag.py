from fastapi import APIRouter

from services.rag import build_knowledge, reindex_knowledge

router = APIRouter()


@router.post("/rag/reindex")
def reindex(agent_id: int | None = None) -> dict:
    knowledge = build_knowledge(agent_id=agent_id)
    reindex_knowledge(knowledge=knowledge, agent_id=agent_id)
    return {"ok": True, "agent_id": agent_id}
