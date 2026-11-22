-- Films with Kalshi markets: thresholds per film and when each was first seen.
SELECT event_ticker,
       COUNT(*)           AS thresholds,
       MIN(kalshi_title)  AS title,
       MIN(first_seen_at) AS first_seen
FROM markets
GROUP BY event_ticker
ORDER BY event_ticker;
