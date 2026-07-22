import os
from agno.knowledge.knowledge import Knowledge
from agno.vectordb.qdrant import Qdrant
from agno.vectordb.search import SearchType
from agno.knowledge.embedder.openai import OpenAIEmbedder
from agno.knowledge.embedder.google import GeminiEmbedder

from tools.getAi import get_active_faqs, get_active_knowledge_base, get_active_products


def _collection_name(agent_id: int | None) -> str:
    suffix = str(agent_id) if agent_id is not None else "default"
    return f"cronocrm-ai-profile-{suffix}"


def _build_embedder():
    provider = os.getenv("AGNO_EMBEDDER", "openai").strip().lower()
    if provider == "google":
        return GeminiEmbedder()
    return OpenAIEmbedder()


def build_knowledge(*, agent_id: int | None) -> Knowledge:
    qdrant_url = os.getenv("QDRANT_URL") or "http://localhost:6333"
    qdrant_api_key = os.getenv("QDRANT_API_KEY")

    vector_db = Qdrant(
        collection=_collection_name(agent_id),
        url=qdrant_url,
        api_key=qdrant_api_key,
        search_type=SearchType.hybrid,
        embedder=_build_embedder(),
    )

    return Knowledge(vector_db=vector_db)


def _clear_agent_docs(*, knowledge: Knowledge, agent_id: int | None) -> None:
    vector_db = knowledge.vector_db
    try:
        vector_db.delete_by_metadata({"agent_id": agent_id})
    except Exception:
        pass


def _insert_text(*, knowledge: Knowledge, name: str, text: str, metadata: dict) -> None:
    text_value = str(text or "").strip()
    if not text_value:
        return
    knowledge.insert(name=name, text_content=text_value, metadata=metadata)


def _index_faqs(*, knowledge: Knowledge, agent_id: int | None) -> None:
    faqs = get_active_faqs(agent_id=agent_id)
    for faq in faqs:
        q = (faq.get("question") or "").strip()
        a = (faq.get("answer") or "").strip()
        if not q or not a:
            continue
        _insert_text(
            knowledge=knowledge,
            name=f"faq:{faq.get('id')}",
            text=f"Question: {q}\nAnswer: {a}",
            metadata={"agent_id": agent_id, "type": "faq", "row_id": faq.get("id")},
        )


def _index_kb(*, knowledge: Knowledge, agent_id: int | None) -> None:
    kb_entries = get_active_knowledge_base(agent_id=agent_id)
    for kb in kb_entries:
        q = (kb.get("question") or "").strip()
        a = (kb.get("answer") or "").strip()
        if not q or not a:
            continue
        category = (kb.get("category") or "").strip()
        prefix = f"Category: {category}\n" if category else ""
        _insert_text(
            knowledge=knowledge,
            name=f"kb:{kb.get('id')}",
            text=f"{prefix}Question: {q}\nAnswer: {a}",
            metadata={"agent_id": agent_id, "type": "knowledge_base", "row_id": kb.get("id")},
        )


def _product_text(item: dict) -> str | None:
    product_name = (item.get("product_name") or "").strip()
    if not product_name:
        return None

    variant_name = (item.get("variant_name") or "").strip()
    title = product_name if not variant_name else f"{product_name} - {variant_name}"
    description = (item.get("product_description") or "").strip()

    price = item.get("variant_price") if item.get("variant_id") else item.get("product_price")
    discount = item.get("variant_discount") if item.get("variant_id") else item.get("product_discount")

    parts = [f"Product: {title}"]
    if description:
        parts.append(f"Description: {description}")
    if price is not None:
        parts.append(f"Price: {price}")
    if discount is not None:
        parts.append(f"Discount: {discount}")

    return "\n".join(parts)


def _index_products(*, knowledge: Knowledge, agent_id: int | None) -> None:
    products = get_active_products(agent_id=agent_id)
    for item in products:
        text = _product_text(item)
        if not text:
            continue
        _insert_text(
            knowledge=knowledge,
            name=f"product:{item.get('id')}",
            text=text,
            metadata={
                "agent_id": agent_id,
                "type": "product",
                "row_id": item.get("id"),
                "product_id": item.get("product_id"),
                "variant_id": item.get("variant_id"),
            },
        )


def reindex_knowledge(*, knowledge: Knowledge, agent_id: int | None) -> None:
    _clear_agent_docs(knowledge=knowledge, agent_id=agent_id)
    _index_faqs(knowledge=knowledge, agent_id=agent_id)
    _index_kb(knowledge=knowledge, agent_id=agent_id)
    _index_products(knowledge=knowledge, agent_id=agent_id)
