from pydantic import BaseModel


class ContactSchema(BaseModel):
    id: str
