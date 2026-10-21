-- =====================================================================
--  Food Delivery Platform - Business Analysis Queries
--  File   : 03_analysis_queries.sql
--  Phase  : 2 - 34 business questions answered with SQL
--  Engine : MySQL 8.0+ (tested on MySQL 9.6)
--  Author : Anand Kumar
--
--  Definitions used everywhere:
--    Revenue / GMV   = final_amount of DELIVERED orders (what customers paid)
--    Late delivery   = delivered later than promised_minutes after order_time
--    Data window     = 1 Jan 2025 - 30 Sep 2026
--
--  SQL topics covered: JOINs, GROUP BY / HAVING, CASE, subqueries, CTEs,
--  window functions (RANK, DENSE_RANK, ROW_NUMBER, NTILE, LAG, SUM OVER),
--  date functions, conditional aggregation (pivot), anti-joins.
-- =====================================================================
USE food_delivery;


-- =====================================================================
-- SECTION A. REVENUE & GROWTH
-- =====================================================================

-- Q1. Headline KPIs for the whole business
-- Technique: conditional aggregation
SELECT
    COUNT(*)                                                        AS total_orders,
    SUM(status = 'DELIVERED')                                       AS delivered_orders,
    ROUND(100 * AVG(status = 'CANCELLED'), 2)                       AS cancel_rate_pct,
    ROUND(SUM(CASE WHEN status = 'DELIVERED' THEN final_amount END)) AS gmv_rupees,
    ROUND(AVG(CASE WHEN status = 'DELIVERED' THEN final_amount END), 2) AS avg_order_value,
    COUNT(DISTINCT customer_id)                                     AS active_customers,
    ROUND(SUM(discount_amount))                                     AS total_discount_given
FROM orders;


-- Q2. Month-over-month revenue growth
-- Technique: CTE + LAG() window function
WITH monthly AS (
    SELECT DATE_FORMAT(order_time, '%Y-%m') AS month,
           COUNT(*)                         AS orders,
           ROUND(SUM(final_amount))         AS revenue
    FROM orders
    WHERE status = 'DELIVERED'
    GROUP BY month
)
SELECT month,
       orders,
       revenue,
       LAG(revenue) OVER (ORDER BY month)                                   AS prev_month_revenue,
       ROUND(100 * (revenue - LAG(revenue) OVER (ORDER BY month))
                  / LAG(revenue) OVER (ORDER BY month), 1)                  AS mom_growth_pct
FROM monthly
ORDER BY month;


-- Q3. Revenue by city and each city's share of total revenue
-- Technique: window SUM() OVER () for the grand total
SELECT c.city_name,
       COUNT(*)                                                   AS orders,
       ROUND(SUM(o.final_amount))                                 AS revenue,
       ROUND(100 * SUM(o.final_amount) / SUM(SUM(o.final_amount)) OVER (), 1) AS revenue_share_pct,
       ROUND(AVG(o.final_amount), 2)                              AS avg_order_value
FROM orders o
JOIN restaurants r ON r.restaurant_id = o.restaurant_id
JOIN zones z       ON z.zone_id = r.zone_id
JOIN cities c      ON c.city_id = z.city_id
WHERE o.status = 'DELIVERED'
GROUP BY c.city_name
ORDER BY revenue DESC;


-- Q4. Top 10 restaurants by revenue in each city
-- Technique: RANK() OVER (PARTITION BY city)
WITH rest_rev AS (
    SELECT c.city_name,
           r.restaurant_name,
           r.cuisine_type,
           r.avg_rating,
           ROUND(SUM(o.final_amount)) AS revenue,
           RANK() OVER (PARTITION BY c.city_id ORDER BY SUM(o.final_amount) DESC) AS city_rank
    FROM orders o
    JOIN restaurants r ON r.restaurant_id = o.restaurant_id
    JOIN zones z       ON z.zone_id = r.zone_id
    JOIN cities c      ON c.city_id = z.city_id
    WHERE o.status = 'DELIVERED'
    GROUP BY c.city_id, c.city_name, r.restaurant_id, r.restaurant_name, r.cuisine_type, r.avg_rating
)
SELECT city_name, city_rank, restaurant_name, cuisine_type, avg_rating, revenue
FROM rest_rev
WHERE city_rank <= 10
ORDER BY city_name, city_rank;


