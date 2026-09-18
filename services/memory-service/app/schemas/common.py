"""通用 schema：分页容器（docs/06 §M-03 分页约定）。"""

from typing import Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    """分页响应容器。"""

    items: list[T]
    total: int
