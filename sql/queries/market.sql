-- One market's price history, newest first (arg: ticker, e.g. KXRT-DUNE-80).
SELECT ts,
       CAST(ROUND(yes_bid * 100) AS INTEGER) AS bid_c,
       CAST(ROUND(yes_ask * 100) AS INTEGER) AS ask_c,
       yes_bid_size         AS bid_qty,
       yes_ask_size         AS ask_qty,
       volume,
       open_interest
FROM price_snapshots
WHERE ticker = :arg
ORDER BY ts DESC
LIMIT 50;