-- Q5. Running (cumulative) revenue within each year
-- Technique: SUM() OVER (PARTITION BY year ORDER BY month)
WITH monthly AS (
    SELECT YEAR(order_time) AS yr, MONTH(order_time) AS mon, SUM(final_amount) AS revenue
    FROM orders
    WHERE status = 'DELIVERED'
    GROUP BY yr, mon
)
SELECT yr, mon,
       ROUND(revenue)                                                   AS revenue,
       ROUND(SUM(revenue) OVER (PARTITION BY yr ORDER BY mon))          AS ytd_revenue
FROM monthly
ORDER BY yr, mon;


-- Q6. Which cuisines bring the most money, and their basket size
-- Technique: JOIN + GROUP BY + subquery for items per order
SELECT r.cuisine_type,
       COUNT(*)                          AS orders,
       ROUND(SUM(o.final_amount))        AS revenue,
       ROUND(AVG(o.final_amount), 2)     AS avg_order_value,
       ROUND(AVG(oi.items_in_order), 2)  AS avg_items_per_order
FROM orders o
JOIN restaurants r ON r.restaurant_id = o.restaurant_id
JOIN (SELECT order_id, SUM(quantity) AS items_in_order
      FROM order_items GROUP BY order_id) oi ON oi.order_id = o.order_id
WHERE o.status = 'DELIVERED'
GROUP BY r.cuisine_type
ORDER BY revenue DESC;


-- Q7. Weekday vs weekend - average orders per day
-- Technique: two-level aggregation (per day, then per day type)
SELECT day_type,
       COUNT(*)                 AS days,
       ROUND(AVG(orders), 1)    AS avg_orders_per_day,
       ROUND(AVG(revenue))      AS avg_revenue_per_day
FROM (
    SELECT DATE(order_time) AS d,
           CASE WHEN DAYOFWEEK(order_time) IN (1, 7) THEN 'Weekend (Sat-Sun)'
                WHEN DAYOFWEEK(order_time) = 6       THEN 'Friday'
                ELSE 'Weekday (Mon-Thu)' END AS day_type,
           COUNT(*)          AS orders,
           SUM(final_amount) AS revenue
    FROM orders
    GROUP BY d, day_type
) daily
GROUP BY day_type
ORDER BY avg_orders_per_day DESC;


-- =====================================================================
-- SECTION B. TIME & LOCATION PATTERNS
-- =====================================================================

-- Q8. Peak ordering hours
-- Technique: GROUP BY HOUR() + share of total with window function
SELECT hour_of_day,
       COUNT(*)                                         AS orders,
       ROUND(100 * COUNT(*) / SUM(COUNT(*)) OVER (), 2) AS pct_of_orders,
       CASE WHEN hour_of_day BETWEEN 12 AND 14 THEN 'Lunch peak'
            WHEN hour_of_day BETWEEN 19 AND 22 THEN 'Dinner peak'
            ELSE '' END                                 AS slot
FROM (SELECT HOUR(order_time) AS hour_of_day FROM orders) h
GROUP BY hour_of_day
ORDER BY hour_of_day;


-- Q9. The 10 busiest day-of-week + hour slots (staffing planning)
SELECT DAYNAME(order_time) AS day_name,
       HOUR(order_time)    AS hour_of_day,
       COUNT(*)            AS orders
FROM orders
GROUP BY DAYOFWEEK(order_time), day_name, hour_of_day
ORDER BY orders DESC
LIMIT 10;


-- Q10. Busiest delivery zones, ranked inside each city
-- Technique: DENSE_RANK() OVER (PARTITION BY city)
SELECT c.city_name,
       z.zone_name,
       COUNT(*) AS orders,
       DENSE_RANK() OVER (PARTITION BY c.city_id ORDER BY COUNT(*) DESC) AS zone_rank_in_city
