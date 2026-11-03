// Talking to the host from inside the in-chat view (MCP Apps).

import type { App } from "@modelcontextprotocol/ext-apps";

export type Data = Record<string, any>;

export class ToolFailure extends Error {}

/** The structured result of a ctx tool, or a ToolFailure carrying the server's message. */
export function parseResult(result: { isError?: boolean; structuredContent?: unknown; content?: any[] }): Data {
  const text = (result.content || []).filter((c) => c?.type === "text").map((c) => c.text).join("\n");
  if (result.isError) {
    let message = text || "The tool call failed.";
    try {
      message = JSON.parse(text).message || message;
    } catch {
      /* plain-text error */
    }
    throw new ToolFailure(message);
  }
  if (result.structuredContent && typeof result.structuredContent === "object") {
    return result.structuredContent as Data;
  }
  try {
    return JSON.parse(text);
  } catch {
    return { text };
  }
}

export async function callTool(app: App, name: string, args: Data): Promise<Data> {
  return parseResult(await app.callServerTool({ name, arguments: args }));
}

/** Tell the model what the user did in the view, so the conversation stays in sync. */
export async function tellModel(app: App, text: string): Promise<void> {
  try {
    await app.updateModelContext({ content: [{ type: "text", text }] });
  } catch {
    /* host without model-context support: the action itself still happened */
  }
}

export function applyTheme(theme: string | undefined) {
  if (theme === "dark" || theme === "light") document.documentElement.dataset.theme = theme;
}

export const short = (id?: string | null) => (id || "").replace(/-/g, "").slice(0, 8);
export const label = (t: string) => t.replace(/_/g, " ");
