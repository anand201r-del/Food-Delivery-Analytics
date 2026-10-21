-- =====================================================================
--  Food Delivery Platform - Indexing & Query Performance
--  File   : 05_indexes_performance.sql
--  Phase  : 3 - indexes on frequently searched columns, EXPLAIN before/after
--  Engine : MySQL 8.0+ (tested on MySQL 9.6)
--  Author : Anand Kumar
--
--  How to read the results:
--    * EXPLAIN FORMAT=TRADITIONAL -> look at "type", "key" and "rows"
--        type = ALL    : full table scan (reads every row)       -> slow
--        type = range  : uses an index to read only a date range  -> fast
--        type = ref    : index lookup on one value                -> fast
--        Extra = "Using index" : answered from the index alone (covering index)
--    * Workbench Output panel -> "Duration" column shows the run time
--
--  Safe to re-run: part 1 drops the indexes first, so you always get a
--  clean BEFORE / AFTER comparison.
-- =====================================================================
USE food_delivery;

DROP PROCEDURE IF EXISTS sp_drop_index_if_exists;
DELIMITER $$
CREATE PROCEDURE sp_drop_index_if_exists(IN p_table VARCHAR(64), IN p_index VARCHAR(64))
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.statistics
               WHERE table_schema = DATABASE() AND table_name = p_table AND index_name = p_index) THEN
        SET @ddl = CONCAT('ALTER TABLE ', p_table, ' DROP INDEX ', p_index);
        PREPARE stmt FROM @ddl;
        EXECUTE stmt;
        DEALLOCATE PREPARE stmt;
    END IF;
END$$

-- A foreign key always needs an index that starts with its column. When we
-- create (customer_id, order_time), MySQL silently drops the automatic FK
-- index on customer_id and uses ours instead - so ours can then only be
-- dropped if a plain FK index exists again. This helper recreates it.
DROP PROCEDURE IF EXISTS sp_add_index_if_missing$$
CREATE PROCEDURE sp_add_index_if_missing(IN p_table VARCHAR(64), IN p_index VARCHAR(64), IN p_columns VARCHAR(255))
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.statistics
                   WHERE table_schema = DATABASE() AND table_name = p_table AND index_name = p_index) THEN
        SET @ddl = CONCAT('CREATE INDEX ', p_index, ' ON ', p_table, ' (', p_columns, ')');
        PREPARE stmt FROM @ddl;
        EXECUTE stmt;
        DEALLOCATE PREPARE stmt;
    END IF;
END$$
DELIMITER ;


-- =====================================================================
-- PART 1. BEFORE - no extra indexes (only primary keys + foreign keys)
-- =====================================================================
CALL sp_add_index_if_missing('orders', 'idx_fk_orders_customer', 'customer_id');
CALL sp_add_index_if_missing('orders', 'fk_order_restaurant', 'restaurant_id');
CALL sp_add_index_if_missing('menu_items', 'idx_fk_menu_restaurant', 'restaurant_id');
CALL sp_add_index_if_missing('ratings', 'idx_fk_ratings_restaurant', 'restaurant_id');
CALL sp_drop_index_if_exists('orders', 'idx_orders_status_time');
CALL sp_drop_index_if_exists('orders', 'idx_orders_time');
CALL sp_drop_index_if_exists('orders', 'idx_orders_customer_time');
CALL sp_drop_index_if_exists('orders', 'idx_orders_status_restaurant');
CALL sp_drop_index_if_exists('orders', 'idx_orders_restaurant_status');   -- older name
CALL sp_drop_index_if_exists('menu_items', 'idx_menu_restaurant_available');
CALL sp_drop_index_if_exists('delivery_partners', 'idx_partners_available_zone');
CALL sp_drop_index_if_exists('customers', 'idx_customers_signup');
CALL sp_drop_index_if_exists('ratings', 'idx_ratings_restaurant_food');
ANALYZE TABLE orders, menu_items, delivery_partners, customers, ratings;

-- T1. Daily revenue for one month (manager report)
EXPLAIN FORMAT=TRADITIONAL
SELECT DATE(order_time) AS d, COUNT(*) AS orders, SUM(final_amount) AS revenue
FROM orders
WHERE status = 'DELIVERED' AND order_time >= '2026-08-01' AND order_time < '2026-09-01'
GROUP BY d;

SELECT DATE(order_time) AS d, COUNT(*) AS orders, SUM(final_amount) AS revenue
FROM orders
WHERE status = 'DELIVERED' AND order_time >= '2026-08-01' AND order_time < '2026-09-01'
GROUP BY d;

-- T2. Last order date of every customer (churn analysis)
EXPLAIN FORMAT=TRADITIONAL
SELECT customer_id, MAX(order_time) AS last_order FROM orders GROUP BY customer_id;

SELECT customer_id, MAX(order_time) AS last_order FROM orders GROUP BY customer_id;

