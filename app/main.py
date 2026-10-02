from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .engine import ValidationError, prune, review, revise
from .models import PruneRequest, ReviewRequest, ReviseRequest

app = FastAPI(
    title="Local Data Export Risk Review API",
    description=(
        "Reviews every output column of an export node for sensitive content "
        "provenance and sensitive row-level influence, can revise a blocked "
        "graph into a re-submittable, review-safe export, and can prune a graph "
        "down to a chosen subset of export columns."
    ),
    version="1.2.0",
)


@app.exception_handler(ValidationError)
async def _validation_error_handler(_request: Request, exc: ValidationError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"error": exc.detail()})


@app.exception_handler(Exception)
async def _unexpected_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    # Never leak stack traces from a local review service.
    return JSONResponse(
        status_code=500,
        content={"error": {"code": "INTERNAL", "message": str(exc)}},
    )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/review")
async def review_export(request: ReviewRequest) -> dict[str, object]:
    return review(request)


@app.post("/api/revise")
async def revise_export(request: ReviseRequest) -> dict[str, object]:
    return revise(request)


@app.post("/api/prune")
async def prune_export(request: PruneRequest) -> dict[str, object]:
    return prune(request)
