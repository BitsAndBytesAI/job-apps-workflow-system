from fastapi import APIRouter


router = APIRouter()


@router.get("/")
def dashboard() -> dict[str, str]:
    return {"status": "ok", "page": "dashboard"}
