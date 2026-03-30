from pydantic import BaseModel


class ProviderModelConfig(BaseModel):
    provider: str
    model_id: str
