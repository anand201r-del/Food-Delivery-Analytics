-- =====================================================================
--  Food Delivery Platform - Reporting Views (for managers & dashboards)
--  File   : 06_views.sql
--  Phase  : 3/4 - ready-made reports; Power BI / Tableau read these views
--  Engine : MySQL 8.0+ (tested on MySQL 9.6)
--  Author : Anand Kumar
--
--  vw_order_details          one row per order with everything joined (BI fact table)
--  vw_daily_city_revenue     daily orders / revenue / cancel % per state and city
--  vw_monthly_kpis           monthly KPIs with month-over-month growth
--  vw_restaurant_performance revenue, rating, delivery speed per restaurant
--  vw_partner_performance    deliveries, avg minutes, late %, rating per partner
--  vw_customer_360           lifetime value + Active / At risk / Churned segment
--  vw_coupon_performance     usage, discount cost, revenue per rupee of discount
-- =====================================================================
USE food_delivery;

-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW vw_order_details AS
SELECT o.order_id,
       DATE(o.order_time)                                   AS order_date,
       DATE_FORMAT(o.order_time, '%Y-%m')                   AS order_month,
       HOUR(o.order_time)                                   AS order_hour,
       DAYNAME(o.order_time)                                AS day_name,
       DAYOFWEEK(o.order_time) IN (1, 7)                    AS is_weekend,
       ci.state,
       ci.city_name,
       z.zone_name                                          AS restaurant_zone,
       r.restaurant_id,
       r.restaurant_name,
       r.cuisine_type,
       o.customer_id,
       o.partner_id,
       dp.vehicle_type,
       o.status,
       cp.coupon_code,
       o.item_total,
       o.discount_amount,
       o.delivery_fee,
       o.final_amount,
       p.payment_method,
       p.payment_status,
       o.promised_minutes,
       ROUND(TIMESTAMPDIFF(SECOND, o.order_time, o.delivered_time) / 60, 1) AS delivery_minutes,
       CASE WHEN o.delivered_time IS NULL THEN NULL
            ELSE TIMESTAMPDIFF(SECOND, o.order_time, o.delivered_time) / 60 > o.promised_minutes
       END                                                  AS is_late
FROM orders o
JOIN restaurants r            ON r.restaurant_id = o.restaurant_id
JOIN zones z                  ON z.zone_id = r.zone_id
JOIN cities ci                ON ci.city_id = z.city_id
LEFT JOIN delivery_partners dp ON dp.partner_id = o.partner_id
LEFT JOIN coupons cp          ON cp.coupon_id = o.coupon_id
LEFT JOIN payments p          ON p.order_id = o.order_id;

-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW vw_daily_city_revenue AS
SELECT DATE(o.order_time)                                        AS order_date,
       ci.state,
       ci.city_name,
       COUNT(*)                                                  AS total_orders,
       SUM(o.status = 'DELIVERED')                               AS delivered_orders,
       ROUND(100 * AVG(o.status = 'CANCELLED'), 2)               AS cancel_rate_pct,
       ROUND(SUM(CASE WHEN o.status = 'DELIVERED' THEN o.final_amount ELSE 0 END), 2) AS revenue,
       ROUND(AVG(CASE WHEN o.status = 'DELIVERED' THEN o.final_amount END), 2)         AS avg_order_value,
       ROUND(SUM(o.discount_amount), 2)                          AS discount_given
FROM orders o
JOIN restaurants r ON r.restaurant_id = o.restaurant_id
JOIN zones z       ON z.zone_id = r.zone_id
JOIN cities ci     ON ci.city_id = z.city_id
GROUP BY DATE(o.order_time), ci.state, ci.city_name;

-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW vw_monthly_kpis AS
WITH firsts AS (
    SELECT customer_id, DATE_FORMAT(MIN(order_time), '%Y-%m') AS first_month
    FROM orders GROUP BY customer_id
), m AS (
    SELECT DATE_FORMAT(o.order_time, '%Y-%m')                                     AS month,
           COUNT(*)                                                               AS total_orders,
           SUM(o.status = 'DELIVERED')                                            AS delivered_orders,
           SUM(CASE WHEN o.status = 'DELIVERED' THEN o.final_amount ELSE 0 END)   AS revenue,
           COUNT(DISTINCT o.customer_id)                                          AS active_customers,
           SUM(o.discount_amount)                                                 AS discount_given
    FROM orders o
    GROUP BY DATE_FORMAT(o.order_time, '%Y-%m')
)
SELECT m.month,
       m.total_orders,
       m.delivered_orders,
       ROUND(m.revenue, 2)                                                AS revenue,
       ROUND(m.revenue / m.delivered_orders, 2)                           AS avg_order_value,
       m.active_customers,
       (SELECT COUNT(*) FROM firsts f WHERE f.first_month = m.month)      AS new_customers,
       ROUND(m.discount_given, 2)                                         AS discount_given,
       ROUND(100 * (m.revenue - LAG(m.revenue) OVER (ORDER BY m.month))
                  / LAG(m.revenue) OVER (ORDER BY m.month), 1)            AS mom_revenue_growth_pct
FROM m;

