import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, model_validator

from pulse.api.dependencies import require_role
from pulse.models import Membership, MembershipRole
from pulse.repositories.clickhouse import get_client as get_clickhouse_client
from pulse.services import deletion as deletion_service
from pulse.services import projects as projects_service

router = APIRouter(prefix="/api/v1/orgs/{org_id}/projects/{project_id}/subjects", tags=["deletion"])


class DeleteSubjectRequest(BaseModel):
    user_id: str | None = Field(default=None, max_length=200)
    anonymous_id: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def _require_an_identifier(self) -> "DeleteSubjectRequest":
        if not self.user_id and not self.anonymous_id:
            raise ValueError("provide user_id and/or anonymous_id")
        return self


class ArchiveErasure(BaseModel):
    entries_removed: int
    objects_rewritten: int
    objects_deleted: int
    # Archive objects that could not be read or parsed and so were NOT cleaned.
    # Non-zero means the deletion is incomplete for those objects.
    unreadable_objects: int


class DeleteSubjectResponse(BaseModel):
    rollup_buckets_recomputed: int
    rollup_verified: bool
    archive: ArchiveErasure


@router.post("/delete", response_model=DeleteSubjectResponse)
async def delete_subject(
    project_id: uuid.UUID,
    body: DeleteSubjectRequest,
    # The most destructive action in the app -- an irreversible cross-partition
    # data deletion -- so it gets the highest bar this app has, Owner only,
    # above every other config-changing action (Admin+) in this phase.
    membership: Membership = Depends(require_role(MembershipRole.OWNER)),
) -> DeleteSubjectResponse:
    if await projects_service.get_project(membership.org_id, project_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    clickhouse_client = await get_clickhouse_client()
    report = await deletion_service.delete_subject(
        clickhouse_client,
        membership.org_id,
        project_id,
        membership.user_id,
        user_id=body.user_id,
        anonymous_id=body.anonymous_id,
    )
    return DeleteSubjectResponse(
        rollup_buckets_recomputed=report.rollup_buckets_recomputed,
        rollup_verified=report.rollup_verified,
        archive=ArchiveErasure(
            entries_removed=report.archive.entries_removed,
            objects_rewritten=report.archive.objects_rewritten,
            objects_deleted=report.archive.objects_deleted,
            unreadable_objects=report.archive.unreadable,
        ),
    )
