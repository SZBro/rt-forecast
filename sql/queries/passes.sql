-- Snapshots per 15-minute pass (latest 8): did every pass get every market?
SELECT datetime((strftime('%s', ts) / 900) * 900, 'unixepoch') AS pass,
       COUNT(*)               AS snapshots,
       COUNT(DISTINCT ticker) AS markets,
       MIN(ts)                AS started,
       MAX(ts)                AS finished
FROM price_snapshots
GROUP BY pass
ORDER BY pass DESC
LIMIT 8;