-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW vw_restaurant_performance AS
SELECT r.restaurant_id,
       r.restaurant_name,
       ci.city_name,
       z.zone_name,
       r.cuisine_type,
       r.is_veg_only,
       r.avg_rating,
       r.total_ratings,
       COUNT(o.order_id)                                              AS total_orders,
       ROUND(100 * AVG(o.status = 'CANCELLED'), 2)                    AS cancel_rate_pct,
       ROUND(SUM(CASE WHEN o.status = 'DELIVERED' THEN o.final_amount ELSE 0 END), 2) AS revenue,
       ROUND(AVG(CASE WHEN o.status = 'DELIVERED' THEN o.final_amount END), 2)         AS avg_order_value,
       ROUND(AVG(TIMESTAMPDIFF(SECOND, o.order_time, o.delivered_time) / 60), 1)      AS avg_delivery_minutes,
       ROUND(100 * AVG(TIMESTAMPDIFF(SECOND, o.order_time, o.delivered_time) / 60 > o.promised_minutes), 1) AS late_pct
FROM restaurants r
JOIN zones z       ON z.zone_id = r.zone_id
JOIN cities ci     ON ci.city_id = z.city_id
LEFT JOIN orders o ON o.restaurant_id = r.restaurant_id
GROUP BY r.restaurant_id, r.restaurant_name, ci.city_name, z.zone_name, r.cuisine_type,
         r.is_veg_only, r.avg_rating, r.total_ratings;

-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW vw_partner_performance AS
SELECT p.partner_id,
       p.full_name,
       ci.city_name,
       p.vehicle_type,
       COUNT(o.order_id)                                                         AS deliveries,
       ROUND(AVG(TIMESTAMPDIFF(SECOND, o.order_time, o.delivered_time) / 60), 1) AS avg_delivery_minutes,
       ROUND(100 * AVG(TIMESTAMPDIFF(SECOND, o.order_time, o.delivered_time) / 60 > o.promised_minutes), 1) AS late_pct,
       ROUND(AVG(rt.delivery_rating), 2)                                         AS avg_delivery_rating
FROM delivery_partners p
JOIN zones z   ON z.zone_id = p.zone_id
JOIN cities ci ON ci.city_id = z.city_id
LEFT JOIN orders o   ON o.partner_id = p.partner_id AND o.status = 'DELIVERED'
LEFT JOIN ratings rt ON rt.order_id = o.order_id
GROUP BY p.partner_id, p.full_name, ci.city_name, p.vehicle_type;

-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW vw_customer_360 AS
WITH agg AS (
    SELECT customer_id,
           COUNT(*)                                                             AS total_orders,
           SUM(CASE WHEN status = 'DELIVERED' THEN final_amount ELSE 0 END)     AS lifetime_value,
           MIN(order_time)                                                      AS first_order,
           MAX(order_time)                                                      AS last_order
    FROM orders GROUP BY customer_id
), data_end AS (
    SELECT MAX(order_time) AS end_time FROM orders
)
SELECT cu.customer_id,
       cu.full_name,
       ci.city_name,
       cu.signup_date,
       COALESCE(a.total_orders, 0)                       AS total_orders,
       ROUND(COALESCE(a.lifetime_value, 0), 2)           AS lifetime_value,
       ROUND(a.lifetime_value / a.total_orders, 2)       AS avg_order_value,
       DATE(a.first_order)                               AS first_order_date,
       DATE(a.last_order)                                AS last_order_date,
       DATEDIFF(d.end_time, a.last_order)                AS days_since_last_order,
       CASE WHEN a.customer_id IS NULL                      THEN 'Never ordered'
            WHEN DATEDIFF(d.end_time, a.last_order) <= 30   THEN 'Active'
            WHEN DATEDIFF(d.end_time, a.last_order) <= 90   THEN 'At risk'
            ELSE 'Churned' END                           AS segment
FROM customers cu
JOIN zones z   ON z.zone_id = cu.zone_id
JOIN cities ci ON ci.city_id = z.city_id
LEFT JOIN agg a ON a.customer_id = cu.customer_id
CROSS JOIN data_end d;

-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW vw_coupon_performance AS
SELECT c.coupon_code,
       c.discount_type,
       c.discount_value,
       c.min_order_value,
       c.valid_from,
       c.valid_to,
       COUNT(o.order_id)                                             AS orders_used,
       ROUND(COALESCE(SUM(o.discount_amount), 0), 2)                 AS total_discount,
       ROUND(COALESCE(SUM(o.final_amount), 0), 2)                    AS revenue,
       ROUND(AVG(o.item_total), 2)                                   AS avg_basket,
       ROUND(SUM(o.final_amount) / NULLIF(SUM(o.discount_amount), 0), 2) AS revenue_per_rupee_discount
FROM coupons c
LEFT JOIN orders o ON o.coupon_id = c.coupon_id AND o.status = 'DELIVERED'
GROUP BY c.coupon_id, c.coupon_code, c.discount_type, c.discount_value, c.min_order_value,
         c.valid_from, c.valid_to;


-- =====================================================================
-- Try them
-- =====================================================================
SELECT * FROM vw_monthly_kpis ORDER BY month;
SELECT * FROM vw_daily_city_revenue WHERE order_date = '2026-09-27' ORDER BY revenue DESC;
SELECT state, ROUND(SUM(revenue)) AS revenue, SUM(total_orders) AS orders
FROM vw_daily_city_revenue GROUP BY state ORDER BY revenue DESC;
SELECT * FROM vw_restaurant_performance ORDER BY revenue DESC LIMIT 10;
SELECT * FROM vw_partner_performance WHERE deliveries >= 50 ORDER BY late_pct LIMIT 10;
SELECT segment, COUNT(*) AS customers, ROUND(AVG(lifetime_value)) AS avg_ltv
FROM vw_customer_360 GROUP BY segment ORDER BY customers DESC;
SELECT * FROM vw_coupon_performance ORDER BY revenue_per_rupee_discount DESC;
SELECT * FROM vw_order_details LIMIT 20;