FROM orders o
JOIN customers cu ON cu.customer_id = o.customer_id
JOIN zones z      ON z.zone_id = cu.zone_id
JOIN cities c     ON c.city_id = z.city_id
GROUP BY c.city_id, c.city_name, z.zone_id, z.zone_name
ORDER BY orders DESC
LIMIT 15;


-- Q11. Top 10 busiest days ever (festivals and holidays show up here)
SELECT DATE(order_time)          AS order_date,
       DAYNAME(order_time)       AS day_name,
       COUNT(*)                  AS orders,
       ROUND(SUM(final_amount))  AS revenue
FROM orders
GROUP BY order_date, day_name
ORDER BY orders DESC
LIMIT 10;


-- =====================================================================
-- SECTION C. CUSTOMERS - CHURN, RETENTION, LIFETIME VALUE
-- =====================================================================

-- Q12. Customers who ordered exactly once and never came back
-- Technique: HAVING on a grouped subquery
SELECT COUNT(*) AS one_time_customers,
       ROUND(100 * COUNT(*) / (SELECT COUNT(DISTINCT customer_id) FROM orders), 1) AS pct_of_ordering_customers
FROM (
    SELECT customer_id
    FROM orders
    GROUP BY customer_id
    HAVING COUNT(*) = 1
) once;


-- Q13. Churned customers: no order in the last 90 days of the data
-- Technique: CTE + DATEDIFF against the last date in the dataset
WITH last_order AS (
    SELECT customer_id, MAX(order_time) AS last_order_time, COUNT(*) AS orders
    FROM orders
    GROUP BY customer_id
), data_end AS (
    SELECT MAX(order_time) AS end_time FROM orders
)
SELECT CASE WHEN DATEDIFF(d.end_time, l.last_order_time) <= 30 THEN '1. Active (<=30 days)'
            WHEN DATEDIFF(d.end_time, l.last_order_time) <= 90 THEN '2. At risk (31-90 days)'
            ELSE '3. Churned (>90 days)' END                     AS status,
       COUNT(*)                                                  AS customers,
       ROUND(100 * COUNT(*) / SUM(COUNT(*)) OVER (), 1)          AS pct,
       ROUND(AVG(l.orders), 1)                                   AS avg_lifetime_orders
FROM last_order l CROSS JOIN data_end d
GROUP BY status
ORDER BY status;


-- Q14. Repeat-order rate
SELECT COUNT(*)                                       AS ordering_customers,
       SUM(orders >= 2)                               AS repeat_customers,
       ROUND(100 * AVG(orders >= 2), 1)               AS repeat_rate_pct,
       ROUND(100 * AVG(orders >= 5), 1)               AS pct_with_5_plus_orders
FROM (SELECT customer_id, COUNT(*) AS orders FROM orders GROUP BY customer_id) t;


-- Q15. Customer lifetime value (CLV) - top 20 customers
-- Technique: multi-step CTE
WITH cust AS (
    SELECT o.customer_id,
           COUNT(*)                                  AS orders,
           SUM(o.final_amount)                       AS total_spend,
           MIN(o.order_time)                         AS first_order,
           MAX(o.order_time)                         AS last_order
    FROM orders o
    WHERE o.status = 'DELIVERED'
    GROUP BY o.customer_id
), clv AS (
    SELECT c.*,
           GREATEST(DATEDIFF(last_order, first_order), 1) / 30.0 AS active_months,
           total_spend / orders                                  AS avg_order_value
    FROM cust c
)
SELECT cu.full_name,
       ci.city_name,
       clv.orders,
       ROUND(clv.total_spend)                     AS lifetime_value,
       ROUND(clv.avg_order_value, 2)              AS avg_order_value,
       ROUND(clv.orders / clv.active_months, 2)   AS orders_per_month,
       DATE(clv.first_order)                      AS first_order,
       DATE(clv.last_order)                       AS last_order
FROM clv
JOIN customers cu ON cu.customer_id = clv.customer_id
JOIN zones z      ON z.zone_id = cu.zone_id
JOIN cities ci    ON ci.city_id = z.city_id
ORDER BY lifetime_value DESC
LIMIT 20;


