import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev server proxies /api to the FastAPI backend (session 1/2's work), so
// the frontend can call relative paths like fetch("/api/chats") in dev
// without hardcoding a port or fighting CORS locally. In production this
// would instead be whatever reverse proxy sits in front of both.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
});
