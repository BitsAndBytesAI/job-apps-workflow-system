from pydantic import BaseModel


class JobSchema(BaseModel):
    id: str