-- Q16. Customer value segments (quartiles by spend)
-- Technique: NTILE(4) window function
WITH spend AS (
    SELECT customer_id, COUNT(*) AS orders, SUM(final_amount) AS total_spend
    FROM orders
    WHERE status = 'DELIVERED'
    GROUP BY customer_id
), seg AS (
    SELECT *, NTILE(4) OVER (ORDER BY total_spend DESC) AS quartile FROM spend
)
SELECT CASE quartile WHEN 1 THEN 'Q1 - Top 25%' WHEN 2 THEN 'Q2' WHEN 3 THEN 'Q3'
                     ELSE 'Q4 - Bottom 25%' END                     AS segment,
       COUNT(*)                                                     AS customers,
       ROUND(AVG(orders), 1)                                        AS avg_orders,
       ROUND(AVG(total_spend))                                      AS avg_lifetime_value,
       ROUND(100 * SUM(total_spend) / SUM(SUM(total_spend)) OVER (), 1) AS pct_of_revenue
FROM seg
GROUP BY quartile
ORDER BY quartile;


-- Q17. Monthly cohort retention: of customers whose first order was in
--      month M, what % ordered again 1, 2 and 3 months later?
-- Technique: CTE + PERIOD_DIFF + conditional aggregation (pivot)
WITH first_order AS (
    SELECT customer_id, MIN(DATE_FORMAT(order_time, '%Y%m')) AS cohort
    FROM orders GROUP BY customer_id
), activity AS (
    SELECT DISTINCT o.customer_id, f.cohort,
           PERIOD_DIFF(DATE_FORMAT(o.order_time, '%Y%m'), f.cohort) AS month_no
    FROM orders o JOIN first_order f ON f.customer_id = o.customer_id
)
SELECT CONCAT(LEFT(cohort, 4), '-', RIGHT(cohort, 2))                       AS cohort_month,
       SUM(month_no = 0)                                                    AS new_customers,
       ROUND(100 * SUM(month_no = 1) / SUM(month_no = 0), 1)                AS m1_retention_pct,
       ROUND(100 * SUM(month_no = 2) / SUM(month_no = 0), 1)                AS m2_retention_pct,
       ROUND(100 * SUM(month_no = 3) / SUM(month_no = 0), 1)                AS m3_retention_pct
FROM activity
GROUP BY cohort
ORDER BY cohort;


-- Q18. Customers who signed up but never placed an order
-- Technique: anti-join (LEFT JOIN ... IS NULL)
SELECT ci.city_name,
       COUNT(*) AS never_ordered
FROM customers cu
LEFT JOIN orders o ON o.customer_id = cu.customer_id
JOIN zones z       ON z.zone_id = cu.zone_id
JOIN cities ci     ON ci.city_id = z.city_id
WHERE o.order_id IS NULL
GROUP BY ci.city_name
ORDER BY never_ordered DESC;


-- =====================================================================
-- SECTION D. DELIVERY PERFORMANCE
-- =====================================================================

-- Q19. Average delivery time and late % per delivery partner
--      (partners with at least 50 deliveries, fastest first)
WITH d AS (
    SELECT partner_id,
           TIMESTAMPDIFF(SECOND, order_time, delivered_time) / 60.0 AS minutes,
           promised_minutes
    FROM orders
    WHERE status = 'DELIVERED'
)
SELECT p.partner_id,
       p.full_name,
       p.vehicle_type,
       COUNT(*)                                     AS deliveries,
       ROUND(AVG(d.minutes), 1)                     AS avg_delivery_min,
       ROUND(100 * AVG(d.minutes > d.promised_minutes), 1) AS late_pct,
       RANK() OVER (ORDER BY AVG(d.minutes))        AS speed_rank
FROM d
JOIN delivery_partners p ON p.partner_id = d.partner_id
GROUP BY p.partner_id, p.full_name, p.vehicle_type
HAVING COUNT(*) >= 50
ORDER BY speed_rank
LIMIT 20;


