import { useEffect, useRef, useState } from "react";
import { useAuth0 } from "@auth0/auth0-react";
import { setAccessTokenGetter } from "./auth";
import { Sidebar } from "./components/Sidebar";
import { ChatPane } from "./components/ChatPane";
import { SettingsPanel } from "./components/SettingsPanel";
import type { Chat, ColumnInfo, CurrentUser, Message } from "./types";
import { api } from "./api";
import { SUGGESTION_CHIPS } from "./constants";

export default function App() {
  const {
    isLoading: authLoading,
    isAuthenticated,
    loginWithRedirect,
    logout,
    getAccessTokenSilently,
    user: auth0User,
  } = useAuth0();
  const [backendAuthReady, setBackendAuthReady] = useState(false);
  const [currentUser, setCurrentUser] = useState<CurrentUser | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);

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
  const [attachedFile, setAttachedFile] = useState<{
    file: File | null;
    filename: string;
    keptPrivateCount: number;
  } | null>(null);
  const [collapsed, setCollapsed] = useState(false);
  const [isStreaming, setIsStreaming] = useState(false);
  const [isUploading, setIsUploading] = useState(false);
  const [pendingFile, setPendingFile] = useState<File | null>(null);
  const [pendingColumns, setPendingColumns] = useState<ColumnInfo[]>([]);
  const [pendingRowCount, setPendingRowCount] = useState(0);
  const [selectedColumns, setSelectedColumns] = useState<string[]>([]);
  const [isEditingFile, setIsEditingFile] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const streamControllerRef = useRef<AbortController | null>(null);

  useEffect(() => {
    if (!isAuthenticated) {
      setBackendAuthReady(false);
      setCurrentUser(null);
      setSettingsOpen(false);
      return;
    }

    let cancelled = false;
    (async () => {
      try {
        const backendUser = await api.getCurrentUser();
        if (!cancelled) {
          // Auth0 access tokens for a custom API do not necessarily contain
          // profile claims such as email/name. The Auth0 React SDK already
          // has the authenticated user's ID-token profile, so use those
          // claims for the local UI while keeping the backend `sub` and role
          // authoritative for authorization.
          setCurrentUser({
            ...backendUser,
            email: backendUser.email || auth0User?.email || null,
            display_name:
              backendUser.display_name && !backendUser.display_name.startsWith('google-oauth2|')
                ? backendUser.display_name
                : auth0User?.name || auth0User?.nickname || auth0User?.email || backendUser.display_name,
          });
          setBackendAuthReady(true);
        }
      } catch (e) {
        if (!cancelled) {
          setBackendAuthReady(false);
          setLoadError(e instanceof Error ? e.message : "The backend could not validate your login.");
        }
      }
    })();
    return () => { cancelled = true; };
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
    setAttachedFile(null);
    setIsEditingFile(false);
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
        setAttachedFile({
          file: null,
          filename: fileInfo.filename,
          keptPrivateCount: fileInfo.kept_private_count,
        });
      } else {
        setAttachedFile(null);
      }
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : "Couldn't load that chat.");
    }
  }

  async function handleLogout() {
    streamControllerRef.current?.abort();
    setIsStreaming(false);
    setSettingsOpen(false);
    setCurrentUser(null);
    setLoadError(null);
    await logout({
      logoutParams: {
        returnTo: window.location.origin,
      },
    });
  }

  async function handleNewChat() {
    const created = await api.createChat();
    setChats((prev) => [created, ...prev]);
    setActiveChatId(created.chat_id);
    setMessages([]);
    setAttachedFile(null);
    setIsEditingFile(false);
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
        setAttachedFile(null);
        setIsEditingFile(false);
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
    setIsEditingFile(false);
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

  async function handleEditFile() {
    if (!activeChatId || !attachedFile) return;
    if (!attachedFile.file) {
      window.alert("For privacy, Privy does not keep the original file after a page refresh. Re-attach the original file to edit its masking settings.");
      return;
    }

    setIsUploading(true);
    setIsEditingFile(true);
    try {
      const preview = await api.previewFile(activeChatId, attachedFile.file);
      setPendingFile(attachedFile.file);
      setPendingColumns(preview.columns);
      setPendingRowCount(preview.row_count);
      // Start from the currently selected masking state where possible.
      setSelectedColumns(preview.columns.filter((c) => c.enabled).map((c) => c.name));
    } catch (e) {
      setIsEditingFile(false);
      const msg = e instanceof Error ? e.message : "Couldn't reopen masking settings.";
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
    setIsEditingFile(false);
  }

  async function handleRemoveFile() {
    if (!activeChatId || !attachedFile) return;
    if (!window.confirm(`Remove ${attachedFile.filename} from this chat?`)) return;

    setIsUploading(true);
    try {
      await api.removeFile(activeChatId);
      setAttachedFile(null);
      handleCancelFile();
    } catch (e) {
      const msg = e instanceof Error ? e.message : "Couldn't remove the file.";
      window.alert(msg);
    } finally {
      setIsUploading(false);
    }
  }

  async function handleApplyFile() {
    if (!activeChatId || !pendingFile || selectedColumns.length === 0) return;

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

      setAttachedFile({
        file: pendingFile,
        filename: result.filename,
        keptPrivateCount: result.kept_private_count,
      });
      handleCancelFile();
    } catch (e) {
      const msg = e instanceof Error ? e.message : "Masking failed.";
      window.alert(msg);
    } finally {
      setIsUploading(false);
    }
  }

  function handleStopGeneration() {
    streamControllerRef.current?.abort();
  }

  async function handleSend(text: string) {
    if (isStreaming) return;

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

    const controller = new AbortController();
    streamControllerRef.current = controller;

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
          streamControllerRef.current = null;
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
          streamControllerRef.current = null;
        },
        onAbort: () => {
          setIsStreaming(false);
          streamControllerRef.current = null;
        },
      },
      controller.signal
    );

    if (streamControllerRef.current === controller) {
      streamControllerRef.current = null;
    }
  }

  if (authLoading) {
    return <div className="flex h-screen w-screen items-center justify-center bg-bg text-ink"><div className="text-sm text-ink/60">Loading Privy…</div></div>;
  }

  if (!isAuthenticated) {
    return (
      <div className="flex h-screen w-screen items-center justify-center bg-bg px-6">
        <div className="w-full max-w-md rounded-2xl border border-border bg-surface p-8 text-center shadow-sm">
          <div className="mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-2xl bg-bg text-2xl" aria-hidden>🔒</div>
          <h1 className="font-display text-2xl font-semibold">Welcome to Privy</h1>
          <p className="mt-2 text-sm leading-6 text-ink/60">Sign in to keep your chats associated with your account.</p>
          <button type="button" onClick={() => loginWithRedirect()} className="mt-6 w-full rounded-xl bg-ink px-4 py-3 text-sm font-medium text-white hover:opacity-90">Sign in</button>
        </div>
      </div>
    );
  }

  if (!backendAuthReady) {
    return <div className="flex h-screen w-screen items-center justify-center bg-bg text-ink"><div className="text-sm text-ink/60">Signing you in…</div></div>;
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
        onOpenSettings={() => setSettingsOpen(true)}
        onLogout={handleLogout}
        currentUser={currentUser}
        isAdmin={currentUser?.role === "admin"}
      />
      <ChatPane
        messages={messages}
        suggestions={SUGGESTION_CHIPS}
        attachment={attachedFile ? {
          filename: attachedFile.filename,
          keptPrivateCount: attachedFile.keptPrivateCount,
          canEdit: Boolean(attachedFile.file),
        } : undefined}
        isStreaming={isStreaming}
        isUploading={isUploading}
        pendingFile={pendingFile}
        pendingColumns={pendingColumns}
        pendingRowCount={pendingRowCount}
        selectedColumns={selectedColumns}
        isEditingFile={isEditingFile}
        onSelectedColumnsChange={setSelectedColumns}
        onCancelFile={handleCancelFile}
        onApplyFile={handleApplyFile}
        onEditFile={handleEditFile}
        onRemoveFile={handleRemoveFile}
        onStop={handleStopGeneration}
        onSend={handleSend}
        onUploadFile={handleUploadFile}
      />
      {currentUser?.role === "admin" && (
        <SettingsPanel open={settingsOpen} onClose={() => setSettingsOpen(false)} />
      )}
    </div>
  );
}
