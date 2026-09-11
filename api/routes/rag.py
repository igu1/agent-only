from fastapi import APIRouter, Depends

from api.routes.shared.admin_auth import require_admin_token, require_org_token
from services.rag import build_knowledge, reindex_knowledge

router = APIRouter()


# require_admin_token FIRST: it is what resolves the org from the token, and
# require_org_token then refuses a call that named none. Reindexing writes to
# <qdrant_prefix>-<agent_id>, so "whose?" is not a detail here.
@router.post("/rag/reindex",
             dependencies=[Depends(require_admin_token), Depends(require_org_token)])
def reindex(agent_id: int | None = None) -> dict:
    knowledge = build_knowledge(agent_id=agent_id)
    reindex_knowledge(knowledge=knowledge, agent_id=agent_id)
    return {"ok": True, "agent_id": agent_id}