-- Q20. Late-delivery % by vehicle type
SELECT p.vehicle_type,
       COUNT(*)                                                        AS deliveries,
       ROUND(AVG(TIMESTAMPDIFF(SECOND, o.order_time, o.delivered_time) / 60), 1) AS avg_minutes,
       ROUND(100 * AVG(TIMESTAMPDIFF(SECOND, o.order_time, o.delivered_time) / 60
                       > o.promised_minutes), 1)                       AS late_pct
FROM orders o
JOIN delivery_partners p ON p.partner_id = o.partner_id
WHERE o.status = 'DELIVERED'
GROUP BY p.vehicle_type
ORDER BY late_pct;


-- Q21. Late-delivery % by hour - do peak hours run late?
SELECT HOUR(order_time) AS hour_of_day,
       COUNT(*)         AS deliveries,
       ROUND(100 * AVG(TIMESTAMPDIFF(SECOND, order_time, delivered_time) / 60
                       > promised_minutes), 1) AS late_pct
FROM orders
WHERE status = 'DELIVERED'
GROUP BY hour_of_day
ORDER BY hour_of_day;


-- Q22. Monsoon effect: late % by month in three cities with different rainy seasons
-- (Mumbai: June-September, Chennai: October-December north-east monsoon, Delhi: July-August)
-- Technique: conditional aggregation; NULLIF avoids divide-by-zero
SELECT DATE_FORMAT(o.order_time, '%Y-%m') AS month,
       ROUND(100 * SUM(ci.city_name = 'Mumbai'  AND TIMESTAMPDIFF(SECOND, o.order_time, o.delivered_time) / 60 > o.promised_minutes)
                 / NULLIF(SUM(ci.city_name = 'Mumbai'), 0), 1)  AS mumbai_late_pct,
       ROUND(100 * SUM(ci.city_name = 'Chennai' AND TIMESTAMPDIFF(SECOND, o.order_time, o.delivered_time) / 60 > o.promised_minutes)
                 / NULLIF(SUM(ci.city_name = 'Chennai'), 0), 1) AS chennai_late_pct,
       ROUND(100 * SUM(ci.city_name = 'Delhi'   AND TIMESTAMPDIFF(SECOND, o.order_time, o.delivered_time) / 60 > o.promised_minutes)
                 / NULLIF(SUM(ci.city_name = 'Delhi'), 0), 1)   AS delhi_late_pct
FROM orders o
JOIN restaurants r ON r.restaurant_id = o.restaurant_id
JOIN zones z       ON z.zone_id = r.zone_id
JOIN cities ci     ON ci.city_id = z.city_id
WHERE o.status = 'DELIVERED'
GROUP BY month
ORDER BY month;


-- Q23. Does a late delivery hurt the rating?
SELECT CASE WHEN late_by <= 0  THEN '1. On time'
            WHEN late_by <= 10 THEN '2. Late 1-10 min'
            WHEN late_by <= 20 THEN '3. Late 11-20 min'
            ELSE '4. Late 20+ min' END       AS lateness,
       COUNT(*)                              AS rated_orders,
       ROUND(AVG(delivery_rating), 2)        AS avg_delivery_rating,
       ROUND(AVG(food_rating), 2)            AS avg_food_rating
FROM (
    SELECT r.delivery_rating, r.food_rating,
           TIMESTAMPDIFF(SECOND, o.order_time, o.delivered_time) / 60 - o.promised_minutes AS late_by
    FROM ratings r
    JOIN orders o ON o.order_id = r.order_id
) t
GROUP BY lateness
ORDER BY lateness;


-- =====================================================================
-- SECTION E. COUPONS - DO THEY PAY OFF?
-- =====================================================================

-- Q24. Coupon scorecard: usage, discount cost, revenue per rupee of discount
SELECT c.coupon_code,
       c.discount_type,
       c.discount_value,
       COUNT(o.order_id)                                       AS orders_used,
       ROUND(SUM(o.discount_amount))                           AS total_discount,
       ROUND(SUM(o.final_amount))                              AS revenue,
       ROUND(AVG(o.item_total), 2)                             AS avg_basket,
       ROUND(100 * SUM(o.discount_amount) / SUM(o.item_total), 1) AS discount_pct_of_basket,
       ROUND(SUM(o.final_amount) / SUM(o.discount_amount), 2)  AS revenue_per_rupee_discount
