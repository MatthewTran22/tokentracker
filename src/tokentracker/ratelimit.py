"""DB-backed rate limiter.

Works on serverless (Vercel) where each cold start is a fresh process —
process-local state would reset on every scale-up. Backing the window in
Postgres makes limits effective across all running instances.
"""

import random


async def check_rate(pool, bucket: str, limit: int, window_seconds: int) -> bool:
    """Return True if the request is allowed; record a hit on success.

    A slim race window between the count and insert is tolerated — at our
    configured limits (single-digit per minute) it doesn't materially weaken
    the defense. Occasional cleanup keeps the table bounded without a cron.
    """
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT count(*) FROM rate_limits
                 WHERE bucket = %s
                   AND hit_at > now() - make_interval(secs => %s)
                """,
                (bucket, window_seconds),
            )
            count = (await cur.fetchone())[0]
            if count >= limit:
                return False
            await cur.execute(
                "INSERT INTO rate_limits (bucket, hit_at) VALUES (%s, now())",
                (bucket,),
            )
            # Opportunistic pruning: ~1% of hits do a cleanup. Bounded growth
            # without needing a separate cron job.
            if random.random() < 0.01:
                await cur.execute(
                    "DELETE FROM rate_limits WHERE hit_at < now() - interval '1 hour'"
                )
    return True
