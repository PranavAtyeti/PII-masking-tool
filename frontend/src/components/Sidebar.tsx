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

function Icon({ name, className = "h-4 w-4" }: { name: "plus" | "chevron" | "settings" | "login" | "logout" | "more" | "chat" | "search"; className?: string }) {
  const common = { className, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: 1.8, strokeLinecap: "round" as const, strokeLinejoin: "round" as const, "aria-hidden": true };
  if (name === "plus") return <svg {...common}><path d="M12 5v14M5 12h14" /></svg>;
  if (name === "chevron") return <svg {...common}><path d="m9 18 6-6-6-6" /></svg>;
  if (name === "settings") return <svg {...common}><path d="M12 8.2a3.8 3.8 0 1 0 0 7.6 3.8 3.8 0 0 0 0-7.6Z" /><path d="m19.4 15 .1.1a1.8 1.8 0 0 1-2.5 2.5l-.1-.1a1.8 1.8 0 0 0-3 .8v.2a1.8 1.8 0 0 1-3.6 0v-.2a1.8 1.8 0 0 0-3-.8l-.1.1a1.8 1.8 0 1 1-2.5-2.5l.1-.1a1.8 1.8 0 0 0-.8-3H4a1.8 1.8 0 0 1 0-3.6h.2a1.8 1.8 0 0 0 .8-3l-.1-.1a1.8 1.8 0 1 1 2.5-2.5l.1.1a1.8 1.8 0 0 0 3-.8V2a1.8 1.8 0 0 1 3.6 0v.2a1.8 1.8 0 0 0 3 .8l.1-.1a1.8 1.8 0 1 1 2.5 2.5l-.1.1a1.8 1.8 0 0 0 .8 3h.2a1.8 1.8 0 0 1 0 3.6h-.2a1.8 1.8 0 0 0-.8 2.9Z" /></svg>;
  if (name === "login") return <svg {...common}><path d="M10 17l5-5-5-5" /><path d="M15 12H3" /><path d="M14 3h5v18h-5" /></svg>;
  if (name === "logout") return <svg {...common}><path d="M14 8V5a2 2 0 0 0-2-2H5v18h7a2 2 0 0 0 2-2v-3" /><path d="M10 12h11" /><path d="m18 9 3 3-3 3" /></svg>;
  if (name === "more") return <svg {...common}><circle cx="5" cy="12" r="1" fill="currentColor" stroke="none" /><circle cx="12" cy="12" r="1" fill="currentColor" stroke="none" /><circle cx="19" cy="12" r="1" fill="currentColor" stroke="none" /></svg>;
  if (name === "search") return <svg {...common}><circle cx="10.8" cy="10.8" r="6.3" /><path d="m16 16 4.2 4.2" /></svg>;
  return <svg {...common}><path d="M5 6.5h14v10H8.5L5 20V6.5Z" /><path d="M8 10h8M8 13h5" /></svg>;
}

