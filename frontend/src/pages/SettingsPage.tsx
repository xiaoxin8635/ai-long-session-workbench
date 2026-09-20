/**
 * 设置页（EchoDesk 前端，M-F5）。
 *
 * 个人资料（auth store 只读展示，服务端暂无资料更新端点）+ 当前
 * workspace 信息 + 成员管理（ADMIN+：列表/按用户名添加；非 ADMIN
 * 展示 403 提示）。
 */
import { useEffect, useState, type JSX } from "react";
import { addMember, listMembers, listWorkspaces } from "../api/workspaces";
import type { MemberRole, WorkspaceMember, WorkspaceRead } from "../api/workspaces";
import { errorMessage, useAuthStore } from "../stores/auth";

/** 角色徽标文案与样式。 */
const ROLE_LABELS: Record<MemberRole, string> = {
  owner: "所有者",
  admin: "管理员",
  member: "成员",
};
const ROLE_STYLES: Record<MemberRole, string> = {
  owner: "bg-violet-100 text-violet-700 dark:bg-violet-950 dark:text-violet-400",
  admin: "bg-sky-100 text-sky-700 dark:bg-sky-950 dark:text-sky-400",
  member: "bg-slate-100 text-slate-500 dark:bg-slate-800 dark:text-slate-400",
};

/**
 * 设置页组件（数据自包含，无独立 store）。
 *
 * @returns 页面 JSX。
 */
