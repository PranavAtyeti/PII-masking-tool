// SESSION 3 ONLY. This whole file goes away in session 4, once App.tsx
// fetches real chats/messages from the backend instead of starting from
// this. Kept in one place, clearly labeled, so nothing "mock" quietly
// survives into the wired-up version by accident.

import type { Chat, Message } from "./types";

export const MOCK_CHATS: Chat[] = [
  {
    chat_id: "mock-1",
    title: "What looks sensitive in this file?",
    created_at: Date.now() / 1000 - 3600,
    updated_at: Date.now() / 1000 - 120,
  },
  {
    chat_id: "mock-2",
    title: "Draft a polite follow-up email",
    created_at: Date.now() / 1000 - 7200,
    updated_at: Date.now() / 1000 - 3600,
  },
];

export const MOCK_MESSAGES: Record<string, Message[]> = {
  "mock-1": [
    { role: "user", content: "What looks sensitive in this file?", masked_count: 0 },
    {
      role: "assistant",
      content:
        "The file contains several types of sensitive personal data: EmployeeID, Full Name, Email, Mobile Number, and a few free-text notes mentioning names.",
      masked_count: 111,
    },
  ],
  "mock-2": [],
};

export const SUGGESTION_CHIPS = [
  "What can this app do?",
  "Draft a polite follow-up email",
  "Explain how my privacy is protected",
];
