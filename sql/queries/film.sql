-- One film's latest price at each threshold (arg: film, e.g. DUNE).
-- Prices are stored 0-1; shown in cents like Kalshi. Each market's own latest
-- snapshot (correlated subquery), since ts is per market, not per pass.
SELECT m.threshold,
       m.ticker,
       CAST(ROUND(p.yes_bid * 100) AS INTEGER) AS bid_c,
       CAST(ROUND(p.yes_ask * 100) AS INTEGER) AS ask_c,
       p.yes_ask_size         AS ask_qty,
       p.volume,
       p.ts
FROM price_snapshots p
JOIN markets m USING (ticker)
WHERE m.event_ticker = :arg
  AND p.ts = (SELECT MAX(ts) FROM price_snapshots WHERE ticker = p.ticker)
ORDER BY m.threshold;
