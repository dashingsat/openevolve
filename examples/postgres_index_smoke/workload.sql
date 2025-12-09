-- Representative OLTP-style join + filter workload for smoke tests
SELECT
    c.id AS customer_id,
    c.segment,
    c.region,
    o.id AS order_id,
    o.created_at,
    o.status,
    SUM((oi.quantity * oi.price_cents) - oi.discount_cents) AS gross_cents
FROM orders o
JOIN customers c ON c.id = o.customer_id
JOIN order_items oi ON oi.order_id = o.id
WHERE o.created_at >= NOW() - INTERVAL '30 days'
  AND o.status IN ('shipped', 'processing')
  AND c.segment IN ('enterprise', 'midmarket')
GROUP BY c.id, c.segment, c.region, o.id, o.created_at, o.status
HAVING SUM(oi.quantity) > 3
ORDER BY o.created_at DESC, gross_cents DESC
LIMIT 50;

