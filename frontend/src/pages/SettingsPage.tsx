/**
 * 设置页（EchoDesk 前端 · 「宣纸书卷」古风，M-F5）。
 *
 * 个人资料（auth store 只读展示，服务端暂无资料更新端点）+ 当前
 * workspace 信息 + 成员管理（ADMIN+：列表/按用户名添加；非 ADMIN
 * 展示 403 提示）。区块以竹青书签竖条 + 宋体小标题分节，成员行纸面墨边。
 */
import { useEffect, useState, type JSX } from "react";
import { addMember, listMembers, listWorkspaces } from "../api/workspaces";
import type { MemberRole, WorkspaceMember, WorkspaceRead } from "../api/workspaces";
import { errorMessage, useAuthStore } from "../stores/auth";

/** 角色徽标文案与样式（古风 token 映射：所有者=紫棠 / 管理员=黛蓝 / 成员=淡墨）。 */
const ROLE_LABELS: Record<MemberRole, string> = {
  owner: "所有者",
  admin: "管理员",
  member: "成员",
};
const ROLE_STYLES: Record<MemberRole, string> = {
  owner: "bg-violet/12 text-violet ring-1 ring-inset ring-violet/30",
  admin: "bg-info/12 text-info ring-1 ring-inset ring-info/30",
  member: "bg-line/8 text-secondary ring-1 ring-inset ring-line/15",
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
    <div className="anim-rise-in mx-auto max-w-3xl space-y-5 p-6">
      <h1 className="flex items-center gap-2 font-display text-lg font-semibold text-primary">
        <span className="bookmark-bar h-4" aria-hidden />
        设置
      </h1>

      {/* 个人资料（服务端暂无更新端点，只读展示） */}
      <section className="rounded-xl border border-line/12 bg-surface/50 p-4 shadow-panel">
        <h2 className="flex items-center gap-2 font-display text-sm font-semibold text-primary">
          <span className="bookmark-bar h-3.5" aria-hidden />
          个人资料
        </h2>
        <dl className="mt-3 grid grid-cols-1 gap-x-6 gap-y-2 text-xs sm:grid-cols-2">
          <ProfileItem label="用户名" value={user?.username ?? "-"} />
          <ProfileItem label="显示名" value={user?.display_name ?? "-"} />
          <ProfileItem label="邮箱" value={user?.email ?? "-"} />
          <ProfileItem label="注册时间" value={user ? new Date(user.created_at).toLocaleString() : "-"} />
          <ProfileItem label="用户 ID" value={user?.id ?? "-"} mono />
        </dl>
      </section>

      {/* workspace 信息 */}
      <section className="rounded-xl border border-line/12 bg-surface/50 p-4 shadow-panel">
        <h2 className="flex items-center gap-2 font-display text-sm font-semibold text-primary">
          <span className="bookmark-bar h-3.5" aria-hidden />
          工作区
        </h2>
        {workspace === null ? (
          <p className="mt-2 font-display text-xs text-muted">加载中…</p>
        ) : (
          <dl className="mt-3 grid grid-cols-1 gap-x-6 gap-y-2 text-xs sm:grid-cols-2">
            <ProfileItem label="名称" value={workspace.name} />
            <ProfileItem label="创建时间" value={new Date(workspace.created_at).toLocaleString()} />
            <ProfileItem label="Workspace ID" value={workspace.id} mono />
          </dl>
        )}
      </section>

      {/* 成员管理（ADMIN+） */}
      <section className="rounded-xl border border-line/12 bg-surface/50 p-4 shadow-panel">
        <h2 className="flex items-center gap-2 font-display text-sm font-semibold text-primary">
          <span className="bookmark-bar h-3.5" aria-hidden />
          成员管理
        </h2>
        {members !== null && (
          <ul className="mt-3 space-y-1.5">
            {members.map((m, i) => (
              <li
                key={m.user_id}
                className="anim-rise-in flex items-center gap-2 rounded-lg border border-line/8 bg-elevated/60 px-3 py-2 text-xs"
                style={{ animationDelay: `${i * 40}ms` }}
              >
                <span className={"rounded px-1.5 py-0.5 font-display text-[10px] " + ROLE_STYLES[m.role]}>
                  {ROLE_LABELS[m.role]}
                </span>
                <span className="font-medium text-primary">{m.username}</span>
                {m.display_name !== null && m.display_name !== "" && (
                  <span className="text-muted">{m.display_name}</span>
                )}
                <span className="ml-auto text-muted">
                  {new Date(m.created_at).toLocaleDateString()} 加入
                </span>
              </li>
            ))}
          </ul>
        )}
        {members === null && memberError !== null && (
          <p className="mt-2 text-xs text-muted">{memberError}（成员管理需要 ADMIN 及以上角色）</p>
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
            className="input min-w-0 flex-1 rounded-lg px-2.5 py-1.5 text-xs"
          />
          <select
            value={roleDraft}
            onChange={(e) => setRoleDraft(e.target.value as Exclude<MemberRole, "owner">)}
            className="input rounded-lg px-2.5 py-1.5 text-xs"
            aria-label="成员角色"
          >
            <option value="member">成员</option>
            <option value="admin">管理员</option>
          </select>
          <button
            type="button"
            disabled={adding || usernameDraft.trim() === ""}
            onClick={() => void submitAdd()}
            className="btn-primary shrink-0 rounded-lg px-3 py-1.5 text-xs"
          >
            添加
          </button>
        </div>
        {members !== null && memberError !== null && (
          <p role="alert" className="mt-2 text-xs text-danger">
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
      <dt className="shrink-0 text-muted">{label}</dt>
      <dd
        className={"min-w-0 truncate text-secondary " + (mono ? "font-mono text-[11px]" : "")}
        title={value}
      >
        {value}
      </dd>
    </div>
  );
}
