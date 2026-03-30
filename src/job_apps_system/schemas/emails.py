from pydantic import BaseModel


class EmailSchema(BaseModel):
    id: str
