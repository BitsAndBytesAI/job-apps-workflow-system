from fastapi import APIRouter


router = APIRouter()


@router.get("/")
def setup() -> dict[str, str]:
    return {"status": "ok", "page": "setup"}
