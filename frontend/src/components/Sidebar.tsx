import { useState } from "react";
import type { Chat } from "../types";

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
        className="mb-4 rounded-md border border-white/20 bg-white/[0.07] py-2 text-sm font-medium transition-colors hover:border-accent hover:bg-white/[0.14]"
      >
        + New chat
      </button>

      {chats.length > 0 && (
        <>
          <p className="mb-2 px-1 text-xs text-white/50">Recent chats</p>
          <div className="flex flex-1 flex-col gap-1 overflow-y-auto">
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
    </aside>
  );
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
