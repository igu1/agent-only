from pydantic import BaseModel


class ChatRequest(BaseModel):
    message: str
    debug: bool = False
    user_id: str = "default"


class ChatResponse(BaseModel):
    content: str


class ToolsResponse(BaseModel):
    tools: list[str]
