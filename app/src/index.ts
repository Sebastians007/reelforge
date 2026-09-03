import { Hono } from "hono";
import type { Env, Topic } from "./pipeline";
import { runTopic, runAllActiveTopics, approveRun } from "./pipeline";

const app = new Hono<{ Bindings: Env }>();

// ---- Dashboard data ----

app.get("/api/topics", async (c) => {
  const { results } = await c.env.DB.prepare(`SELECT * FROM topics ORDER BY id`).all();
  return c.json(results);
});

app.post("/api/topics", async (c) => {
  const body = await c.req.json<Partial<Topic>>();
  if (!body.name || !body.rss_source || !body.voice_id) {
    return c.json({ error: "name, rss_source, and voice_id are required" }, 400);
  }
  await c.env.DB.prepare(
    `INSERT INTO topics (name, rss_source, aspect_ratio, voice_id, active) VALUES (?, ?, ?, ?, 1)`
  )
    .bind(body.name, body.rss_source, body.aspect_ratio ?? "9:16", body.voice_id)
    .run();
  return c.json({ ok: true });
});

app.post("/api/topics/:id/toggle", async (c) => {
  const id = c.req.param("id");
  await c.env.DB.prepare(`UPDATE topics SET active = 1 - active WHERE id = ?`).bind(id).run();
  return c.json({ ok: true });
});

app.post("/api/topics/:id/run", async (c) => {
  const id = Number(c.req.param("id"));
  const topic = await c.env.DB.prepare(`SELECT * FROM topics WHERE id = ?`).bind(id).first<Topic>();
  if (!topic) return c.json({ error: "Topic not found" }, 404);

  try {
    const runId = await runTopic(c.env, topic);
    return c.json({ ok: true, runId });
  } catch (err) {
    return c.json({ error: err instanceof Error ? err.message : String(err) }, 500);
  }
});

app.get("/api/runs", async (c) => {
  const { results } = await c.env.DB.prepare(
    `SELECT runs.*, topics.name AS topic_name FROM runs
     JOIN topics ON topics.id = runs.topic_id
     ORDER BY runs.id DESC LIMIT 100`
  ).all();
  return c.json(results);
});

app.post("/api/runs/:id/approve", async (c) => {
  const id = Number(c.req.param("id"));
  try {
    await approveRun(c.env, id);
    return c.json({ ok: true });
  } catch (err) {
    return c.json({ error: err instanceof Error ? err.message : String(err) }, 500);
  }
});

// ---- Webhook called by the GitHub Actions render job when it finishes ----

app.post("/api/webhook/render-complete", async (c) => {
  const body = await c.req.json<{
    secret: string;
    run_id: number;
    status: "uploaded" | "failed";
    youtube_video_id?: string;
    error_message?: string;
  }>();

  if (body.secret !== c.env.WEBHOOK_SECRET) {
    return c.json({ error: "Bad secret" }, 401);
  }

  if (body.status === "uploaded") {
    await c.env.DB.prepare(
      `UPDATE runs SET status = 'uploaded', youtube_video_id = ?, updated_at = datetime('now') WHERE id = ?`
    )
      .bind(body.youtube_video_id, body.run_id)
      .run();
  } else {
    await c.env.DB.prepare(
      `UPDATE runs SET status = 'failed', error_message = ?, updated_at = datetime('now') WHERE id = ?`
    )
      .bind(body.error_message ?? "Unknown render failure", body.run_id)
      .run();
  }

  return c.json({ ok: true });
});

// Note: the dashboard itself (public/index.html) is served automatically by
// Cloudflare's static assets handling (see [assets] in wrangler.toml) — it
// never reaches this Worker script. Only /api/* routes run here.

export default {
  fetch: app.fetch,
  async scheduled(_event: ScheduledEvent, env: Env, ctx: ExecutionContext) {
    ctx.waitUntil(runAllActiveTopics(env));
  },
};
