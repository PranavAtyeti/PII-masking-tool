import { useState } from "react";
import type { Chat, CurrentUser } from "../types";

interface SidebarProps {
  chats: Chat[];
  activeChatId: string | null;
  collapsed: boolean;
  onToggleCollapsed: () => void;
  onNewChat: () => void;
  onSelectChat: (chatId: string) => void;
  onRenameChat: (chatId: string, title: string) => void;
  onDeleteChat: (chatId: string) => void;
  onExportChat: (chatId: string) => void;
  onOpenSettings: () => void;
  onLogout: () => void;
  onSignIn: () => void;
  currentUser: CurrentUser | null;
  isAdmin: boolean;
  isGuest: boolean;
}

export function Sidebar({
  chats,
  activeChatId,
  collapsed,
  onToggleCollapsed,
  onNewChat,
  onSelectChat,
  onRenameChat,
  onDeleteChat,
  onExportChat,
  onOpenSettings,
  onLogout,
  onSignIn,
  currentUser,
  isAdmin,
  isGuest,
}: SidebarProps) {
  if (collapsed) {
    return (
      <div className="flex w-14 flex-col items-center gap-4 bg-gradient-to-b from-sidebar-from to-sidebar-to py-4">
        <button
          onClick={onToggleCollapsed}
          aria-label="Expand sidebar"
          className="rounded-md p-2 text-white/70 hover:bg-white/10 hover:text-white"
        >
          »
        </button>
        <span className="text-xl" title="Privy">
          🔒
        </span>
        {isAdmin && (
          <button
            onClick={onOpenSettings}
            aria-label="Open settings"
            title="Settings"
            className="rounded-md p-2 text-white/70 hover:bg-white/10 hover:text-white"
          >
            ⚙
          </button>
        )}
        {isGuest && (
          <button
            onClick={onSignIn}
            aria-label="Sign in to save chats"
            title="Sign in to save chats"
            className="rounded-md p-2 text-white/70 hover:bg-white/10 hover:text-white"
          >
            ⇥
          </button>
        )}
        <button
          onClick={onLogout}
          aria-label={isGuest ? "End guest session" : "Log out"}
          title={isGuest ? "End guest session" : "Log out"}
          className="rounded-md p-2 text-white/70 hover:bg-white/10 hover:text-white"
        >
          ↪
        </button>
      </div>
    );
  }

  return (
    <aside className="flex w-72 shrink-0 flex-col bg-gradient-to-b from-sidebar-from to-sidebar-to px-4 py-5 text-white">
      <div className="mb-4 flex items-start justify-between">
        <div>
          <h1 className="flex items-center gap-2 text-lg font-semibold">
            <span aria-hidden>🔒</span> Privy
          </h1>
          <p className="mt-1 text-xs text-white/60">Personal data stays on this device</p>
        </div>
        <button
          onClick={onToggleCollapsed}
          aria-label="Collapse sidebar"
          className="rounded-md p-1.5 text-white/50 hover:bg-white/10 hover:text-white"
        >
          «
        </button>
      </div>

      <button
        onClick={onNewChat}
        disabled={isGuest && chats.length > 0}
        className="mb-1 rounded-md border border-white/20 bg-white/[0.07] py-2 text-sm font-medium transition-colors hover:border-accent hover:bg-white/[0.14] disabled:cursor-not-allowed disabled:opacity-40"
      >
        + New chat
      </button>
      {isGuest && (
        <p className="mb-3 px-1 text-[11px] text-white/40">Guest mode · 1 chat · 5 questions</p>
      )}

      <div className="flex min-h-0 flex-1 flex-col">
        {chats.length > 0 && (
          <>
            <p className="mb-2 px-1 text-xs uppercase tracking-[0.12em] text-white/40">Recent</p>
            <div className="flex min-h-0 flex-1 flex-col gap-1 overflow-y-auto">
              {chats.map((chat) => (
                <ChatListItem
                  key={chat.chat_id}
                  chat={chat}
                  isActive={chat.chat_id === activeChatId}
                  onSelect={() => onSelectChat(chat.chat_id)}
                  onRename={(title) => onRenameChat(chat.chat_id, title)}
                  onDelete={() => onDeleteChat(chat.chat_id)}
                  onExport={() => onExportChat(chat.chat_id)}
                />
              ))}
            </div>
          </>
        )}
      </div>

      <div className="mt-4 border-t border-white/10 pt-3">
        {currentUser && (
          <div className="mb-2 flex items-center gap-3 rounded-lg px-2 py-2">
            <div
              className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-white/15 text-xs font-semibold text-white"
              aria-hidden
            >
              {getInitials(currentUser.display_name, currentUser.email)}
            </div>
            <div className="min-w-0 flex-1">
              <p className="truncate text-sm font-medium text-white">
                {currentUser.display_name || "Privy user"}
              </p>
              <p className="truncate text-xs text-white/50">
                {currentUser.email || (isGuest ? "Temporary session" : "Signed in")}
              </p>
              {isAdmin && (
                <p className="mt-0.5 text-[11px] text-white/40">Administrator</p>
              )}
            </div>
          </div>
        )}

        <div className="flex flex-col gap-1">
          {isGuest && (
            <button
              onClick={onSignIn}
              className="flex items-center gap-2 rounded-md px-3 py-2 text-left text-sm text-white/70 transition-colors hover:bg-white/[0.10] hover:text-white"
            >
              <span aria-hidden>⇥</span>
              <span>Sign in to save chats</span>
            </button>
          )}
          {isAdmin && (
            <button
              onClick={onOpenSettings}
              className="flex items-center gap-2 rounded-md px-3 py-2 text-left text-sm text-white/70 transition-colors hover:bg-white/[0.10] hover:text-white"
            >
              <span aria-hidden>⚙</span>
              <span>Admin settings</span>
            </button>
          )}
          <button
            onClick={onLogout}
            className="flex items-center gap-2 rounded-md px-3 py-2 text-left text-sm text-white/70 transition-colors hover:bg-white/[0.10] hover:text-white"
          >
            <span aria-hidden>↪</span>
            <span>{isGuest ? "End guest session" : "Log out"}</span>
          </button>
        </div>
      </div>
    </aside>
  );
}