FROM coupons c
LEFT JOIN orders o ON o.coupon_id = c.coupon_id AND o.status = 'DELIVERED'
GROUP BY c.coupon_id, c.coupon_code, c.discount_type, c.discount_value
ORDER BY revenue_per_rupee_discount DESC;


-- Q25. Did limited-time coupons increase orders?
--      Compare average daily orders during the coupon window with the
--      same number of days just before it.
WITH promo AS (
    SELECT coupon_id, coupon_code, valid_from, valid_to,
           DATEDIFF(valid_to, valid_from) + 1 AS days
    FROM coupons
    WHERE DATEDIFF(valid_to, valid_from) < 120          -- seasonal coupons only
), w AS (
    SELECT p.coupon_code, p.days,
           (SELECT COUNT(*) FROM orders o
             WHERE o.order_time >= p.valid_from
               AND o.order_time <  p.valid_to + INTERVAL 1 DAY)                 AS orders_during,
           (SELECT COUNT(*) FROM orders o
             WHERE o.order_time >= p.valid_from - INTERVAL p.days DAY
               AND o.order_time <  p.valid_from)                                AS orders_before,
           (SELECT COALESCE(SUM(o.discount_amount), 0) FROM orders o
             WHERE o.coupon_id = p.coupon_id AND o.status = 'DELIVERED')        AS discount_cost
    FROM promo p
)
SELECT coupon_code,
       days,
       ROUND(orders_before / days, 1)                                AS daily_orders_before,
       ROUND(orders_during / days, 1)                                AS daily_orders_during,
       ROUND(100 * (orders_during - orders_before) / orders_before, 1) AS lift_pct,
       orders_during - orders_before                                 AS extra_orders,
       ROUND(discount_cost)                                          AS discount_cost,
       ROUND(discount_cost / NULLIF(orders_during - orders_before, 0), 2) AS cost_per_extra_order,
       CASE WHEN orders_during - orders_before <= 0 THEN 'Lost money - no lift'
            WHEN discount_cost / (orders_during - orders_before) > 100 THEN 'Expensive lift'
            ELSE 'Worked' END                                        AS verdict
FROM w
ORDER BY lift_pct DESC;


-- Q26. Do customers acquired with a coupon come back?
-- Technique: ROW_NUMBER() to find each customer's first order
WITH ranked AS (
    SELECT customer_id, coupon_id,
           ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY order_time) AS rn,
           COUNT(*)     OVER (PARTITION BY customer_id)                     AS total_orders
    FROM orders
)
SELECT CASE WHEN coupon_id IS NULL THEN 'First order without coupon'
            ELSE 'First order with coupon' END       AS acquisition,
       COUNT(*)                                       AS customers,
       ROUND(100 * AVG(total_orders >= 2), 1)         AS came_back_pct,
       ROUND(AVG(total_orders), 2)                    AS avg_lifetime_orders
FROM ranked
WHERE rn = 1
GROUP BY acquisition;


-- =====================================================================
-- SECTION F. MENU, RATINGS & PAYMENTS
-- =====================================================================

-- Q27. Top 3 best-selling dishes in every cuisine
-- Technique: ROW_NUMBER() OVER (PARTITION BY cuisine)
WITH sales AS (
    SELECT r.cuisine_type, m.item_name,
           SUM(oi.quantity)                  AS units_sold,
           ROUND(SUM(oi.quantity * oi.unit_price)) AS item_revenue,
           ROW_NUMBER() OVER (PARTITION BY r.cuisine_type ORDER BY SUM(oi.quantity) DESC) AS rn
    FROM order_items oi
    JOIN menu_items m  ON m.item_id = oi.item_id
    JOIN restaurants r ON r.restaurant_id = m.restaurant_id
    GROUP BY r.cuisine_type, m.item_name
)
SELECT cuisine_type, rn AS position, item_name, units_sold, item_revenue
FROM sales
WHERE rn <= 3
ORDER BY cuisine_type, rn;


