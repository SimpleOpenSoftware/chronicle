"""Provider-side staged enrollment; Chronicle authorizes the privacy decision."""

import asyncio

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, UploadFile
from pydantic import BaseModel, ConfigDict, Field, ValidationError

import simple_speaker_recognition.core.gallery_catalog as gallery_catalog
from simple_speaker_recognition.core.enrollment_operations import (
    Binding,
    EnrollmentOperations,
    OperationError,
)

from .enrollment import get_audio_backend, get_auth, get_db

router = APIRouter(prefix="/enrollment/operations")


async def get_operations(
    gallery=Depends(get_db), x_speaker_catalog: str | None = Header(None)
):
    return EnrollmentOperations(
        gallery, get_audio_backend(), get_auth().enrollment_audio_dir, x_speaker_catalog
    )


class QuarantineRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str = Field(min_length=1, max_length=64)


async def respond(operation):
    try:
        return await operation
    except OperationError as exc:
        raise HTTPException(exc.status, exc.reason) from None


@router.get("/catalog")
async def catalog(user_id: str | None = None, operations=Depends(get_operations)):

    try:
        return await asyncio.to_thread(gallery_catalog.catalog_snapshot, user_id)
    except OperationError as exc:
        raise HTTPException(exc.status, exc.reason) from None


@router.post("/{operation_id}/prepare")
async def prepare(
    operation_id: str,
    binding: str = Form(...),
    file: UploadFile = File(...),
    operations=Depends(get_operations),
):
    try:
        parsed = Binding.model_validate_json(binding)
    except ValidationError:
        # Pydantic's default detail can echo names or evidence supplied as input.
        raise HTTPException(422, "Invalid enrollment binding") from None
    content = await file.read(32 * 1024 * 1024 + 1)
    return await respond(operations.prepare(operation_id, parsed, content))


@router.post("/{operation_id}/activate")
async def activate(
    operation_id: str, binding: Binding, operations=Depends(get_operations)
):
    return await respond(operations.activate(operation_id, binding))


@router.post("/{operation_id}/quarantine")
async def quarantine(
    operation_id: str, body: QuarantineRequest, operations=Depends(get_operations)
):
    return await respond(operations.quarantine(operation_id, body.user_id))
