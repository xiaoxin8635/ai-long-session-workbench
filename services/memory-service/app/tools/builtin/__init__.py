"""内置工具包（M-09 首批）：todo / doc_reader / web_fetch。

导入本包即触发三个子模块向 default_registry 注册（app/api 路由导入时完成）。
"""

from app.tools.builtin import doc_reader, todo, web_fetch

__all__ = ["doc_reader", "todo", "web_fetch"]
