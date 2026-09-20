/**
 * SSE 解析器单测（EchoDesk 前端）。
 *
 * 覆盖：标准分帧、跨 chunk 撕裂、\r\n 兼容、多行 data、event 字段、
 * 注释行忽略、流结束冲刷残留。
 */
import { describe, expect, it } from "vitest";
import { consumeSse, type SseEvent } from "../lib/sse";

/**
 * 将若干字符串分片编码为 ReadableStream（模拟网络字节流）。
 *
 * @param chunks - 字符串分片序列（可故意在事件中间截断）。
 * @returns 可读字节流。
 */
function streamFromChunks(chunks: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  return new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) {
        controller.enqueue(encoder.encode(chunk));
      }
      controller.close();
    },
  });
}

/**
 * 消费整条流并收集全部事件。
 *
 * @param chunks - 字符串分片序列。
 * @returns 解析出的事件列表。
 */
async function collect(chunks: string[]): Promise<SseEvent[]> {
  const events: SseEvent[] = [];
  await consumeSse(streamFromChunks(chunks), (event) => events.push(event));
  return events;
}

describe("consumeSse", () => {
  it("按空行分帧解析多个标准事件", async () => {
    const events = await collect(['data: {"a":1}\n\ndata: {"b":2}\n\n']);
    expect(events).toHaveLength(2);
    expect(events[0]!.event).toBeNull();
    expect(events[0]!.data).toBe('{"a":1}');
    expect(events[1]!.data).toBe('{"b":2}');
  });

  it("跨 chunk 撕裂的事件正确拼装", async () => {
    const events = await collect([
      'data: {"content":"你',
      '好"}\n\ndata: [DONE]\n\n',
    ]);
    expect(events).toHaveLength(2);
    expect(events[0]!.data).toBe('{"content":"你好"}');
    expect(events[1]!.data).toBe("[DONE]");
  });

  it("兼容 \\r\\n 换行与字段值前导空格", async () => {
    const events = await collect(["data: x\r\n\r\ndata:y\r\n\r\n"]);
    expect(events.map((e) => e.data)).toEqual(["x", "y"]);
  });

  it("多行 data 以换行拼接，event 字段透传", async () => {
    const events = await collect(["event: error\ndata: line1\ndata: line2\n\n"]);
    expect(events[0]!.event).toBe("error");
    expect(events[0]!.data).toBe("line1\nline2");
  });

  it("注释行忽略，连续空行不产生空事件", async () => {
    const events = await collect([": keep-alive\n\n\ndata: ok\n\n"]);
    expect(events).toHaveLength(1);
    expect(events[0]!.data).toBe("ok");
  });

  it("流结束时冲刷未以空行终止的残留事件", async () => {
    const events = await collect(["data: tail"]);
    expect(events).toHaveLength(1);
    expect(events[0]!.data).toBe("tail");
  });
});
