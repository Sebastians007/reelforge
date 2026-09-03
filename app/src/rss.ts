import { XMLParser } from "fast-xml-parser";

export interface Story {
  title: string;
  link: string;
  summary: string;
}

/** Fetches an RSS feed and returns the top story. */
export async function fetchTopStory(feedUrl: string): Promise<Story> {
  const res = await fetch(feedUrl, {
    headers: { "User-Agent": "Reelforge/1.0" },
  });
  if (!res.ok) {
    throw new Error(`RSS fetch failed (${res.status}) for ${feedUrl}`);
  }
  const xml = await res.text();
  const parser = new XMLParser({ ignoreAttributes: false });
  const parsed = parser.parse(xml);

  const items = parsed?.rss?.channel?.item ?? parsed?.feed?.entry;
  const first = Array.isArray(items) ? items[0] : items;
  if (!first) {
    throw new Error(`No items found in RSS feed: ${feedUrl}`);
  }

  const title = String(first.title ?? "").trim();
  const link = typeof first.link === "string" ? first.link : first.link?.["@_href"] ?? "";
  const rawSummary = first.description ?? first.summary ?? first["content:encoded"] ?? "";
  const summary = String(rawSummary).replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();

  return { title, link: String(link).trim(), summary: summary.slice(0, 1000) };
}
