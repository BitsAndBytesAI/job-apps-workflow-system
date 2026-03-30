from fastapi import APIRouter


router = APIRouter()


@router.get("/")
def list_jobs() -> dict[str, list]:
    return {"jobs": []}
