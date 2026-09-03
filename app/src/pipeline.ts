import type { D1Database } from "@cloudflare/workers-types";
import { fetchTopStory } from "./rss";
import { triggerRenderWorkflow } from "./clients/github";
import { approveVideo } from "./clients/youtube";

export interface Env {
  DB: D1Database;
  GITHUB_TOKEN: string;
  GITHUB_OWNER: string;
  GITHUB_REPO: string;
  YOUTUBE_CLIENT_ID: string;
  YOUTUBE_CLIENT_SECRET: string;
  YOUTUBE_REFRESH_TOKEN: string;
  WEBHOOK_SECRET: string;
  DASHBOARD_PASSWORD: string;
  APP_URL: string; // this Worker's own live URL, used to build the webhook callback URL
}

export interface Topic {
  id: number;
  name: string;
  rss_source: string;
  aspect_ratio: string;
  voice_id: string;
  active: number;
}

/** Pulls the top story for a topic and kicks off a render run. Used by both cron and the Run Now button. */
export async function runTopic(env: Env, topic: Topic): Promise<number> {
  const story = await fetchTopStory(topic.rss_source);

  const insert = await env.DB.prepare(
    `INSERT INTO runs (topic_id, status, story_title, story_link) VALUES (?, 'queued', ?, ?) RETURNING id`
  )
    .bind(topic.id, story.title, story.link)
    .first<{ id: number }>();

  const runId = insert!.id;

  try {
    await triggerRenderWorkflow(env, {
      runId,
      topicName: topic.name,
      storyTitle: story.title,
      storyLink: story.link,
      storySummary: story.summary,
      aspectRatio: topic.aspect_ratio,
      voiceId: topic.voice_id,
      webhookUrl: `${env.APP_URL}/api/webhook/render-complete`,
    });
  } catch (err) {
    await env.DB.prepare(`UPDATE runs SET status = 'failed', error_message = ?, updated_at = datetime('now') WHERE id = ?`)
      .bind(err instanceof Error ? err.message : String(err), runId)
      .run();
    throw err;
  }

  return runId;
}

/** Runs every active topic. Called by the daily cron. */
export async function runAllActiveTopics(env: Env): Promise<void> {
  const { results } = await env.DB.prepare(`SELECT * FROM topics WHERE active = 1`).all<Topic>();
  for (const topic of results) {
    try {
      await runTopic(env, topic);
    } catch (err) {
      console.error(`Failed to start run for topic ${topic.name}:`, err);
    }
  }
}

/** Flips a run's video to public on YouTube and marks it approved. */
export async function approveRun(env: Env, runId: number): Promise<void> {
  const run = await env.DB.prepare(`SELECT * FROM runs WHERE id = ?`).bind(runId).first<{
    youtube_video_id: string | null;
    status: string;
  }>();

  if (!run || !run.youtube_video_id) {
    throw new Error(`Run ${runId} has no uploaded YouTube video yet`);
  }

  await approveVideo(env, run.youtube_video_id);

  await env.DB.prepare(`UPDATE runs SET status = 'approved', updated_at = datetime('now') WHERE id = ?`)
    .bind(runId)
    .run();
}
