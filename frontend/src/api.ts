import type { AdminConfig, Chat, Message, UploadResult, ColumnInfo } from "./types";

const BASE = "/api";

async function jsonOrThrow<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail ?? detail;
    } catch {
      // response wasn't JSON -- fall back to statusText
    }
    throw new ApiError(res.status, detail);
  }
  return res.json();
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

export const api = {
  getAdminConfig(): Promise<AdminConfig> {
    return fetch(`${BASE}/admin/config`).then((r) => jsonOrThrow<AdminConfig>(r));
  },

  updateAdminConfig(body: { api_key?: string; model?: string }): Promise<AdminConfig> {
    return fetch(`${BASE}/admin/config`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then((r) => jsonOrThrow<AdminConfig>(r));
  },

  listChats(): Promise<Chat[]> {
    return fetch(`${BASE}/chats`).then((r) => jsonOrThrow<Chat[]>(r));
  },

  createChat(title = "New chat"): Promise<Chat> {
    return fetch(`${BASE}/chats`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title }),
    }).then((r) => jsonOrThrow<Chat>(r));
  },

  getMessages(chatId: string): Promise<Message[]> {
    return fetch(`${BASE}/chats/${chatId}/messages`).then((r) => jsonOrThrow<Message[]>(r));
  },

  renameChat(chatId: string, title: string): Promise<Chat> {
    return fetch(`${BASE}/chats/${chatId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title }),
    }).then((r) => jsonOrThrow<Chat>(r));
  },

  async deleteChat(chatId: string): Promise<void> {
    const res = await fetch(`${BASE}/chats/${chatId}`, { method: "DELETE" });
    if (!res.ok && res.status !== 204) throw new ApiError(res.status, res.statusText);
  },

  async exportChat(chatId: string): Promise<void> {
    const res = await fetch(`${BASE}/chats/${chatId}/export`);
    if (!res.ok) throw new ApiError(res.status, res.statusText);
    const blob = await res.blob();
    const disposition = res.headers.get("Content-Disposition") ?? "";
    const match = disposition.match(/filename="([^"]+)"/);
    const filename = match?.[1] ?? "chat.txt";
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
  },

  async getFileInfo(chatId: string): Promise<{
    filename: string;
    row_count: number;
    truncated: boolean;
    kept_private_count: number;
  } | null> {
    const res = await fetch(`${BASE}/upload/${chatId}`);
    if (res.status === 404) return null;
    return jsonOrThrow(res);
  },

  async previewFile(chatId: string, file: File): Promise<{
    row_count: number;
    truncated: boolean;
    columns: ColumnInfo[];
  }> {
    const form = new FormData();
    form.append("file", file);
    const res = await fetch(`${BASE}/upload/${chatId}/preview`, {
      method: "POST",
      body: form,
    });
    return jsonOrThrow(res);
  },

  uploadFile(
    chatId: string,
    file: File,
    opts: { useNer: boolean; nerConfidence: number; disabledColumns?: string[] }
  ): Promise<UploadResult> {
    const form = new FormData();
    form.append("file", file);
    form.append("use_ner", String(opts.useNer));
    form.append("ner_confidence", String(opts.nerConfidence));
    form.append("disabled_columns", (opts.disabledColumns ?? []).join(","));
    return fetch(`${BASE}/upload/${chatId}`, { method: "POST", body: form }).then((r) =>
      jsonOrThrow<UploadResult>(r)
    );
  },

  async removeFile(chatId: string): Promise<void> {
    const res = await fetch(`${BASE}/upload/${chatId}`, { method: "DELETE" });
    if (!res.ok && res.status !== 204) throw new ApiError(res.status, res.statusText);
  },

  async streamMessage(
    chatId: string,
    body: { question: string; use_ner: boolean; ner_confidence: number; concise: boolean },
    handlers: {
      onDelta: (text: string) => void;
      onDone: (maskedCount: number) => void;
      onError: (message: string) => void;
      onAbort?: () => void;
    },
    signal?: AbortSignal
  ): Promise<void> {
    let res: Response;

    try {
      res = await fetch(`${BASE}/chats/${chatId}/messages`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        signal,
      });
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") {
        handlers.onAbort?.();
        return;
      }
      handlers.onError(error instanceof Error ? error.message : "Couldn't start the response stream.");
      return;
    }

    if (!res.ok || !res.body) {
      let detail = res.statusText;
      try {
        const errBody = await res.json();
        detail = errBody.detail ?? detail;
      } catch {
        // not JSON, use statusText
      }
      handlers.onError(detail);
      return;
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        let boundary = buffer.indexOf("\n\n");

        while (boundary !== -1) {
          const rawEvent = buffer.slice(0, boundary);
          buffer = buffer.slice(boundary + 2);
          handleEvent(rawEvent, handlers);
          boundary = buffer.indexOf("\n\n");
        }
      }

      buffer += decoder.decode();
      if (buffer.trim()) handleEvent(buffer, handlers);
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") {
        handlers.onAbort?.();
        return;
      }
      handlers.onError(error instanceof Error ? error.message : "The response stream ended unexpectedly.");
    } finally {
      reader.releaseLock();
    }
  },
};

function handleEvent(
  rawEvent: string,
  handlers: {
    onDelta: (text: string) => void;
    onDone: (maskedCount: number) => void;
    onError: (message: string) => void;
    onAbort?: () => void;
  }
) {
  const dataLine = rawEvent.split("\n").find((line) => line.startsWith("data:"));
  if (!dataLine) return;

  const payload = dataLine.slice("data:".length).trim();
  if (!payload) return;

  let obj: { delta?: string; done?: boolean; masked_count?: number; error?: string };
  try {
    obj = JSON.parse(payload);
  } catch {
    return;
  }

  if (typeof obj.delta === "string") {
    handlers.onDelta(obj.delta);
  } else if (obj.done) {
    handlers.onDone(obj.masked_count ?? 0);
  } else if (obj.error) {
    handlers.onError(obj.error);
  }
}
