from fastapi import APIRouter


router = APIRouter()


@router.get("/health")
def health() -> dict:
    return {"status": "ok"}


@router.post("/infra/clear-cache")
def clear_cache() -> dict:
    from infra.cache_utils import invalidate_ai_cache
    invalidate_ai_cache()
    return {"status": "cleared"}