export default function SettingsPage(): JSX.Element {
  const user = useAuthStore((s) => s.user);

  /** 当前 workspace。 */
  const [workspace, setWorkspace] = useState<WorkspaceRead | null>(null);
  /** 成员列表（null = 无权限或未加载）。 */
  const [members, setMembers] = useState<WorkspaceMember[] | null>(null);
  /** 成员区错误文案（403 无权限 / 404 用户不存在等）。 */
  const [memberError, setMemberError] = useState<string | null>(null);
  /** 新成员用户名草稿。 */
  const [usernameDraft, setUsernameDraft] = useState("");
  /** 新成员角色。 */
  const [roleDraft, setRoleDraft] = useState<Exclude<MemberRole, "owner">>("member");
  /** 提交中标记。 */
  const [adding, setAdding] = useState(false);

  // 首挂载：取 workspace + 成员列表（非 ADMIN 时 listMembers 403 → 提示文案）
  useEffect(() => {
    let cancelled = false;
    listWorkspaces()
      .then(async (workspaces) => {
        const ws = workspaces[0];
        if (ws === undefined || cancelled) {
          return;
        }
        setWorkspace(ws);
        try {
          const rows = await listMembers(ws.id);
          if (!cancelled) {
            setMembers(rows);
          }
        } catch (err) {
          if (!cancelled) {
            setMemberError(errorMessage(err));
          }
        }
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setMemberError(errorMessage(err));
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  /**
   * 提交添加成员（成功后刷新列表并清空表单）。
   */
  async function submitAdd(): Promise<void> {
    if (workspace === null || usernameDraft.trim() === "") {
      return;
    }
    setAdding(true);
    setMemberError(null);
    try {
      await addMember(workspace.id, usernameDraft.trim(), roleDraft);
      setMembers(await listMembers(workspace.id));
      setUsernameDraft("");
    } catch (err) {
      setMemberError(errorMessage(err));
    } finally {
      setAdding(false);
    }
  }

  return (
    <div className="mx-auto max-w-3xl space-y-5 p-6">
      <h1 className="text-lg font-semibold">设置</h1>

      {/* 个人资料（服务端暂无更新端点，只读展示） */}
      <section className="rounded-xl border border-slate-200 p-4 dark:border-slate-800">
        <h2 className="text-sm font-semibold">个人资料</h2>
        <dl className="mt-3 grid grid-cols-1 gap-x-6 gap-y-2 text-xs sm:grid-cols-2">
          <ProfileItem label="用户名" value={user?.username ?? "-"} />
          <ProfileItem label="显示名" value={user?.display_name ?? "-"} />
          <ProfileItem label="邮箱" value={user?.email ?? "-"} />
          <ProfileItem label="注册时间" value={user ? new Date(user.created_at).toLocaleString() : "-"} />
          <ProfileItem label="用户 ID" value={user?.id ?? "-"} mono />
        </dl>
      </section>

      {/* workspace 信息 */}
      <section className="rounded-xl border border-slate-200 p-4 dark:border-slate-800">
        <h2 className="text-sm font-semibold">工作区</h2>
        {workspace === null ? (
          <p className="mt-2 text-xs text-slate-400">加载中…</p>
        ) : (
          <dl className="mt-3 grid grid-cols-1 gap-x-6 gap-y-2 text-xs sm:grid-cols-2">
            <ProfileItem label="名称" value={workspace.name} />
            <ProfileItem label="创建时间" value={new Date(workspace.created_at).toLocaleString()} />
            <ProfileItem label="Workspace ID" value={workspace.id} mono />
          </dl>
        )}
      </section>

      {/* 成员管理（ADMIN+） */}
      <section className="rounded-xl border border-slate-200 p-4 dark:border-slate-800">
        <h2 className="text-sm font-semibold">成员管理</h2>
        {members !== null && (
          <ul className="mt-3 space-y-1.5">
            {members.map((m) => (
              <li
                key={m.user_id}
                className="flex items-center gap-2 rounded-lg bg-slate-50 px-3 py-2 text-xs dark:bg-slate-900"
              >
                <span className={"rounded px-1.5 py-0.5 text-[10px] " + ROLE_STYLES[m.role]}>
                  {ROLE_LABELS[m.role]}
                </span>
                <span className="font-medium">{m.username}</span>
                {m.display_name !== null && m.display_name !== "" && (
                  <span className="text-slate-400">{m.display_name}</span>
                )}
                <span className="ml-auto text-slate-400">
                  {new Date(m.created_at).toLocaleDateString()} 加入
                </span>
              </li>
            ))}
          </ul>
        )}
        {members === null && memberError !== null && (
          <p className="mt-2 text-xs text-slate-400">{memberError}（成员管理需要 ADMIN 及以上角色）</p>
        )}

        {/* 添加成员表单（ADMIN+ 时展示；非 ADMIN 提交会收到服务端 403 文案） */}
        <div className="mt-4 flex items-center gap-2">
          <input
            value={usernameDraft}
            onChange={(e) => setUsernameDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.nativeEvent.isComposing) {
                void submitAdd();
              }
            }}
            placeholder="按用户名添加成员"
            className="flex-1 rounded-lg border border-slate-300 px-2 py-1.5 text-xs dark:border-slate-700 dark:bg-slate-900"
          />
          <select
            value={roleDraft}
            onChange={(e) => setRoleDraft(e.target.value as Exclude<MemberRole, "owner">)}
            className="rounded-lg border border-slate-300 px-2 py-1.5 text-xs dark:border-slate-700 dark:bg-slate-900"
            aria-label="成员角色"
          >
            <option value="member">成员</option>
            <option value="admin">管理员</option>
          </select>
          <button
            type="button"
            disabled={adding || usernameDraft.trim() === ""}
            onClick={() => void submitAdd()}
            className="rounded-lg bg-sky-600 px-3 py-1.5 text-xs font-medium text-white transition hover:bg-sky-700 disabled:opacity-40"
          >
            添加
          </button>
        </div>
        {members !== null && memberError !== null && (
          <p role="alert" className="mt-2 text-xs text-red-600 dark:text-red-400">
            {memberError}
          </p>
        )}
      </section>
    </div>
  );
}

/**
 * 资料键值对条目。
 *
 * @param props - label 键名；value 值；mono 是否等宽字体展示。
 * @returns 条目 JSX。
 */
function ProfileItem({ label, value, mono = false }: { label: string; value: string; mono?: boolean }): JSX.Element {
  return (
    <div className="flex items-center gap-2">
      <dt className="shrink-0 text-slate-400">{label}</dt>
      <dd className={"min-w-0 truncate " + (mono ? "font-mono text-[11px]" : "")} title={value}>
        {value}
      </dd>
    </div>
  );
}