-- Q28. "Hidden risk" restaurants: high revenue but low rating
--      (top 20% by revenue in the platform, rating below 3.8)
WITH rev AS (
    SELECT r.restaurant_id, r.restaurant_name, r.cuisine_type, r.avg_rating, r.total_ratings,
           SUM(o.final_amount) AS revenue,
           PERCENT_RANK() OVER (ORDER BY SUM(o.final_amount)) AS revenue_percentile
    FROM restaurants r
    JOIN orders o ON o.restaurant_id = r.restaurant_id AND o.status = 'DELIVERED'
    GROUP BY r.restaurant_id, r.restaurant_name, r.cuisine_type, r.avg_rating, r.total_ratings
)
SELECT restaurant_name, cuisine_type, avg_rating, total_ratings,
       ROUND(revenue) AS revenue,
       ROUND(100 * revenue_percentile) AS revenue_percentile
FROM rev
WHERE revenue_percentile >= 0.80 AND avg_rating < 3.8
ORDER BY revenue DESC;


-- Q29. Payment method mix by city
-- Technique: pivot with conditional aggregation
SELECT ci.city_name,
       ROUND(100 * AVG(p.payment_method = 'UPI'), 1)        AS upi_pct,
       ROUND(100 * AVG(p.payment_method = 'CARD'), 1)       AS card_pct,
       ROUND(100 * AVG(p.payment_method = 'WALLET'), 1)     AS wallet_pct,
       ROUND(100 * AVG(p.payment_method = 'NETBANKING'), 1) AS netbanking_pct,
       ROUND(100 * AVG(p.payment_method = 'COD'), 1)        AS cod_pct,
       ROUND(100 * AVG(p.payment_status = 'REFUNDED'), 2)   AS refunded_pct
FROM payments p
JOIN orders o      ON o.order_id = p.order_id
JOIN restaurants r ON r.restaurant_id = o.restaurant_id
JOIN zones z       ON z.zone_id = r.zone_id
JOIN cities ci     ON ci.city_id = z.city_id
GROUP BY ci.city_name
ORDER BY upi_pct DESC;


-- Q30. Menu items that have never been ordered (candidates to remove)
-- Technique: NOT EXISTS subquery
SELECT r.restaurant_name, m.item_name, m.category, m.price
FROM menu_items m
JOIN restaurants r ON r.restaurant_id = m.restaurant_id
WHERE NOT EXISTS (SELECT 1 FROM order_items oi WHERE oi.item_id = m.item_id)
ORDER BY r.restaurant_name, m.item_name;


-- =====================================================================
-- SECTION F. STATES & REGIONS
-- =====================================================================

-- Q31. Revenue by state: rank, share of India, and how many cities each state has on the app
-- Technique: GROUP BY + DENSE_RANK + share with SUM() OVER ()
SELECT ci.state,
       COUNT(DISTINCT ci.city_id)                                AS cities,
       COUNT(*)                                                  AS delivered_orders,
       ROUND(SUM(o.final_amount))                                AS revenue,
       ROUND(100 * SUM(o.final_amount) / SUM(SUM(o.final_amount)) OVER (), 2) AS share_pct,
       DENSE_RANK() OVER (ORDER BY SUM(o.final_amount) DESC)     AS state_rank
FROM orders o
JOIN restaurants r ON r.restaurant_id = o.restaurant_id
JOIN zones z       ON z.zone_id = r.zone_id
JOIN cities ci     ON ci.city_id = z.city_id
WHERE o.status = 'DELIVERED'
GROUP BY ci.state
ORDER BY state_rank;


