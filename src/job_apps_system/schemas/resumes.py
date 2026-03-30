from pydantic import BaseModel


class ResumeSchema(BaseModel):
    id: str
