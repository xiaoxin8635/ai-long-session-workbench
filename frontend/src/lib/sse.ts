/**
 * 通用 SSE（Server-Sent Events）流解析器（EchoDesk 前端）。
 *
 * 聊天接口是 POST + SSE（EventSource 不支持 POST），因此用 fetch 拿到
 * ReadableStream 后自行按 SSE 规范分帧：事件以空行分隔，字段 `data:`（多行
 * data 以 "\n" 拼接）。本解析器与具体业务无关，供 api/chat 等复用。
 */

/** 解析出的单个 SSE 事件。 */
export interface SseEvent {
  /** 事件名（服务端未设置时为 null）。 */
  event: string | null;
  /** 数据负载（多行 data 已拼接）。 */
  data: string;
}

/**
 * 消费 SSE 字节流，逐事件回调。
 *
 * @param body - fetch 响应的可读字节流。
 * @param onEvent - 每解析出一个完整事件调用一次。
 * @returns 流正常结束时 resolve；读流出错时 reject。
 */
export async function consumeSse(
  body: ReadableStream<Uint8Array>,
  onEvent: (event: SseEvent) => void
): Promise<void> {
  const reader = body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";
  // 当前事件的多行 data 缓冲与事件名
  let dataLines: string[] = [];
  let eventName: string | null = null;

  /**
   * 冲刷当前事件缓冲（遇空行/流结束时调用）。
   */
  function flushEvent(): void {
    if (dataLines.length === 0 && eventName === null) {
      return; // 空事件（如连续空行）跳过
    }
    onEvent({ event: eventName, data: dataLines.join("\n") });
    dataLines = [];
    eventName = null;
  }

  /**
   * 处理单行：兼容 \r\n、空行触发冲刷、注释行忽略、字段解析。
   *
   * @param rawLine - 不含换行符的原始行。
   */
  function handleLine(rawLine: string): void {
    let line = rawLine;
    if (line.endsWith("\r")) {
      line = line.slice(0, -1);
    }
    if (line === "") {
      flushEvent();
    } else if (line.startsWith(":")) {
      // 注释行，忽略
    } else if (line.startsWith("data:")) {
      dataLines.push(stripField(line, "data:"));
    } else if (line.startsWith("event:")) {
      eventName = stripField(line, "event:");
    }
  }

  for (;;) {
    const { done, value } = await reader.read();
    if (done) {
      break;
    }
    buffer += decoder.decode(value, { stream: true });
    let newlineIdx = buffer.indexOf("\n");
    while (newlineIdx !== -1) {
      const line = buffer.slice(0, newlineIdx);
      buffer = buffer.slice(newlineIdx + 1);
      handleLine(line);
      newlineIdx = buffer.indexOf("\n");
    }
  }
  // 流结束：先处理残留的未换行尾行，再冲刷未终止的事件（服务端异常截断时尽力交付）
  handleLine(buffer);
  buffer = "";
  flushEvent();
}

/**
 * 去除 SSE 字段名前缀及其后的单个空格。
 *
 * @param line - 原始行。
 * @param prefix - 字段前缀（"data:" / "event:"）。
 * @returns 字段值。
 */
function stripField(line: string, prefix: string): string {
  const value = line.slice(prefix.length);
  return value.startsWith(" ") ? value.slice(1) : value;
}
