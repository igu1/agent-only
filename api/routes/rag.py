from fastapi import APIRouter, Depends

from api.routes.shared.admin_auth import require_admin_token
from services.rag import build_knowledge, reindex_knowledge

router = APIRouter()


@router.post("/rag/reindex", dependencies=[Depends(require_admin_token)])
def reindex(agent_id: int | None = None) -> dict:
    knowledge = build_knowledge(agent_id=agent_id)
    reindex_knowledge(knowledge=knowledge, agent_id=agent_id)
    return {"ok": True, "agent_id": agent_id}
