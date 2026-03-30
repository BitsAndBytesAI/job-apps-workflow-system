from fastapi import APIRouter


router = APIRouter()


@router.get("/")
def list_interviews() -> dict[str, list]:
    return {"interviews": []}
