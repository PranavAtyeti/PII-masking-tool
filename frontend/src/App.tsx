import { useEffect, useRef, useState } from "react";
import {
  clearAccessToken,
  getAccessToken,
  setAccessToken,
  setAccessTokenGetter,
  setGuestSessionGetter,
  restoreSession,
} from "./auth";
import { Sidebar } from "./components/Sidebar";
import { ChatPane } from "./components/ChatPane";
import { SettingsPanel } from "./components/SettingsPanel";
import type {
  Chat,
  ChatFileInfo,
  ColumnInfo,
  CurrentUser,
  Message,
  ModelOption,
} from "./types";
import { api } from "./api";
import { AuthPanel } from "./components/AuthPanel";
import { SUGGESTION_CHIPS } from "./constants";
import type { ChatAttachment } from "./components/ChatInput";
import {
  MAX_FILE_SIZE_BYTES,
  MAX_FILE_SIZE_MB,
  MAX_FILES_PER_CHAT,
  MAX_FILES_PER_SELECTION,
} from "./uploadLimits";

interface LocalAttachment extends ChatAttachment {
  file: File | null;
  columns: ColumnInfo[];
}

export default function App() {
  const [authLoading, setAuthLoading] = useState(true);
  const [isAuthenticated, setIsAuthenticated] = useState(false);

  const [backendAuthReady, setBackendAuthReady] = useState(false);
  const [guestSessionId, setGuestSessionId] = useState<string | null>(
    () => sessionStorage.getItem("privy-guest-session")
  );
  const [currentUser, setCurrentUser] = useState<CurrentUser | null>(null);
  const [authPanelOpen, setAuthPanelOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [chats, setChats] = useState<Chat[]>([]);
  const [activeChatId, setActiveChatId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [attachments, setAttachments] = useState<LocalAttachment[]>([]);
  const [collapsed, setCollapsed] = useState(false);
  const [isStreaming, setIsStreaming] = useState(false);
  const [isUploading, setIsUploading] = useState(false);
  const [pendingFile, setPendingFile] = useState<File | null>(null);
  const [pendingColumns, setPendingColumns] = useState<ColumnInfo[]>([]);
  const [pendingRowCount, setPendingRowCount] = useState(0);
  const [selectedColumns, setSelectedColumns] = useState<string[]>([]);
  const [useNerForFile, setUseNerForFile] = useState(false);
  const [pendingFileId, setPendingFileId] = useState<string | null>(null);
  const [pendingFileQueue, setPendingFileQueue] = useState<File[]>([]);
  const [isEditingFile, setIsEditingFile] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [models, setModels] = useState<ModelOption[]>([]);
  const [selectedModelId, setSelectedModelId] = useState<string>(
    () => localStorage.getItem("privy-selected-model") || ""
  );

  const streamControllerRef = useRef<AbortController | null>(null);

  // Wire local JWT and guest-session credentials into the API layer.
  useEffect(() => {
    setAccessTokenGetter(() => getAccessToken() ?? "");
    setGuestSessionGetter(() => guestSessionId);

    return () => {
      setAccessTokenGetter(null);
      setGuestSessionGetter(null);
    };
  }, [guestSessionId]);

  // Restore a local login after a page refresh using the HttpOnly refresh cookie.
  useEffect(() => {
    let cancelled = false;

    (async () => {
      try {
        const session = await restoreSession();
        if (cancelled) return;

        if (session) {
          setAccessToken(session.access_token);
          setCurrentUser(session.user as CurrentUser);
          setIsAuthenticated(true);
          setLoadError(null);
        }
      } catch {
        if (!cancelled) {
          clearAccessToken();
          setIsAuthenticated(false);
        }
      } finally {
        if (!cancelled) setAuthLoading(false);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, []);

  // Resolve the backend-side user/session before loading chats.
  useEffect(() => {
    if (authLoading) return;

    if (!isAuthenticated && !guestSessionId) {
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
          setCurrentUser(backendUser);
          setBackendAuthReady(true);
          setLoadError(null);
        }
      } catch (e) {
        if (!cancelled) {
          setBackendAuthReady(false);
          setLoadError(
            e instanceof Error
              ? e.message
              : "The backend could not validate your login."
          );
        }
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [authLoading, isAuthenticated, guestSessionId]);

  // Discover models only after authentication/guest session is ready.
  useEffect(() => {
    if (!backendAuthReady) return;

    api
      .getModels()
      .then((catalog) => {
        setModels(catalog.models);

        const saved = localStorage.getItem("privy-selected-model");
        const savedIsAvailable =
          !!saved && catalog.models.some((model) => model.id === saved);

        const next =
          savedIsAvailable
            ? saved!
            : catalog.default_model_id || catalog.models[0]?.id || "";

        setSelectedModelId(next);

        if (next) {
          localStorage.setItem("privy-selected-model", next);
        }
      })
      .catch(() => {
        // Model discovery is non-blocking.
      });
  }, [backendAuthReady]);

  // Load chats after the backend identity is ready.
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
          setMessages([]);
          setAttachments([]);
        }
      } catch (e) {
        setLoadError(
          e instanceof Error ? e.message : "Couldn't reach the backend."
        );
      }
    })();

    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [backendAuthReady]);

  function clearPendingFileState() {
    setPendingFile(null);
    setPendingColumns([]);
    setPendingRowCount(0);
    setSelectedColumns([]);
    setUseNerForFile(false);
    setPendingFileId(null);
    setPendingFileQueue([]);
    setIsEditingFile(false);
  }

  async function selectChat(chatId: string) {
    setActiveChatId(chatId);
    clearPendingFileState();
    setAttachments([]);

    try {
      const [msgs, fileInfos] = await Promise.all([
        api.getMessages(chatId),
        api.listFiles(chatId),
      ]);

      setMessages(msgs);
      setAttachments(fileInfos.map(toLocalAttachment));
      setLoadError(null);
    } catch (e) {
      setLoadError(
        e instanceof Error ? e.message : "Couldn't load that chat."
      );
    }
  }

  function toLocalAttachment(fileInfo: ChatFileInfo): LocalAttachment {
    return {
      fileId: fileInfo.file_id,
      filename: fileInfo.filename,
      maskedCount: fileInfo.masked_count,
      canEdit: false,
      file: null,
      columns: fileInfo.columns,
    };
  }

  async function handleStartGuest() {
    try {
      const result = await api.createGuestSession();
      sessionStorage.setItem("privy-guest-session", result.session_id);
      setGuestSessionId(result.session_id);
      setCurrentUser(result.user);
      setLoadError(null);
    } catch (e) {
      setLoadError(
        e instanceof Error ? e.message : "Couldn't start a guest session."
      );
    }
  }

  async function handleSignIn() {
    setLoadError(null);
    setAuthPanelOpen(true);
  }

  async function handleAuthSuccess(accessToken: string, user: CurrentUser) {
    setAccessToken(accessToken);
    setCurrentUser(user);
    setIsAuthenticated(true);
    setAuthPanelOpen(false);
    setLoadError(null);
  }

  async function handleLogout() {
    streamControllerRef.current?.abort();
    setIsStreaming(false);
    setSettingsOpen(false);
    setLoadError(null);

    if (!isAuthenticated && guestSessionId) {
      sessionStorage.removeItem("privy-guest-session");
      setGuestSessionId(null);
      setCurrentUser(null);
      setChats([]);
      setActiveChatId(null);
      setMessages([]);
      setAttachments([]);
      setBackendAuthReady(false);
      clearPendingFileState();
      return;
    }

    try {
      await api.logout();
    } finally {
      clearAccessToken();
      setIsAuthenticated(false);
      setCurrentUser(null);
      setChats([]);
      setActiveChatId(null);
      setMessages([]);
      setAttachments([]);
      setBackendAuthReady(false);
      clearPendingFileState();
    }
  }

  async function handleNewChat() {
    const created = await api.createChat();

    setChats((prev) => [created, ...prev]);
    setActiveChatId(created.chat_id);
    setMessages([]);
    setAttachments([]);
    clearPendingFileState();
  }

  async function handleRenameChat(chatId: string, title: string) {
    const updated = await api.renameChat(chatId, title);
    setChats((prev) =>
      prev.map((c) => (c.chat_id === chatId ? updated : c))
    );
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
        setAttachments([]);
        clearPendingFileState();
      }
    }
  }

  async function handleExportChat(chatId: string) {
    await api.exportChat(chatId);
  }

  async function prepareNewFile(file: File) {
    if (!activeChatId) return;

    setIsUploading(true);
    setIsEditingFile(false);
    setUseNerForFile(false);

    try {
      const preview = await api.previewFile(activeChatId, file);

      setPendingFile(file);
      setPendingFileId(null);
      setPendingColumns(preview.columns);
      setPendingRowCount(preview.row_count);
      setSelectedColumns(
        preview.columns.filter((c) => c.type).map((c) => c.name)
      );
      setUseNerForFile(false);
    } catch (e) {
      window.alert(
        e instanceof Error ? e.message : "Couldn't inspect that file."
      );
      setPendingFileQueue([]);
    } finally {
      setIsUploading(false);
    }
  }

  async function handleUploadFiles(files: File[]) {
    if (!activeChatId || isUploading || pendingFile) return;

    if (files.length > MAX_FILES_PER_SELECTION) {
      window.alert(
        `You can select up to ${MAX_FILES_PER_SELECTION} files at a time.`
      );
      return;
    }

    const oversized = files.find(
      (file) => file.size > MAX_FILE_SIZE_BYTES
    );

    if (oversized) {
      window.alert(
        `"${oversized.name}" is too large. Privy allows files up to ${MAX_FILE_SIZE_MB} MB each.`
      );
      return;
    }

    if (files.length + attachments.length > MAX_FILES_PER_CHAT) {
      const remaining = Math.max(
        0,
        MAX_FILES_PER_CHAT - attachments.length
      );

      window.alert(
        remaining === 0
          ? `This chat already has ${MAX_FILES_PER_CHAT} files. Remove a file before adding another.`
          : `This chat can hold up to ${MAX_FILES_PER_CHAT} files. You can add ${remaining} more.`
      );
      return;
    }

    const [first, ...rest] = files;
    setPendingFileQueue(rest);
    await prepareNewFile(first);
  }

  async function handleEditFile(fileId: string) {
    if (!activeChatId || isUploading) return;

    const attachment = attachments.find((item) => item.fileId === fileId);
    if (!attachment) return;

    if (!attachment.file) {
      window.alert(
        "For privacy, Privy does not keep the original file after a page refresh. Re-attach the original file to edit its masking settings."
      );
      return;
    }

    setIsUploading(true);
    setIsEditingFile(true);
    // File metadata does not retain the previous NER preference. Require an
    // explicit opt-in whenever the source file is reattached for editing.
    setUseNerForFile(false);

    try {
      const preview = await api.previewFile(activeChatId, attachment.file);

      setPendingFile(attachment.file);
      setPendingFileId(fileId);
      setPendingColumns(preview.columns);
      setPendingRowCount(preview.row_count);

      const enabledNames = attachment.columns
        .filter((column) => column.enabled)
        .map((column) => column.name);

      setSelectedColumns(
        enabledNames.length > 0
          ? enabledNames
          : preview.columns.filter((column) => column.type).map((c) => c.name)
      );
    } catch (e) {
      setIsEditingFile(false);
      window.alert(
        e instanceof Error
          ? e.message
          : "Couldn't reopen masking settings."
      );
    } finally {
      setIsUploading(false);
    }
  }

  async function handleRemoveFile(fileId: string) {
    if (!activeChatId || isUploading) return;

    const attachment = attachments.find((item) => item.fileId === fileId);
    if (!attachment) return;

    if (!window.confirm(`Remove ${attachment.filename} from this chat?`)) {
      return;
    }

    setIsUploading(true);

    try {
      await api.removeFile(activeChatId, fileId);

      setAttachments((prev) =>
        prev.filter((item) => item.fileId !== fileId)
      );

      if (pendingFileId === fileId) {
        clearPendingFileState();
      }
    } catch (e) {
      window.alert(
        e instanceof Error ? e.message : "Couldn't remove the file."
      );
    } finally {
      setIsUploading(false);
    }
  }

  async function handleApplyFile() {
    if (
      !activeChatId ||
      !pendingFile ||
      selectedColumns.length === 0 ||
      isUploading
    ) {
      return;
    }

    setIsUploading(true);

    try {
      const selected = new Set(selectedColumns);

      const disabledColumns = pendingColumns
        .map((column) => column.name)
        .filter((name) => !selected.has(name));

      const result = await api.uploadFile(activeChatId, pendingFile, {
        useNer: useNerForFile,
        nerConfidence: 0.6,
        disabledColumns,
        fileId: pendingFileId ?? undefined,
      });

      const updated: LocalAttachment = {
        fileId: result.file_id,
        filename: result.filename,
        maskedCount: result.masked_count,
        canEdit: true,
        file: pendingFile,
        columns: result.columns,
      };

      setAttachments((prev) => {
        if (pendingFileId) {
          return prev.map((item) =>
            item.fileId === pendingFileId ? updated : item
          );
        }

        return [...prev, updated];
      });

      const nextFile = pendingFileQueue[0];

      setPendingFileQueue((prev) => prev.slice(1));
      setPendingFileId(null);
      setPendingFile(null);
      setPendingColumns([]);
      setPendingRowCount(0);
      setSelectedColumns([]);
      setIsEditingFile(false);

      if (nextFile) {
        await prepareNewFile(nextFile);
      }
    } catch (e) {
      window.alert(
        e instanceof Error ? e.message : "Masking failed."
      );
    } finally {
      setIsUploading(false);
    }
  }

  function handleStopGeneration() {
    streamControllerRef.current?.abort();
  }

  function handleModelChange(modelId: string) {
    setSelectedModelId(modelId);
    localStorage.setItem("privy-selected-model", modelId);
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

    setMessages((prev) => [
      ...prev,
      { role: "user", content: text, masked_count: 0 },
      { role: "assistant", content: "", masked_count: 0 },
    ]);

    setIsStreaming(true);

    const runStream = async (allowUnmaskedRisk: boolean) => {
      const controller = new AbortController();
      streamControllerRef.current = controller;

      await api.streamMessage(
        chatId!,
        {
          question: text,
          use_ner: true,
          ner_confidence: 0.6,
          concise: true,
          model_id: selectedModelId || undefined,
          allow_unmasked_risk: allowUnmaskedRisk,
        },
        {
          onDelta: (piece) => {
            setMessages((prev) => {
              const next = [...prev];
              const last = next[next.length - 1];

              if (!last) return prev;

              next[next.length - 1] = {
                ...last,
                content: last.content + piece,
              };

              return next;
            });
          },

          onDone: (maskedCount) => {
            setMessages((prev) => {
              const next = [...prev];
              const last = next[next.length - 1];

              if (!last) return prev;

              next[next.length - 1] = {
                ...last,
                masked_count: maskedCount,
              };

              return next;
            });

            setIsStreaming(false);
            streamControllerRef.current = null;
            api.listChats().then(setChats).catch(() => {});
          },

          onSecurityWarning: (warning) => {
            if (allowUnmaskedRisk) return;

            const types = warning.findings
              .map(
                (finding) =>
                  `${finding.type.replaceAll("_", " ")} (${finding.count})`
              )
              .join(", ");

            const detail = types
              ? `Detected: ${types}.`
              : warning.message;

            const proceed = window.confirm(
              `${warning.message}\n\n${detail}\n\nSend to the selected model anyway?`
            );

            if (proceed) {
              void runStream(true);
            } else {
              setMessages((prev) => prev.slice(0, -2));
              setIsStreaming(false);
              streamControllerRef.current = null;
            }
          },

          onError: (message) => {
            setMessages((prev) => {
              const next = [...prev];
              const last = next[next.length - 1];

              if (!last) return prev;

              next[next.length - 1] = {
                ...last,
                content: message,
                masked_count: 0,
              };

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
    };

    await runStream(false);
  }

  if (authLoading) {
    return (
      <div className="flex h-screen w-screen items-center justify-center bg-bg text-ink">
        <div className="text-sm text-ink/60">Loading Privy…</div>
      </div>
    );
  }

  if (!isAuthenticated && !guestSessionId) {
    return (
      <AuthPanel
        onAuthenticated={handleAuthSuccess}
        onGuest={handleStartGuest}
        error={loadError}
      />
    );
  }

  if (loadError) {
    return (
      <div className="flex h-screen w-screen items-center justify-center p-6 text-center">
        <div className="max-w-xl">
          <p className="mb-2 text-lg font-semibold">
            Privy couldn't sign you in
          </p>

          <p className="text-sm text-ink/60">
            {loadError}
          </p>

          <p className="mt-4 text-sm text-ink/60">
            Check the FastAPI terminal for the exact error, then reload the
            page.
          </p>

          <button
            type="button"
            onClick={() => window.location.reload()}
            className="mt-5 rounded-xl bg-ink px-4 py-2 text-sm font-medium text-white hover:opacity-90"
          >
            Reload
          </button>
        </div>
      </div>
    );
  }

  if (!backendAuthReady) {
    return (
      <div className="flex h-screen w-screen items-center justify-center bg-bg text-ink">
        <div className="text-sm text-ink/60">Signing you in…</div>
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
        onSelectChat={selectChat}
        onRenameChat={handleRenameChat}
        onDeleteChat={handleDeleteChat}
        onExportChat={handleExportChat}
        onOpenSettings={() => setSettingsOpen(true)}
        onLogout={handleLogout}
        onSignIn={handleSignIn}
        currentUser={currentUser}
        isAdmin={currentUser?.role === "admin"}
        isGuest={currentUser?.role === "guest"}
      />

      <ChatPane
        messages={messages}
        suggestions={SUGGESTION_CHIPS}
        attachments={attachments}
        isStreaming={isStreaming}
        isUploading={isUploading}
        pendingFile={pendingFile}
        pendingColumns={pendingColumns}
        pendingRowCount={pendingRowCount}
        selectedColumns={selectedColumns}
        useNerForFile={useNerForFile}
        isEditingFile={isEditingFile}
        onSelectedColumnsChange={setSelectedColumns}
        onUseNerForFileChange={setUseNerForFile}
        onCancelFile={clearPendingFileState}
        onApplyFile={handleApplyFile}
        onEditFile={handleEditFile}
        onRemoveFile={handleRemoveFile}
        onStop={handleStopGeneration}
        onSend={handleSend}
        onUploadFiles={handleUploadFiles}
        pendingQueueCount={pendingFileQueue.length}
        models={models}
        selectedModelId={selectedModelId}
        onModelChange={handleModelChange}
      />

      {currentUser?.role === "admin" && (
        <SettingsPanel
          open={settingsOpen}
          onClose={() => setSettingsOpen(false)}
        />
      )}

      {authPanelOpen && !isAuthenticated && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 px-6 backdrop-blur-sm">
          <div className="w-full max-w-md">
            <AuthPanel
              compact
              onAuthenticated={handleAuthSuccess}
              onGuest={async () => {
                setAuthPanelOpen(false);
                await handleStartGuest();
              }}
              onClose={() => setAuthPanelOpen(false)}
              error={loadError}
            />
          </div>
        </div>
      )}
    </div>
  );
}
