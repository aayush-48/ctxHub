import { StrictMode, useState } from "react";
import { createRoot } from "react-dom/client";
import { useApp } from "@modelcontextprotocol/ext-apps/react";
import { applyTheme, parseResult, ToolFailure, type Data } from "./bridge";
import { View } from "./views";
import "./mcp.css";

function Root() {
  const [data, setData] = useState<Data | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState<string | null>(null);

  const { app, error: connectError } = useApp({
    appInfo: { name: "ctx", version: "0.1.0" },
    capabilities: {},
    autoResize: true,
    onAppCreated: (a) => {
      a.ontoolinput = (params) => {
        setPending(String(params.arguments?.repo ?? ""));
      };
      a.ontoolresult = (result) => {
        setPending(null);
        try {
          setData(parseResult(result));
          setError(null);
        } catch (e) {
          setError(e instanceof ToolFailure ? e.message : String(e));
        }
      };
      a.ontoolcancelled = () => setPending(null);
      a.onhostcontextchanged = (ctx) => applyTheme(ctx.theme);
    },
  });

  if (app) applyTheme(app.getHostContext()?.theme);
  if (connectError) return <div className="box error">Could not connect to the chat: {connectError.message}</div>;
  if (error) return <div className="box error">{error}</div>;
  if (!app || !data) {
    return <div className="box muted">{pending !== null ? "Working on it…" : "Loading…"}</div>;
  }
  return <View app={app} data={data} onData={setData} />;
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <Root />
  </StrictMode>,
);
