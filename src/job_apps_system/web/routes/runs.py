from fastapi import APIRouter


router = APIRouter()


@router.get("/")
def list_runs() -> dict[str, list]:
    return {"runs": []}