export function Sidebar({ chats, activeChatId, collapsed, onToggleCollapsed, onNewChat, onSelectChat, onRenameChat, onDeleteChat, onExportChat, onOpenSettings, onLogout, onSignIn, currentUser, isAdmin, isGuest }: SidebarProps) {
  if (collapsed) {
    return (
      <aside className="flex w-16 shrink-0 flex-col items-center border-r border-[#dce5f0] bg-[#f7faff] py-4 text-[#19304d]">
        <button type="button" onClick={onToggleCollapsed} aria-label="Expand sidebar" title="Expand sidebar" className="privy-focus-ring flex h-9 w-9 items-center justify-center rounded-lg text-[#667b95] hover:bg-[#eaf2ff] hover:text-[#19304d]"><Icon name="chevron" className="h-4 w-4 rotate-180" /></button>
        <div className="mt-4 flex h-9 w-9 items-center justify-center rounded-xl bg-[#17385f] text-sm font-bold text-white">P</div>
        <button type="button" onClick={onNewChat} disabled={isGuest && chats.length > 0} aria-label="New chat" title="New chat" className="privy-focus-ring mt-6 flex h-9 w-9 items-center justify-center rounded-lg border border-[#d5e2f2] bg-white text-[#3568a8] shadow-sm hover:border-[#b9d1ef] hover:bg-[#edf5ff] disabled:opacity-30"><Icon name="plus" /></button>
        <div className="mt-auto flex flex-col gap-1">
          {isAdmin && <button type="button" onClick={onOpenSettings} aria-label="Settings" title="Settings" className="privy-focus-ring flex h-9 w-9 items-center justify-center rounded-lg text-[#6f8197] hover:bg-[#eaf2ff] hover:text-[#19304d]"><Icon name="settings" /></button>}
          {isGuest && <button type="button" onClick={onSignIn} aria-label="Sign in" title="Sign in to save chats" className="privy-focus-ring flex h-9 w-9 items-center justify-center rounded-lg text-[#6f8197] hover:bg-[#eaf2ff] hover:text-[#19304d]"><Icon name="login" /></button>}
          <button type="button" onClick={onLogout} aria-label={isGuest ? "End guest session" : "Log out"} title={isGuest ? "End guest session" : "Log out"} className="privy-focus-ring flex h-9 w-9 items-center justify-center rounded-lg text-[#6f8197] hover:bg-[#eaf2ff] hover:text-[#19304d]"><Icon name="logout" /></button>
        </div>
      </aside>
    );
  }

  return (
    <aside className="flex w-[252px] shrink-0 flex-col border-r border-[#dce5f0] bg-[#f7faff] px-3 py-4 text-[#19304d]">
      <div className="flex items-center gap-2 px-2 py-1">
        <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-[10px] bg-[#17385f] text-xs font-bold text-white">P</div>
        <div className="min-w-0 flex-1">
          <h1 className="truncate text-[15px] font-semibold tracking-[-0.01em] text-[#183250]">Privy</h1>
          <p className="truncate text-[11px] text-[#7d91aa]">Private AI workspace</p>
        </div>
        <button type="button" onClick={onToggleCollapsed} aria-label="Collapse sidebar" title="Collapse sidebar" className="privy-focus-ring flex h-8 w-8 items-center justify-center rounded-lg text-[#7890aa] hover:bg-[#eaf2ff] hover:text-[#19304d]"><Icon name="chevron" /></button>
      </div>

      <button type="button" onClick={onNewChat} disabled={isGuest && chats.length > 0} className="privy-focus-ring mt-5 flex h-10 items-center gap-2 rounded-lg bg-[#dceaff] px-3 text-left text-[12px] font-semibold text-[#235d9d] transition-colors hover:bg-[#d2e4ff] disabled:cursor-not-allowed disabled:opacity-40">
        <Icon name="plus" className="h-[15px] w-[15px]" /><span>New chat</span><span className="ml-auto text-[11px] font-medium text-[#7290b2]">Ctrl N</span>
      </button>

      <div className="mt-2.5 flex h-9 items-center gap-2 rounded-lg border border-[#dce6f2] bg-white px-3 text-[11px] text-[#91a0b1]">
        <Icon name="search" className="h-3.5 w-3.5" /><span>Search conversations...</span>
      </div>

      {isGuest && <div className="mt-2 rounded-lg bg-[#edf5ff] px-3 py-2 text-[11px] leading-4 text-[#6480a0]">Guest mode · {chats.length > 0 ? "1 chat" : "new chat"} · 5 questions</div>}

      <div className="mt-5 flex min-h-0 flex-1 flex-col">
        {chats.length > 0 ? <>
          <div className="mb-2 px-2"><p className="text-[10px] font-semibold uppercase tracking-[0.13em] text-[#607895]">Recent</p></div>
          <div className="flex min-h-0 flex-1 flex-col gap-1 overflow-y-auto pr-1">
            {chats.map((chat) => <ChatListItem key={chat.chat_id} chat={chat} isActive={chat.chat_id === activeChatId} onSelect={() => onSelectChat(chat.chat_id)} onRename={(title) => onRenameChat(chat.chat_id, title)} onDelete={() => onDeleteChat(chat.chat_id)} onExport={() => onExportChat(chat.chat_id)} />)}
          </div>
        </> : <div className="px-2 pt-3 text-[11px] leading-5 text-[#91a0b1]">Your conversations will appear here.</div>}
      </div>

      <div className="mt-3 border-t border-[#dce5f0] pt-3">
        {currentUser && <div className="flex items-center gap-2.5 rounded-xl px-2 py-2.5">
          <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-[#315f92] text-[11px] font-semibold text-white">{getInitials(currentUser.display_name, currentUser.email)}</div>
          <div className="min-w-0 flex-1"><p className="truncate text-[11px] font-semibold text-[#304965]">{currentUser.display_name || "Privy user"}</p><p className="truncate text-[11px] text-[#8b9caf]">{currentUser.email || (isGuest ? "Temporary session" : "Signed in")}</p></div>
          {isAdmin && <span className="rounded-md bg-white px-1.5 py-1 text-[8px] font-medium text-[#7187a1] shadow-sm">Admin</span>}
        </div>}
        <div className="mt-1 flex flex-col gap-0.5">
          {isGuest && <button type="button" onClick={onSignIn} className="privy-focus-ring flex items-center gap-2 rounded-lg px-3 py-2 text-left text-[11px] text-[#657c97] hover:bg-[#eaf2ff] hover:text-[#19304d]"><Icon name="login" className="h-3.5 w-3.5" /><span>Sign in to save chats</span></button>}
          {isAdmin && <button type="button" onClick={onOpenSettings} className="privy-focus-ring flex items-center gap-2 rounded-lg px-3 py-2 text-left text-[11px] text-[#657c97] hover:bg-[#eaf2ff] hover:text-[#19304d]"><Icon name="settings" className="h-3.5 w-3.5" /><span>Admin settings</span></button>}
          <button type="button" onClick={onLogout} className="privy-focus-ring flex items-center gap-2 rounded-lg px-3 py-2 text-left text-[11px] text-[#657c97] hover:bg-[#eaf2ff] hover:text-[#19304d]"><Icon name="logout" className="h-3.5 w-3.5" /><span>{isGuest ? "End guest session" : "Log out"}</span></button>
        </div>
      </div>
    </aside>
  );
}

function getInitials(displayName: string | null, email: string | null): string {
  const source = displayName?.trim() || email?.trim() || "P";
  const parts = source.split(/\s+/).filter(Boolean);
  if (parts.length >= 2) return `${parts[0][0]}${parts[parts.length - 1][0]}`.toUpperCase();
  return source.slice(0, 2).toUpperCase();
}

interface ChatListItemProps { chat: Chat; isActive: boolean; onSelect: () => void; onRename: (title: string) => void; onDelete: () => void; onExport: () => void; }

function ChatListItem({ chat, isActive, onSelect, onRename, onDelete, onExport }: ChatListItemProps) {
  const [menuOpen, setMenuOpen] = useState(false);
  const [renaming, setRenaming] = useState(false);
  const [draftTitle, setDraftTitle] = useState(chat.title);
  const closeMenu = () => { setMenuOpen(false); setRenaming(false); };
  return <div className="group relative">
    <div className="flex items-center gap-0.5">
      <button type="button" onClick={onSelect} disabled={isActive} className={`privy-focus-ring min-w-0 flex-1 truncate rounded-lg px-2.5 py-2 text-left text-[11px] transition-colors ${isActive ? "cursor-default bg-[#e5effd] font-semibold text-[#234c7b]" : "text-[#61758e] hover:bg-[#edf4fd] hover:text-[#294866]"}`} title={chat.title}>
        <span className="flex items-center gap-2"><Icon name="chat" className={`h-3 w-3 shrink-0 ${isActive ? "text-[#3674b9]" : "text-[#91a2b4]"}`} /><span className="truncate">{chat.title || "New chat"}</span></span>
      </button>
      <button type="button" onClick={() => setMenuOpen((v) => !v)} aria-label="Chat options" aria-expanded={menuOpen} className={`privy-focus-ring flex h-7 w-7 shrink-0 items-center justify-center rounded-lg text-[#91a0b1] hover:bg-[#eaf2ff] hover:text-[#315f92] ${menuOpen ? "bg-[#eaf2ff] text-[#315f92]" : "opacity-0 group-hover:opacity-100"}`}><Icon name="more" className="h-3.5 w-3.5" /></button>
    </div>
    {menuOpen && <div className="absolute right-1 top-full z-30 mt-1 w-44 overflow-hidden rounded-xl border border-[#dce5f0] bg-white p-1.5 text-[#304965] shadow-[0_12px_35px_rgba(30,55,85,0.14)]">
      {renaming ? <div className="p-1"><input autoFocus value={draftTitle} onChange={(e) => setDraftTitle(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") { const cleaned = draftTitle.trim(); if (cleaned && cleaned !== chat.title) onRename(cleaned); closeMenu(); } if (e.key === "Escape") closeMenu(); }} placeholder="Chat name" className="privy-focus-ring w-full rounded-lg border border-[#dce5f0] bg-[#f8fbff] px-2.5 py-2 text-xs outline-none focus:border-[#8bb5e5]" /><div className="mt-1.5 flex gap-1.5"><button type="button" onClick={() => { const cleaned = draftTitle.trim(); if (cleaned && cleaned !== chat.title) onRename(cleaned); closeMenu(); }} className="flex-1 rounded-lg bg-[#17385f] py-1.5 text-[11px] font-medium text-white hover:opacity-90">Save</button><button type="button" onClick={closeMenu} className="flex-1 rounded-lg border border-[#dce5f0] py-1.5 text-[11px] hover:bg-[#f4f7fb]">Cancel</button></div></div> : <div className="flex flex-col"><button type="button" onClick={() => setRenaming(true)} className="rounded-lg px-2.5 py-2 text-left text-[11px] hover:bg-[#f0f5fb]">Rename</button><button type="button" onClick={() => { onExport(); closeMenu(); }} className="rounded-lg px-2.5 py-2 text-left text-[11px] hover:bg-[#f0f5fb]">Export as .txt</button><button type="button" onClick={() => { onDelete(); closeMenu(); }} className="rounded-lg px-2.5 py-2 text-left text-[11px] text-red-600 hover:bg-red-50">Delete</button></div>}
    </div>}
  </div>;
}
