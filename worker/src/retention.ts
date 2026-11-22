/** Delete old media objects in R2. Never starts the container. */

import type { Env } from "./env";

const MEDIA = new Set(["uploads", "downloads", "output", "previews"]);

export async function retainRemoteMedia(env: Env): Promise<{ removed: number }> {
  const days = Math.max(1, Number.parseInt(env.RETENTION_DAYS || "14", 10) || 14);
  const cutoff = Date.now() - days * 86_400_000;
  const stale: string[] = [];
  let cursor: string | undefined;
  do {
    const page = await env.DATA.list({ prefix: "users/", cursor, limit: 1000 });
    for (const obj of page.objects) {
      const parts = obj.key.split("/");
      if (parts.length < 4 || parts[0] !== "users" || !MEDIA.has(parts[2])) continue;
      const uploaded = obj.uploaded ? obj.uploaded.getTime() : 0;
      if (uploaded && uploaded < cutoff) stale.push(obj.key);
    }
    cursor = page.truncated ? page.cursor : undefined;
  } while (cursor);
  let removed = 0;
  for (let i = 0; i < stale.length; i += 1000) {
    const chunk = stale.slice(i, i + 1000);
    await env.DATA.delete(chunk);
    removed += chunk.length;
  }
  return { removed };
}
