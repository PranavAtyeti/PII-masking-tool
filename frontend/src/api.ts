// Typed client for the backend built in sessions 1-2. In dev, calls go to
// relative "/api/..." paths -- vite.config.ts proxies those to the FastAPI
// server, so the browser sees same-origin requests and CORS never enters
// the picture locally. A production build would need this to point at
// wherever the API actually lives instead.

import type { Chat, Message, UploadPreviewResult, UploadResult } from "./types";
import { authHeaders } from "./auth";

const BASE = "/api";

async function authedFetch(input: RequestInfo | URL, init: RequestInit = {}): Promise<Response> {
  const auth = await authHeaders();
  return fetch(input, {
    ...init,
    headers: {
      ...auth,
      ...(init.headers ?? {}),
    },
  });
}

async function jsonOrThrow<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail ?? detail;
    } catch {
      // response wasn't JSON -- fall back to statusText, already set above
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
  getCurrentUser(): Promise<{ sub: string; scope: string; permissions: string[]; azp?: string }> {
    return authedFetch(`${BASE}/auth/me`).then((r) =>
      jsonOrThrow<{ sub: string; scope: string; permissions: string[]; azp?: string }>(r)
    );
  },

  listChats(): Promise<Chat[]> {
    return authedFetch(`${BASE}/chats`).then((r) => jsonOrThrow<Chat[]>(r));
  },

  createChat(title = "New chat"): Promise<Chat> {
    return authedFetch(`${BASE}/chats`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title }),
    }).then((r) => jsonOrThrow<Chat>(r));
  },

  getMessages(chatId: string): Promise<Message[]> {
    return authedFetch(`${BASE}/chats/${chatId}/messages`).then((r) => jsonOrThrow<Message[]>(r));
  },

  renameChat(chatId: string, title: string): Promise<Chat> {
    return authedFetch(`${BASE}/chats/${chatId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title }),
    }).then((r) => jsonOrThrow<Chat>(r));
  },

  async deleteChat(chatId: string): Promise<void> {
    const res = await authedFetch(`${BASE}/chats/${chatId}`, { method: "DELETE" });
    if (!res.ok && res.status !== 204) {
      throw new ApiError(res.status, res.statusText);
    }
  },

  async exportChat(chatId: string): Promise<void> {
    const res = await authedFetch(`${BASE}/chats/${chatId}/export`);
    if (!res.ok) throw new ApiError(res.status, res.statusText);
    const blob = await res.blob();
    // filename comes from the server's Content-Disposition header; browsers
    // don't expose an easy parsed form of it, so re-derive a reasonable one
    // rather than parsing that header by hand.
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

  async getFileInfo(chatId: string): Promise<{ filename: string; row_count: number; truncated: boolean; kept_private_count: number } | null> {
    const res = await authedFetch(`${BASE}/upload/${chatId}`);
    if (res.status === 404) return null; // no file uploaded for this chat -- not an error
    return jsonOrThrow(res);
  },

  previewFile(chatId: string, file: File): Promise<UploadPreviewResult> {
    const form = new FormData();
    form.append("file", file);
    return authedFetch(`${BASE}/upload/${chatId}/preview`, {
      method: "POST",
      body: form,
    }).then((r) => jsonOrThrow<UploadPreviewResult>(r));
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
    return authedFetch(`${BASE}/upload/${chatId}`, { method: "POST", body: form }).then((r) =>
      jsonOrThrow<UploadResult>(r)
    );
  },

  /**
   * Streams an answer via Server-Sent Events. Can't use the browser's
   * EventSource here -- it only supports GET requests with no body, and
   * this endpoint needs a POST with a JSON question. So this hand-parses
   * the fetch() response body instead.
   *
   * SSE framing: events are separated by a blank line ("\n\n"); each event
   * is one or more "data: ..." lines. This backend only ever sends a single
   * "data:" line per event (see routers/messages.py's _sse()), so this
   * parser doesn't need to handle multi-line data blocks -- but chunk
   * boundaries from the network don't respect event boundaries at all, so
   * a "\n\n"-terminated event can still arrive split across two chunks (or
   * two events can arrive in one chunk). Buffering on "\n\n" handles both.
   */
  async streamMessage(
    chatId: string,
    body: { question: string; use_ner: boolean; ner_confidence: number; concise: boolean },
    handlers: {
      onDelta: (text: string) => void;
      onDone: (maskedCount: number) => void;
      onError: (message: string) => void;
    }
  ): Promise<void> {
    const res = await authedFetch(`${BASE}/chats/${chatId}/messages`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });

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
    // flush any trailing event that arrived without a final blank-line
    // separator (e.g. connection closed right after the last event)
    if (buffer.trim()) {
      handleEvent(buffer, handlers);
    }
  },
};

function handleEvent(
  rawEvent: string,
  handlers: { onDelta: (text: string) => void; onDone: (maskedCount: number) => void; onError: (message: string) => void }
) {
  const dataLine = rawEvent.split("\n").find((line) => line.startsWith("data:"));
  if (!dataLine) return;
  const payload = dataLine.slice("data:".length).trim();
  if (!payload) return;

  let obj: { delta?: string; done?: boolean; masked_count?: number; error?: string };
  try {
    obj = JSON.parse(payload);
  } catch {
    return; // malformed event -- skip rather than crash the whole stream
  }

  if (typeof obj.delta === "string") {
    handlers.onDelta(obj.delta);
  } else if (obj.done) {
    handlers.onDone(obj.masked_count ?? 0);
  } else if (obj.error) {
    handlers.onError(obj.error);
  }
}
