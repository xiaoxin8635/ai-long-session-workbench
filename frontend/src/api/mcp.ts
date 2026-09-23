/**
 * MCP 服务管理 API（EchoDesk 前端，M-09 扩展：热插拔）。
 *
 * 对接 memory-service `/api/mcp/servers`：清单（配置 + 运行态）、
 * 热添加（服务端试连成功才落库）、热移除（断连 + 注销工具）。
 * 全局共享模块：无 workspace 维度，登录即可管理。
 */
import { api } from "./client";

/** MCP server 配置 + 运行态条目（GET /api/mcp/servers）。 */
export interface McpServerInfo {
  /** 配置行 ID。 */
  id: string;
  /** 全局唯一标识（工具名前缀 mcp.<name>.*）。 */
  name: string;
  /** 传输方式。 */
  transport: "http" | "stdio";
  /** http 端点（stdio 为 null）。 */
  url: string | null;
  /** stdio 启动命令（http 为 null）。 */
  command: string | null;
  /** stdio 命令参数。 */
  args: string[];
  /** 是否启用。 */
  enabled: boolean;
  /** env=配置种子（不可删）/ user=API 添加。 */
  source: "env" | "user";
  /** 创建时间。 */
  created_at: string;
  /** 当前长连接是否存活。 */
  connected: boolean;
  /** 已注册工具名清单。 */
  tools: string[];
}

/** 热添加请求体（POST /api/mcp/servers）。 */
export interface McpServerCreatePayload {
  /** 全局唯一标识（小写字母/数字/中划线）。 */
  name: string;
  /** 传输方式。 */
  transport: "http" | "stdio";
  /** http 端点（transport=http 必填）。 */
  url?: string;
  /** stdio 启动命令（transport=stdio 必填）。 */
  command?: string;
  /** stdio 命令参数。 */
  args?: string[];
}

/**
 * 列出全部 MCP server（配置 + 运行态）。
 *
 * @returns server 清单。
 */
export async function listMcpServers(): Promise<McpServerInfo[]> {
  const { data } = await api.get<McpServerInfo[]>("/api/mcp/servers");
  return data;
}

/**
 * 热添加 MCP server（服务端试连成功后落库，工具即刻可用）。
 *
 * @param payload - server 配置。
 * @returns 落库后的 server 条目。
 */
export async function addMcpServer(payload: McpServerCreatePayload): Promise<McpServerInfo> {
  const { data } = await api.post<McpServerInfo>("/api/mcp/servers", payload);
  return data;
}

/**
 * 热移除 MCP server（断连 + 注销 mcp.<name>.* 工具 + 删配置）。
 *
 * @param name - server 标识。
 */
export async function removeMcpServer(name: string): Promise<void> {
  await api.delete(`/api/mcp/servers/${encodeURIComponent(name)}`);
}
