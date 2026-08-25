import { useEffect, useState } from "react";
import { useAuth0 } from "@auth0/auth0-react";
import { setAccessTokenGetter } from "./auth";
import { Sidebar } from "./components/Sidebar";
import { ChatPane } from "./components/ChatPane";
import type { Chat, ColumnInfo, Message } from "./types";
import { api } from "./api";
import { SUGGESTION_CHIPS } from "./constants";

export default function App() {
  const { isLoading: authLoading, isAuthenticated, loginWithRedirect, getAccessTokenSilently } = useAuth0();

  useEffect(() => {
    if (isAuthenticated) {
      setAccessTokenGetter(() => getAccessTokenSilently());
    } else {
      setAccessTokenGetter(null);
    }
    return () => setAccessTokenGetter(null);
  }, [isAuthenticated, getAccessTokenSilently]);
  const [chats, setChats] = useState<Chat[]>([]);
  const [activeChatId, setActiveChatId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [filePill, setFilePill] = useState<string | undefined>(undefined);
  const [collapsed, setCollapsed] = useState(false);
  const [isStreaming, setIsStreaming] = useState(false);
  const [isUploading, setIsUploading] = useState(false);
  const [pendingFile, setPendingFile] = useState<File | null>(null);
  const [pendingColumns, setPendingColumns] = useState<ColumnInfo[]>([]);
  const [pendingRowCount, setPendingRowCount] = useState(0);
  const [selectedColumns, setSelectedColumns] = useState<string[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [backendAuthReady, setBackendAuthReady] = useState(false);

  useEffect(() => {
    if (!isAuthenticated) {
      setBackendAuthReady(false);
      return;
    }

    let cancelled = false;
    (async () => {
      try {
        await api.getCurrentUser();
        if (!cancelled) setBackendAuthReady(true);
      } catch (e) {
        if (!cancelled) {
          setBackendAuthReady(false);
          setLoadError(
            e instanceof Error ? e.message : "The backend could not validate your login."
          );
        }
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [isAuthenticated]);

  // On first load: use the most recent existing chat, or create one if
  // there isn't one yet -- same behavior the Streamlit version had.
  useEffect(() => {
    if (!backendAuthReady) return;

    (async () => {
      try {
        const existing = await api.listChats();
        if (existing.length > 0) {
          setChats(existing);
          await selectChat(existing[0].chat_id);
        } else {
          const created = await api.createChat();
          setChats([created]);
          setActiveChatId(created.chat_id);
        }
      } catch (e) {
        setLoadError(e instanceof Error ? e.message : "Couldn't reach the backend.");
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [backendAuthReady]);

  async function selectChat(chatId: string) {
    setActiveChatId(chatId);
    setFilePill(undefined);
    setPendingFile(null);
    setPendingColumns([]);
    setPendingRowCount(0);
    setSelectedColumns([]);
    try {
      const [msgs, fileInfo] = await Promise.all([
        api.getMessages(chatId),
        api.getFileInfo(chatId),
      ]);
      setMessages(msgs);
      if (fileInfo) {
        setFilePill(`${fileInfo.filename} · ${fileInfo.kept_private_count} kept private`);
      }
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : "Couldn't load that chat.");
    }
  }

  async function handleNewChat() {
    const created = await api.createChat();
    setChats((prev) => [created, ...prev]);
    setActiveChatId(created.chat_id);
    setMessages([]);
    setFilePill(undefined);
    setPendingFile(null);
    setPendingColumns([]);
    setPendingRowCount(0);
    setSelectedColumns([]);
  }

  async function handleSelectChat(chatId: string) {
    await selectChat(chatId);
  }

  async function handleRenameChat(chatId: string, title: string) {
    const updated = await api.renameChat(chatId, title);
    setChats((prev) => prev.map((c) => (c.chat_id === chatId ? updated : c)));
  }

  async function handleDeleteChat(chatId: string) {
    await api.deleteChat(chatId);
    const remaining = chats.filter((c) => c.chat_id !== chatId);
    setChats(remaining);
    if (activeChatId === chatId) {
      if (remaining.length > 0) {
        await selectChat(remaining[0].chat_id);
      } else {
        const created = await api.createChat();
        setChats([created]);
        setActiveChatId(created.chat_id);
        setMessages([]);
        setFilePill(undefined);
        setPendingFile(null);
        setPendingColumns([]);
        setPendingRowCount(0);
        setSelectedColumns([]);
      }
    }
  }

  async function handleExportChat(chatId: string) {
    await api.exportChat(chatId);
  }

  async function handleUploadFile(file: File) {
    if (!activeChatId) return;
    setIsUploading(true);
    try {
      const preview = await api.previewFile(activeChatId, file);
      setPendingFile(file);
      setPendingColumns(preview.columns);
      setPendingRowCount(preview.row_count);
      setSelectedColumns(preview.columns.filter((c) => c.type).map((c) => c.name));
    } catch (e) {
      const msg = e instanceof Error ? e.message : "Couldn't inspect that file.";
      window.alert(msg);
    } finally {
      setIsUploading(false);
    }
  }

  function handleCancelFile() {
    setPendingFile(null);
    setPendingColumns([]);
    setPendingRowCount(0);
    setSelectedColumns([]);
  }

  async function handleApplyFile() {
    if (!activeChatId || !pendingFile) return;

    setIsUploading(true);
    try {
      const selected = new Set(selectedColumns);
      const disabledColumns = pendingColumns
        .map((column) => column.name)
        .filter((name) => !selected.has(name));

      const result = await api.uploadFile(activeChatId, pendingFile, {
        useNer: true,
        nerConfidence: 0.6,
        disabledColumns,
      });

      setFilePill(`${result.filename} · ${result.kept_private_count} kept private`);
      handleCancelFile();
    } catch (e) {
      // A 422 here is the masking-integrity gate from routers/upload.py --
      // surface it as-is because nothing was saved when that gate fails.
      const msg = e instanceof Error ? e.message : "Masking failed.";
      window.alert(msg);
    } finally {
      setIsUploading(false);
    }
  }

  async function handleSend(text: string) {
    let chatId = activeChatId;
    if (!chatId) {
      const created = await api.createChat();
      setChats((prev) => [created, ...prev]);
      chatId = created.chat_id;
      setActiveChatId(chatId);
    }

    setMessages((prev) => [...prev, { role: "user", content: text, masked_count: 0 }]);
    setMessages((prev) => [...prev, { role: "assistant", content: "", masked_count: 0 }]);
    setIsStreaming(true);

    await api.streamMessage(
      chatId,
      { question: text, use_ner: true, ner_confidence: 0.6, concise: true },
      {
        onDelta: (piece) => {
          setMessages((prev) => {
            const next = [...prev];
            const last = next[next.length - 1];
            next[next.length - 1] = { ...last, content: last.content + piece };
            return next;
          });
        },
        onDone: (maskedCount) => {
          setMessages((prev) => {
            const next = [...prev];
            const last = next[next.length - 1];
            next[next.length - 1] = { ...last, masked_count: maskedCount };
            return next;
          });
          setIsStreaming(false);
          // The backend titles a new chat as part of the same request that
          // just finished (see routers/messages.py) -- refresh the list now
          // so the sidebar picks up the real title and the reordering by
          // recency, instead of staying on "New chat" until something else
          // happens to trigger a refetch.
          api.listChats().then(setChats).catch(() => {});
        },
        onError: (message) => {
          setMessages((prev) => {
            const next = [...prev];
            next[next.length - 1] = { role: "assistant", content: message, masked_count: 0 };
            return next;
          });
          setIsStreaming(false);
        },
      }
    );
  }

  if (authLoading) {
    return (
      <div className="flex h-screen w-screen items-center justify-center bg-bg text-ink">
        <div className="text-sm text-ink/60">Loading Privy…</div>
      </div>
    );
  }

  if (!isAuthenticated) {
    return (
      <div className="flex h-screen w-screen items-center justify-center bg-bg px-6">
        <div className="w-full max-w-md rounded-2xl border border-border bg-surface p-8 text-center shadow-sm">
          <div className="mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-2xl bg-bg text-2xl" aria-hidden>
            🔒
          </div>
          <h1 className="font-display text-2xl font-semibold">Welcome to Privy</h1>
          <p className="mt-2 text-sm leading-6 text-ink/60">
            Sign in to keep your chats and protected files associated with your account.
          </p>
          <button
            type="button"
            onClick={() => loginWithRedirect()}
            className="mt-6 w-full rounded-xl bg-ink px-4 py-3 text-sm font-medium text-white transition-opacity hover:opacity-90"
          >
            Sign in
          </button>
        </div>
      </div>
    );
  }

  if (loadError) {
    return (
      <div className="flex h-screen w-screen items-center justify-center p-6 text-center">
        <div>
          <p className="mb-2 text-lg font-semibold">Couldn't reach Privy's backend</p>
          <p className="text-sm text-ink/60">{loadError}</p>
          <p className="mt-4 text-sm text-ink/60">
            Make sure the FastAPI server is running (<code>uvicorn app.main:app --port 8000</code>)
            and reload this page.
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="flex h-screen w-screen overflow-hidden">
      <Sidebar
        chats={chats}
        activeChatId={activeChatId}
        collapsed={collapsed}
        onToggleCollapsed={() => setCollapsed((v) => !v)}
        onNewChat={handleNewChat}
        onSelectChat={handleSelectChat}
        onRenameChat={handleRenameChat}
        onDeleteChat={handleDeleteChat}
        onExportChat={handleExportChat}
      />
      <ChatPane
        messages={messages}
        suggestions={SUGGESTION_CHIPS}
        filePill={filePill}
        isStreaming={isStreaming}
        isUploading={isUploading}
        pendingFile={pendingFile}
        pendingColumns={pendingColumns}
        pendingRowCount={pendingRowCount}
        selectedColumns={selectedColumns}
        onSelectedColumnsChange={setSelectedColumns}
        onCancelFile={handleCancelFile}
        onApplyFile={handleApplyFile}
        onSend={handleSend}
        onUploadFile={handleUploadFile}
      />
    </div>
  );
}
