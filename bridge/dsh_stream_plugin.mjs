import { createServer } from "node:http";
import { timingSafeEqual } from "node:crypto";
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";

export const name = "ai-companion-stream";
export const inject = ["agents", "sessionPersistence"];

// Task-owned process only. Use native persistence APIs; never recreate an existing log.
export function apply(ctx) {
  const token = process.env.AI_COMPANION_DSH_CONTROL;
  if (!token || token.length < 32) throw new Error("Missing task control credential");
  const require = createRequire(process.env.AI_COMPANION_DSH_ENTRY);
  const llm = import(pathToFileURL(require.resolve("@deepseek-ai/dsh-llm")));
  const persistence = import(pathToFileURL(require.resolve("@deepseek-ai/dsh-session-persistence")));
  const handles = new Map();
  const notify = (method, params) =>
    process.stdout.write(JSON.stringify({ jsonrpc: "2.0", method, params }) + "\n");
  ctx.on("agent/assistant-stream", ({ agent, frame }) =>
    notify("voice.stream", { sessionId: String(agent.session.id), frame }));
  async function ownedSession(input) {
    if (handles.has(input.sessionId)) return handles.get(input.sessionId);
    const options = { provider: "deepseek-official", model: input.model, maxTokens: input.maxTokens };
    const { SessionPersistenceNotFoundError } = await persistence;
    let handle;
    try {
      handle = await ctx.agents.resume({ resumeSessionId: input.sessionId, agentOptions: options });
      // A resumed unfinished turn must settle before arming media for the new user prompt.
      handle.agent.cancel({ kind: "user" });
      await handle.agent.whenIdle();
    } catch (error) {
      if (!(error instanceof SessionPersistenceNotFoundError)) throw error;
      handle = await ctx.agents.create({
        sessionId: input.sessionId, meta: { cwd: input.cwd }, agentOptions: options
      });
    }
    handles.set(input.sessionId, handle);
    return handle;
  }
  const server = createServer(async (request, response) => {
    const supplied = Buffer.from(request.headers.authorization ?? "");
    const expected = Buffer.from("Bearer " + token);
    if (supplied.length !== expected.length || !timingSafeEqual(supplied, expected)) {
      response.writeHead(401).end(); return;
    }
    if (request.method !== "POST" || !["/cancel", "/prompt"].includes(request.url)) {
      response.writeHead(404).end(); return;
    }
    try {
      let body = "";
      for await (const chunk of request) {
        body += chunk;
        if (Buffer.byteLength(body) > 32768) throw new Error("Request too large");
      }
      const input = JSON.parse(body);
      if (typeof input.sessionId !== "string" || !/^[A-Za-z0-9_-]{1,100}$/.test(input.sessionId))
        throw new Error("Invalid session");
      if (request.url === "/cancel") {
        const handle = handles.get(input.sessionId);
        if (handle) { handle.agent.cancel({ kind: "user" }); await handle.agent.whenIdle(); }
        response.writeHead(200, { "Content-Type": "application/json" }).end('{"cancelled":true}');
      } else {
        if (typeof input.text !== "string" || input.text.length > 8000 ||
            typeof input.model !== "string" || typeof input.cwd !== "string" ||
            !Number.isInteger(input.maxTokens) || input.maxTokens < 1 || input.maxTokens > 4096)
          throw new Error("Invalid prompt");
        const handle = await ownedSession(input);
        const { createUserMessage } = await llm;
        const message = createUserMessage({ content: [{ type: "text", text: input.text }], source: { kind: "user" } });
        notify("voice.prompt-start", { sessionId: input.sessionId, messageId: String(message.id) });
        handle.agent.followup(message);
        response.writeHead(200, { "Content-Type": "application/json" }).end(JSON.stringify({ messageId: String(message.id) }));
      }
    } catch (error) {
      response.writeHead(409, { "Content-Type": "application/json" })
        .end(JSON.stringify({ error: "dsh_session_unavailable", kind: error.constructor.name }));
    }
  });
  ctx.effect(() => {
    server.listen(0, "127.0.0.1", () => notify("voice.ready", { port: server.address().port }));
    return async () => {
      const closed = new Promise(resolve => server.close(resolve));
      server.closeAllConnections();
      await Promise.allSettled([...handles.values()].map(handle => handle.dispose()));
      await closed;
    };
  }, "voice.control");
}
