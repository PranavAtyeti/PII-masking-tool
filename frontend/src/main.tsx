import React from "react";
import ReactDOM from "react-dom/client";
import { Auth0Provider } from "@auth0/auth0-react";
import App from "./App";
import "./index.css";

const domain = import.meta.env.VITE_AUTH0_DOMAIN as string | undefined;
const clientId = import.meta.env.VITE_AUTH0_CLIENT_ID as string | undefined;
const audience = import.meta.env.VITE_AUTH0_AUDIENCE as string | undefined;

if (!domain || !clientId || !audience) {
  throw new Error(
    "Missing Auth0 frontend configuration. Set VITE_AUTH0_DOMAIN, VITE_AUTH0_CLIENT_ID, and VITE_AUTH0_AUDIENCE in frontend/.env."
  );
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <Auth0Provider
      domain={domain}
      clientId={clientId}
      authorizationParams={{
        redirect_uri: window.location.origin,
        audience,
        scope: "openid profile email",
      }}
      cacheLocation="memory"
    >
      <App />
    </Auth0Provider>
  </React.StrictMode>
);