-- Q32. Each state's favourite cuisine (by delivered orders) and its share of the state's orders
-- Technique: CTE + ROW_NUMBER() PARTITION BY state
WITH state_cuisine AS (
    SELECT ci.state, r.cuisine_type, COUNT(*) AS orders,
           ROW_NUMBER() OVER (PARTITION BY ci.state ORDER BY COUNT(*) DESC) AS rn,
           SUM(COUNT(*)) OVER (PARTITION BY ci.state)                     AS state_orders
    FROM orders o
    JOIN restaurants r ON r.restaurant_id = o.restaurant_id
    JOIN zones z       ON z.zone_id = r.zone_id
    JOIN cities ci     ON ci.city_id = z.city_id
    WHERE o.status = 'DELIVERED'
    GROUP BY ci.state, r.cuisine_type
)
SELECT state, cuisine_type AS favourite_cuisine, orders,
       ROUND(100 * orders / state_orders, 1) AS share_of_state_pct
FROM state_cuisine
WHERE rn = 1
ORDER BY state_orders DESC;


-- Q33. Big cities vs the rest: order value, cash on delivery, lateness and growth
-- "Top 10" = the 10 cities with the most orders; growth compares Jan-Sep 2026 with Jan-Sep 2025
-- Technique: CTE ranking + CASE bucket + conditional aggregation
WITH city_rank AS (
    SELECT z.city_id, RANK() OVER (ORDER BY COUNT(*) DESC) AS rnk
    FROM orders o
    JOIN restaurants r ON r.restaurant_id = o.restaurant_id
    JOIN zones z       ON z.zone_id = r.zone_id
    GROUP BY z.city_id
), tagged AS (
    SELECT o.*, p.payment_method,
           CASE WHEN cr.rnk <= 10 THEN 'Top 10 cities' ELSE 'Other 39 cities' END AS city_group
    FROM orders o
    JOIN payments p    ON p.order_id = o.order_id
    JOIN restaurants r ON r.restaurant_id = o.restaurant_id
    JOIN zones z       ON z.zone_id = r.zone_id
    JOIN city_rank cr  ON cr.city_id = z.city_id
)
SELECT city_group,
       COUNT(*)                                                         AS orders,
       ROUND(AVG(CASE WHEN status = 'DELIVERED' THEN final_amount END), 0) AS avg_order_value,
       ROUND(100 * AVG(payment_method = 'COD'), 1)                      AS cod_pct,
       ROUND(100 * AVG(CASE WHEN status = 'DELIVERED'
                            THEN TIMESTAMPDIFF(SECOND, order_time, delivered_time) / 60 > promised_minutes END), 1) AS late_pct,
       ROUND(100 * (SUM(order_time >= '2026-01-01' AND order_time < '2026-10-01')
                  / SUM(order_time >= '2025-01-01' AND order_time < '2025-10-01') - 1), 1) AS yoy_growth_pct
FROM tagged
GROUP BY city_group;


-- Q34. Durga Puja effect: orders per day during Puja week (28 Sep - 2 Oct 2025) compared with
-- the same five weekdays two weeks earlier (14 - 18 Sep 2025), for the eastern states that
-- celebrate it, with the rest of India as a control group
-- Technique: CASE grouping + conditional aggregation over date ranges
SELECT CASE WHEN ci.state IN ('West Bengal', 'Assam', 'Odisha', 'Tripura', 'Bihar', 'Jharkhand')
            THEN ci.state ELSE 'Rest of India (control)' END                          AS region,
       ROUND(SUM(o.order_time >= '2025-09-14' AND o.order_time < '2025-09-19') / 5, 1) AS orders_per_day_before,
       ROUND(SUM(o.order_time >= '2025-09-28' AND o.order_time < '2025-10-03') / 5, 1) AS orders_per_day_puja,
       ROUND(100 * (SUM(o.order_time >= '2025-09-28' AND o.order_time < '2025-10-03')
                  / NULLIF(SUM(o.order_time >= '2025-09-14' AND o.order_time < '2025-09-19'), 0) - 1), 1) AS change_pct
FROM orders o
JOIN restaurants r ON r.restaurant_id = o.restaurant_id
JOIN zones z       ON z.zone_id = r.zone_id
JOIN cities ci     ON ci.city_id = z.city_id
WHERE o.order_time >= '2025-09-14' AND o.order_time < '2025-10-03'
GROUP BY region
ORDER BY orders_per_day_before DESC;
