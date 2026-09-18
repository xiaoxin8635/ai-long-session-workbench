"""workspace 路由：列表 / 详情 / 成员管理（docs/06 §M-02）。

所有端点经由 deps.get_workspace / require_role 完成第二道防线校验；
成员管理（查看/添加）要求 ADMIN 及以上。
"""

import uuid

from fastapi import APIRouter, Depends, status

from app.core.deps import CurrentUser, CurrentWorkspace, DbDep, require_role
from app.core.errors import NotFoundError, PermissionDeniedError
from app.models.enums import MemberRole
from app.repositories import user_repo, workspace_repo
from app.schemas.workspace import MemberAdd, MemberRead, WorkspaceRead

router = APIRouter(prefix="/api/workspaces", tags=["workspaces"])

# ADMIN 门槛依赖（查看/添加成员共用）
_admin_member = Depends(require_role(MemberRole.ADMIN))


@router.get("", response_model=list[WorkspaceRead], summary="列出当前用户的 workspace")
async def list_my_workspaces(user: CurrentUser, db: DbDep) -> list[WorkspaceRead]:
    """返回当前用户所属的全部 workspace（前端切换器数据源）。"""
    return [
        WorkspaceRead.model_validate(ws) for ws in await workspace_repo.list_for_user(db, user.id)
    ]


@router.get("/{ws_id}", response_model=WorkspaceRead, summary="获取 workspace 详情")
async def get_workspace_detail(ws: CurrentWorkspace) -> WorkspaceRead:
    """成员可见（get_workspace 依赖已做成员校验）。"""
    return WorkspaceRead.model_validate(ws)


@router.get(
    "/{ws_id}/members",
    response_model=list[MemberRead],
    summary="列出 workspace 成员（ADMIN+）",
)
async def list_members(
    ws_id: uuid.UUID, db: DbDep, _admin: object = _admin_member
) -> list[MemberRead]:
    """列出全部成员（ADMIN 及以上可见）。

    Raises:
        PermissionDeniedError: 403 —— 操作者低于 ADMIN 或非成员。
    """
    rows = await workspace_repo.list_members(db, ws_id)
    return [
        MemberRead(
            workspace_id=member.workspace_id,
            user_id=user.id,
            username=user.username,
            display_name=user.display_name,
            role=member.role,
            created_at=member.created_at,
        )
        for member, user in rows
    ]


@router.post(
    "/{ws_id}/members",
    response_model=MemberRead,
    status_code=status.HTTP_201_CREATED,
    summary="添加/更新成员（ADMIN+）",
)
async def add_member(
    ws_id: uuid.UUID, payload: MemberAdd, db: DbDep, _admin: object = _admin_member
) -> MemberRead:
    """按用户名添加成员；已存在则更新角色。

    Raises:
        NotFoundError: 404 —— 目标用户名不存在。
        PermissionDeniedError: 403 —— 操作者低于 ADMIN，或试图指定 OWNER 角色。
    """
    if payload.role is MemberRole.OWNER:
        raise PermissionDeniedError("不能通过此接口添加 OWNER")
    target = await user_repo.get_by_username(db, payload.username)
    if target is None:
        raise NotFoundError("目标用户不存在")
    member = await workspace_repo.add_member(db, ws_id=ws_id, user_id=target.id, role=payload.role)
    return MemberRead(
        workspace_id=member.workspace_id,
        user_id=target.id,
        username=target.username,
        display_name=target.display_name,
        role=member.role,
        created_at=member.created_at,
    )