function getInitials(displayName: string | null, email: string | null): string {
  const source = displayName?.trim() || email?.trim() || "P";
  const parts = source.split(/\s+/).filter(Boolean);

  if (parts.length >= 2) {
    return `${parts[0][0]}${parts[parts.length - 1][0]}`.toUpperCase();
  }

  return source.slice(0, 2).toUpperCase();
}

interface ChatListItemProps {
  chat: Chat;
  isActive: boolean;
  onSelect: () => void;
  onRename: (title: string) => void;
  onDelete: () => void;
  onExport: () => void;
}

function ChatListItem({ chat, isActive, onSelect, onRename, onDelete, onExport }: ChatListItemProps) {
  const [menuOpen, setMenuOpen] = useState(false);
  const [renaming, setRenaming] = useState(false);
  const [draftTitle, setDraftTitle] = useState(chat.title);

  const closeMenu = () => {
    setMenuOpen(false);
    setRenaming(false);
  };

  return (
    <div className="relative">
      <div className="flex items-stretch gap-1">
        <button
          onClick={onSelect}
          disabled={isActive}
          className={`flex-1 truncate rounded-md px-3 py-2 text-left text-sm transition-colors ${
            isActive
              ? "cursor-default bg-white/[0.16] font-medium"
              : "bg-white/[0.07] hover:bg-white/[0.14]"
          }`}
          title={chat.title}
        >
          {chat.title || "New chat"}
        </button>
        <button
          onClick={() => setMenuOpen((v) => !v)}
          aria-label="Chat options"
          aria-expanded={menuOpen}
          className="w-8 shrink-0 rounded-md bg-white/[0.07] text-white/70 hover:bg-white/[0.14] hover:text-white"
        >
          ⋮
        </button>
      </div>

      {menuOpen && (
        <div className="absolute right-0 top-full z-10 mt-1 w-48 rounded-md border border-border bg-surface p-1.5 text-ink shadow-lg">
          {renaming ? (
            <div className="flex flex-col gap-1.5 p-1">
              <input
                autoFocus
                value={draftTitle}
                onChange={(e) => setDraftTitle(e.target.value)}
                placeholder="Chat name"
                className="rounded border border-border px-2 py-1 text-sm outline-none focus:border-accent"
              />
              <div className="flex gap-1.5">
                <button
                  onClick={() => {
                    const cleaned = draftTitle.trim();
                    if (cleaned && cleaned !== chat.title) onRename(cleaned);
                    closeMenu();
                  }}
                  className="flex-1 rounded bg-ink py-1 text-xs font-medium text-white hover:opacity-90"
                >
                  Save
                </button>
                <button
                  onClick={closeMenu}
                  className="flex-1 rounded border border-border py-1 text-xs hover:bg-bg"
                >
                  Cancel
                </button>
              </div>
            </div>
          ) : (
            <div className="flex flex-col">
              <button
                onClick={() => setRenaming(true)}
                className="rounded px-2 py-1.5 text-left text-sm hover:bg-bg"
              >
                Rename
              </button>
              <button
                onClick={() => {
                  onExport();
                  closeMenu();
                }}
                className="rounded px-2 py-1.5 text-left text-sm hover:bg-bg"
              >
                Export as .txt
              </button>
              <button
                onClick={() => {
                  onDelete();
                  closeMenu();
                }}
                className="rounded px-2 py-1.5 text-left text-sm text-red-600 hover:bg-red-50"
              >
                Delete
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