-- T3. A customer's 10 most recent orders ("My Orders" screen in the app)
EXPLAIN FORMAT=TRADITIONAL
SELECT order_id, order_time, status, final_amount
FROM orders WHERE customer_id = 10468 ORDER BY order_time DESC LIMIT 10;

SELECT order_id, order_time, status, final_amount
FROM orders WHERE customer_id = 10468 ORDER BY order_time DESC LIMIT 10;

-- T4. Revenue of every restaurant (leaderboards)
EXPLAIN FORMAT=TRADITIONAL
SELECT restaurant_id, SUM(final_amount) AS revenue
FROM orders WHERE status = 'DELIVERED' GROUP BY restaurant_id;

SELECT restaurant_id, SUM(final_amount) AS revenue
FROM orders WHERE status = 'DELIVERED' GROUP BY restaurant_id;


-- =====================================================================
-- PART 2. CREATE THE INDEXES
-- =====================================================================
-- Revenue reports always filter on status + a date range; adding
-- final_amount makes it a COVERING index (no need to read the table rows)
CREATE INDEX idx_orders_status_time       ON orders (status, order_time, final_amount);

-- Plain date-range searches (all statuses)
CREATE INDEX idx_orders_time              ON orders (order_time);

-- Customer history, first/last order, churn, CLV
CREATE INDEX idx_orders_customer_time     ON orders (customer_id, order_time);

-- Restaurant leaderboards: WHERE status = ... GROUP BY restaurant_id
-- (status first, then the GROUP BY column, then the summed column = covering,
--  and rows come out already grouped -> no temporary table)
CREATE INDEX idx_orders_status_restaurant ON orders (status, restaurant_id, final_amount);

-- Show a restaurant's available menu
CREATE INDEX idx_menu_restaurant_available ON menu_items (restaurant_id, is_available);

-- sp_assign_partner: find free partners quickly
CREATE INDEX idx_partners_available_zone  ON delivery_partners (is_available, zone_id);

-- Signup-cohort analysis
CREATE INDEX idx_customers_signup         ON customers (signup_date);

-- Rating trigger recalculates AVG(food_rating) per restaurant -> covering
CREATE INDEX idx_ratings_restaurant_food  ON ratings (restaurant_id, food_rating);

-- The new composite indexes now serve these foreign keys, so the plain
-- single-column helper indexes from part 1 are redundant
CALL sp_drop_index_if_exists('orders', 'idx_fk_orders_customer');
CALL sp_drop_index_if_exists('menu_items', 'idx_fk_menu_restaurant');
CALL sp_drop_index_if_exists('ratings', 'idx_fk_ratings_restaurant');

ANALYZE TABLE orders, menu_items, delivery_partners, customers, ratings;


-- =====================================================================
-- PART 3. AFTER - same queries, compare type / key / rows / Duration
-- =====================================================================
EXPLAIN FORMAT=TRADITIONAL
SELECT DATE(order_time) AS d, COUNT(*) AS orders, SUM(final_amount) AS revenue
FROM orders
WHERE status = 'DELIVERED' AND order_time >= '2026-08-01' AND order_time < '2026-09-01'
GROUP BY d;

SELECT DATE(order_time) AS d, COUNT(*) AS orders, SUM(final_amount) AS revenue
FROM orders
WHERE status = 'DELIVERED' AND order_time >= '2026-08-01' AND order_time < '2026-09-01'
GROUP BY d;

EXPLAIN FORMAT=TRADITIONAL
SELECT customer_id, MAX(order_time) AS last_order FROM orders GROUP BY customer_id;

SELECT customer_id, MAX(order_time) AS last_order FROM orders GROUP BY customer_id;

EXPLAIN FORMAT=TRADITIONAL
SELECT order_id, order_time, status, final_amount
FROM orders WHERE customer_id = 10468 ORDER BY order_time DESC LIMIT 10;

SELECT order_id, order_time, status, final_amount
FROM orders WHERE customer_id = 10468 ORDER BY order_time DESC LIMIT 10;

EXPLAIN FORMAT=TRADITIONAL
SELECT restaurant_id, SUM(final_amount) AS revenue
FROM orders WHERE status = 'DELIVERED' GROUP BY restaurant_id;

SELECT restaurant_id, SUM(final_amount) AS revenue
FROM orders WHERE status = 'DELIVERED' GROUP BY restaurant_id;


-- =====================================================================
-- PART 4. List all indexes on the orders table
-- =====================================================================
SELECT index_name,
       GROUP_CONCAT(column_name ORDER BY seq_in_index) AS columns_in_index,
       MAX(non_unique) = 0                             AS is_unique
FROM information_schema.statistics
WHERE table_schema = DATABASE() AND table_name = 'orders'
GROUP BY index_name
ORDER BY index_name;
