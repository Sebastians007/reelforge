export interface RenderJobInput {
  runId: number;
  topicName: string;
  storyTitle: string;
  storyLink: string;
  storySummary: string;
  aspectRatio: string;
  voiceId: string;
  webhookUrl: string;
}

/** Starts the render.py workflow on GitHub Actions via workflow_dispatch. */
export async function triggerRenderWorkflow(
  env: { GITHUB_TOKEN: string; GITHUB_OWNER: string; GITHUB_REPO: string; WEBHOOK_SECRET: string },
  input: RenderJobInput
): Promise<void> {
  const url = `https://api.github.com/repos/${env.GITHUB_OWNER}/${env.GITHUB_REPO}/actions/workflows/make-video.yml/dispatches`;

  const res = await fetch(url, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      Accept: "application/vnd.github+json",
      "Content-Type": "application/json",
      "User-Agent": "Reelforge",
    },
    body: JSON.stringify({
      ref: "main",
      inputs: {
        run_id: String(input.runId),
        topic_name: input.topicName,
        story_title: input.storyTitle,
        story_link: input.storyLink,
        story_summary: input.storySummary,
        aspect_ratio: input.aspectRatio,
        voice_id: input.voiceId,
        webhook_url: input.webhookUrl,
        webhook_secret: env.WEBHOOK_SECRET,
      },
    }),
  });

  if (!res.ok) {
    const body = await res.text();
    throw new Error(`GitHub workflow dispatch failed (${res.status}): ${body}`);
  }
}
