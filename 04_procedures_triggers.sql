-- =====================================================================
--  Food Delivery Platform - Stored Procedures, Triggers & Transactions
--  File   : 04_procedures_triggers.sql
--  Phase  : 3 - business logic inside the database
--  Engine : MySQL 8.0+ (tested on MySQL 9.6)
--  Author : Anand Kumar
--
--  Run AFTER the data is loaded. Safe to re-run (drops and recreates).
--
--  TRIGGERS
--    trg_orders_after_insert   log the first status of every new order
--    trg_orders_before_update  stamp delivered_time when an order is delivered
--    trg_orders_after_update   log every status change + free the delivery partner
--    trg_ratings_before_insert only the customer of a DELIVERED order can rate it
--    trg_ratings_after_insert  keep restaurants.avg_rating / total_ratings up to date
--    trg_ratings_after_delete  same, when a rating is removed
--    trg_menu_before_update    mark an item unavailable when stock hits 0
--
--  STORED PROCEDURES
--    sp_assign_partner         assign the NEAREST free delivery partner (same city)
--    sp_place_order            ONE TRANSACTION: validate cart, lock stock, apply
--                              coupon, create order + items, reduce stock, record
--                              payment, assign partner -> COMMIT, or ROLLBACK all
--    sp_update_order_status    move an order through its life cycle (rules enforced)
--    sp_cancel_order           ONE TRANSACTION: cancel, restock, refund, free partner
--    sp_customer_summary       quick customer 360 lookup
-- =====================================================================
USE food_delivery;
SET SQL_SAFE_UPDATES = 0;

DROP TRIGGER IF EXISTS trg_orders_after_insert;
DROP TRIGGER IF EXISTS trg_orders_before_update;
DROP TRIGGER IF EXISTS trg_orders_after_update;
DROP TRIGGER IF EXISTS trg_ratings_before_insert;
DROP TRIGGER IF EXISTS trg_ratings_after_insert;
DROP TRIGGER IF EXISTS trg_ratings_after_delete;
DROP TRIGGER IF EXISTS trg_menu_before_update;
DROP PROCEDURE IF EXISTS sp_assign_partner;
DROP PROCEDURE IF EXISTS sp_place_order;
DROP PROCEDURE IF EXISTS sp_update_order_status;
DROP PROCEDURE IF EXISTS sp_cancel_order;
DROP PROCEDURE IF EXISTS sp_customer_summary;

DELIMITER $$

-- =====================================================================
-- TRIGGERS
-- =====================================================================

-- Every new order gets its first entry in the audit log
CREATE TRIGGER trg_orders_after_insert
AFTER INSERT ON orders
FOR EACH ROW
BEGIN
    INSERT INTO order_status_log (order_id, old_status, new_status, changed_at)
    VALUES (NEW.order_id, NULL, NEW.status, NEW.order_time);
END$$

-- Stamp the delivery time automatically
CREATE TRIGGER trg_orders_before_update
BEFORE UPDATE ON orders
FOR EACH ROW
BEGIN
    IF NEW.status = 'DELIVERED' AND OLD.status <> 'DELIVERED' AND NEW.delivered_time IS NULL THEN
        SET NEW.delivered_time = NOW();
    END IF;
END$$

-- Log every status change, and release the partner when the order is finished
CREATE TRIGGER trg_orders_after_update
AFTER UPDATE ON orders
FOR EACH ROW
BEGIN
    IF NOT (OLD.status <=> NEW.status) THEN
        INSERT INTO order_status_log (order_id, old_status, new_status, changed_at)
        VALUES (NEW.order_id, OLD.status, NEW.status, NOW());
    END IF;

    IF NEW.status IN ('DELIVERED', 'CANCELLED')
       AND OLD.status NOT IN ('DELIVERED', 'CANCELLED')
       AND NEW.partner_id IS NOT NULL THEN
        UPDATE delivery_partners SET is_available = TRUE WHERE partner_id = NEW.partner_id;
    END IF;
END$$

-- Business rule: you can only rate your own, delivered order
CREATE TRIGGER trg_ratings_before_insert
BEFORE INSERT ON ratings
FOR EACH ROW
BEGIN
    IF NOT EXISTS (SELECT 1 FROM orders
                   WHERE order_id = NEW.order_id
                     AND customer_id = NEW.customer_id
                     AND restaurant_id = NEW.restaurant_id
                     AND status = 'DELIVERED') THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'Only the customer of a DELIVERED order can rate it';
    END IF;
END$$

-- Keep the restaurant's average rating correct
CREATE TRIGGER trg_ratings_after_insert
AFTER INSERT ON ratings
FOR EACH ROW
BEGIN
    UPDATE restaurants
    SET avg_rating    = (SELECT ROUND(AVG(food_rating), 1) FROM ratings WHERE restaurant_id = NEW.restaurant_id),
        total_ratings = (SELECT COUNT(*)                   FROM ratings WHERE restaurant_id = NEW.restaurant_id)
    WHERE restaurant_id = NEW.restaurant_id;
END$$

CREATE TRIGGER trg_ratings_after_delete
AFTER DELETE ON ratings
FOR EACH ROW
BEGIN
    UPDATE restaurants
    SET avg_rating    = COALESCE((SELECT ROUND(AVG(food_rating), 1) FROM ratings WHERE restaurant_id = OLD.restaurant_id), 0),
        total_ratings = (SELECT COUNT(*) FROM ratings WHERE restaurant_id = OLD.restaurant_id)
    WHERE restaurant_id = OLD.restaurant_id;
END$$

-- Out of stock -> hide the item; restocked -> show it again
CREATE TRIGGER trg_menu_before_update
BEFORE UPDATE ON menu_items
FOR EACH ROW
BEGIN
    IF NEW.stock_qty = 0 THEN
        SET NEW.is_available = FALSE;
    ELSEIF OLD.stock_qty = 0 AND NEW.stock_qty > 0 THEN
        SET NEW.is_available = TRUE;
    END IF;
END$$


-- =====================================================================
-- PROCEDURE: assign the nearest free delivery partner
--   Distance = straight-line distance (ST_Distance_Sphere) between the
--   partner's current location and the restaurant's zone.
--   FOR UPDATE SKIP LOCKED: two orders placed at the same moment can never
--   grab the same partner - the second one simply skips the locked row.
-- =====================================================================
CREATE PROCEDURE sp_assign_partner(IN p_order_id BIGINT, OUT p_partner_id INT)
BEGIN
    DECLARE v_lat  DECIMAL(9,6);
    DECLARE v_lng  DECIMAL(9,6);
    DECLARE v_city INT;

    SET p_partner_id = NULL;

    SELECT z.latitude, z.longitude, z.city_id
      INTO v_lat, v_lng, v_city
    FROM orders o
    JOIN restaurants r ON r.restaurant_id = o.restaurant_id
    JOIN zones z       ON z.zone_id = r.zone_id
    WHERE o.order_id = p_order_id;

    SELECT p.partner_id INTO p_partner_id
    FROM delivery_partners p
    JOIN zones pz ON pz.zone_id = p.zone_id
    WHERE p.is_available = TRUE
      AND pz.city_id = v_city
    ORDER BY ST_Distance_Sphere(POINT(p.current_lng, p.current_lat), POINT(v_lng, v_lat))
    LIMIT 1
    FOR UPDATE OF p SKIP LOCKED;

    IF p_partner_id IS NOT NULL THEN
        UPDATE delivery_partners SET is_available = FALSE WHERE partner_id = p_partner_id;
        UPDATE orders SET partner_id = p_partner_id, status = 'CONFIRMED' WHERE order_id = p_order_id;
    END IF;
END$$


-- =====================================================================
-- PROCEDURE: place an order - everything in ONE transaction
--   p_items example: '[{"item_id": 728, "qty": 2}, {"item_id": 726, "qty": 1}]'
--   If ANY step fails (bad item, not enough stock, invalid coupon, bad
--   payment method...) the whole order is rolled back and p_message says why.
-- =====================================================================
CREATE PROCEDURE sp_place_order(
    IN  p_customer_id    INT,
    IN  p_restaurant_id  INT,
    IN  p_items          JSON,
    IN  p_coupon_code    VARCHAR(20),
    IN  p_payment_method VARCHAR(20),
    OUT p_order_id       BIGINT,
    OUT p_message        VARCHAR(255))
BEGIN
    DECLARE v_item_total DECIMAL(10,2) DEFAULT 0;
    DECLARE v_discount   DECIMAL(10,2) DEFAULT 0;
    DECLARE v_fee        DECIMAL(6,2)  DEFAULT 0;
    DECLARE v_final      DECIMAL(10,2) DEFAULT 0;
    DECLARE v_coupon_id  INT DEFAULT NULL;
    DECLARE v_type       VARCHAR(10);
    DECLARE v_value      DECIMAL(6,2);
    DECLARE v_cap        DECIMAL(8,2);
    DECLARE v_min        DECIMAL(8,2);
    DECLARE v_bad        INT DEFAULT 0;
    DECLARE v_partner    INT DEFAULT NULL;
    DECLARE v_err        VARCHAR(255);

    -- Any error anywhere below -> undo everything
    DECLARE EXIT HANDLER FOR SQLEXCEPTION
    BEGIN
        GET DIAGNOSTICS CONDITION 1 v_err = MESSAGE_TEXT;
        ROLLBACK;
        SET SESSION sql_safe_updates = @fd_safe_mode;
        SET p_order_id = NULL;
        SET p_message  = CONCAT('ROLLED BACK: ', v_err);
    END;

    -- Workbench "safe update mode" would block the multi-table stock UPDATE
    SET @fd_safe_mode = @@SESSION.sql_safe_updates;
    SET SESSION sql_safe_updates = 0;

    SET p_order_id = NULL;
    START TRANSACTION;

    -- 1. Basic checks
    IF NOT EXISTS (SELECT 1 FROM customers WHERE customer_id = p_customer_id AND is_active) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Customer not found or inactive';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM restaurants WHERE restaurant_id = p_restaurant_id AND is_active) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Restaurant not found or closed';
    END IF;
    IF p_items IS NULL OR JSON_LENGTH(p_items) = 0 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Cart is empty';
    END IF;

    -- 2. Every item must be on THIS restaurant's menu, available, with qty > 0
    SELECT COUNT(*) INTO v_bad
    FROM JSON_TABLE(p_items, '$[*]' COLUMNS (item_id INT PATH '$.item_id', qty INT PATH '$.qty')) j
    LEFT JOIN menu_items m
           ON m.item_id = j.item_id AND m.restaurant_id = p_restaurant_id AND m.is_available
    WHERE m.item_id IS NULL OR j.qty IS NULL OR j.qty <= 0;
    IF v_bad > 0 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'An item is not on this menu, unavailable, or has an invalid quantity';
    END IF;

    -- 3. Lock the menu rows (no one else can sell the same stock) and check stock
    SELECT COUNT(*) INTO v_bad
    FROM menu_items m
    JOIN JSON_TABLE(p_items, '$[*]' COLUMNS (item_id INT PATH '$.item_id', qty INT PATH '$.qty')) j
      ON j.item_id = m.item_id
    WHERE m.stock_qty < j.qty
    FOR UPDATE OF m;
    IF v_bad > 0 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Not enough stock for one or more items';
    END IF;

    -- 4. Bill amount from current menu prices
    SELECT SUM(m.price * j.qty) INTO v_item_total
    FROM menu_items m
    JOIN JSON_TABLE(p_items, '$[*]' COLUMNS (item_id INT PATH '$.item_id', qty INT PATH '$.qty')) j
      ON j.item_id = m.item_id;

    -- 5. Coupon
    IF p_coupon_code IS NOT NULL AND p_coupon_code <> '' THEN
        SELECT coupon_id, discount_type, discount_value, max_discount, min_order_value
          INTO v_coupon_id, v_type, v_value, v_cap, v_min
        FROM coupons
        WHERE coupon_code = p_coupon_code
          AND is_active
          AND CURDATE() BETWEEN valid_from AND valid_to;

        IF v_coupon_id IS NULL THEN
            SET v_err = CONCAT('Coupon ', p_coupon_code, ' is invalid or expired');
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = v_err;
        END IF;
        IF v_item_total < v_min THEN
            SET v_err = CONCAT('Coupon ', p_coupon_code, ' needs a minimum order of Rs ', v_min);
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = v_err;
        END IF;

        IF v_type = 'PERCENT' THEN
            SET v_discount = LEAST(v_item_total * v_value / 100, COALESCE(v_cap, v_item_total));
        ELSE
            SET v_discount = v_value;
        END IF;
        SET v_discount = ROUND(LEAST(v_discount, v_item_total), 2);
    END IF;

    SET v_fee   = IF(v_item_total >= 299, 0, 30);
    SET v_final = v_item_total - v_discount + v_fee;

    -- 6. Create the order and its lines
    INSERT INTO orders (customer_id, restaurant_id, coupon_id, order_time, status,
                        item_total, discount_amount, delivery_fee, final_amount, promised_minutes)
    VALUES (p_customer_id, p_restaurant_id, v_coupon_id, NOW(), 'PLACED',
            v_item_total, v_discount, v_fee, v_final, 35);
    SET p_order_id = LAST_INSERT_ID();

    INSERT INTO order_items (order_id, item_id, quantity, unit_price)
    SELECT p_order_id, m.item_id, j.qty, m.price
    FROM menu_items m
    JOIN JSON_TABLE(p_items, '$[*]' COLUMNS (item_id INT PATH '$.item_id', qty INT PATH '$.qty')) j
      ON j.item_id = m.item_id;

    -- 7. Reduce stock
    UPDATE menu_items m
    JOIN (SELECT item_id, qty
          FROM JSON_TABLE(p_items, '$[*]' COLUMNS (item_id INT PATH '$.item_id', qty INT PATH '$.qty')) jt) j
      ON j.item_id = m.item_id
    SET m.stock_qty = m.stock_qty - j.qty;

    -- 8. Record the payment (cash on delivery stays PENDING until delivery)
    INSERT INTO payments (order_id, payment_method, amount, payment_status, paid_at)
    VALUES (p_order_id, p_payment_method, v_final,
            IF(p_payment_method = 'COD', 'PENDING', 'SUCCESS'), NOW());

    -- 9. Nearest free delivery partner
    CALL sp_assign_partner(p_order_id, v_partner);

    COMMIT;
    SET SESSION sql_safe_updates = @fd_safe_mode;

    SET p_message = CONCAT('Order ', p_order_id, ' placed. Pay Rs ', v_final,
                           IF(v_discount > 0, CONCAT(' (saved Rs ', v_discount, ')'), ''),
                           IF(v_partner IS NULL, '. Waiting for a delivery partner.',
                              CONCAT('. Delivery partner ', v_partner, ' assigned.')));
END$$


-- =====================================================================
-- PROCEDURE: move an order through its life cycle
--   PLACED -> CONFIRMED -> PREPARING -> PICKED_UP -> DELIVERED
--   (CANCELLED is handled by sp_cancel_order)
-- =====================================================================
CREATE PROCEDURE sp_update_order_status(
    IN  p_order_id   BIGINT,
    IN  p_new_status VARCHAR(20),
    OUT p_message    VARCHAR(255))
BEGIN
    DECLARE v_old VARCHAR(20);

    SELECT status INTO v_old FROM orders WHERE order_id = p_order_id FOR UPDATE;

    IF v_old IS NULL THEN
        SET p_message = CONCAT('Order ', p_order_id, ' not found');
    ELSEIF (v_old = 'PLACED'    AND p_new_status = 'CONFIRMED')
        OR (v_old = 'CONFIRMED' AND p_new_status = 'PREPARING')
        OR (v_old = 'PREPARING' AND p_new_status = 'PICKED_UP')
        OR (v_old = 'PICKED_UP' AND p_new_status = 'DELIVERED') THEN
        UPDATE orders SET status = p_new_status WHERE order_id = p_order_id;
        IF p_new_status = 'DELIVERED' THEN
            UPDATE payments SET payment_status = 'SUCCESS', paid_at = NOW()
            WHERE order_id = p_order_id AND payment_status = 'PENDING';     -- COD collected
        END IF;
        SET p_message = CONCAT('Order ', p_order_id, ': ', v_old, ' -> ', p_new_status);
    ELSE
        SET p_message = CONCAT('Not allowed: ', v_old, ' -> ', p_new_status);
    END IF;
END$$


-- =====================================================================
-- PROCEDURE: cancel an order - ONE transaction
--   restock items, refund (or void COD), free partner (via trigger)
-- =====================================================================
CREATE PROCEDURE sp_cancel_order(IN p_order_id BIGINT, OUT p_message VARCHAR(255))
BEGIN
    DECLARE v_status VARCHAR(20);
    DECLARE v_err    VARCHAR(255);
    DECLARE EXIT HANDLER FOR SQLEXCEPTION
    BEGIN
        GET DIAGNOSTICS CONDITION 1 v_err = MESSAGE_TEXT;
        ROLLBACK;
        SET SESSION sql_safe_updates = @fd_safe_mode;
        SET p_message = CONCAT('ROLLED BACK: ', v_err);
    END;

    SET @fd_safe_mode = @@SESSION.sql_safe_updates;
    SET SESSION sql_safe_updates = 0;
    START TRANSACTION;
    SELECT status INTO v_status FROM orders WHERE order_id = p_order_id FOR UPDATE;

    IF v_status IS NULL THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Order not found';
    ELSEIF v_status IN ('PICKED_UP', 'DELIVERED', 'CANCELLED') THEN
        SET v_err = CONCAT('Order is already ', v_status, ' and cannot be cancelled');
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = v_err;
    END IF;

    UPDATE menu_items m
    JOIN order_items oi ON oi.item_id = m.item_id
    SET m.stock_qty = m.stock_qty + oi.quantity
    WHERE oi.order_id = p_order_id;

    UPDATE payments
    SET payment_status = IF(payment_status = 'SUCCESS', 'REFUNDED', 'FAILED')
    WHERE order_id = p_order_id;

    UPDATE orders SET status = 'CANCELLED' WHERE order_id = p_order_id;
    COMMIT;
    SET SESSION sql_safe_updates = @fd_safe_mode;
    SET p_message = CONCAT('Order ', p_order_id, ' cancelled, stock restored, payment reversed');
END$$


-- =====================================================================
-- PROCEDURE: customer 360 summary
-- =====================================================================
CREATE PROCEDURE sp_customer_summary(IN p_customer_id INT)
BEGIN
    SELECT cu.customer_id, cu.full_name, ci.city_name, cu.signup_date,
           COUNT(o.order_id)                                         AS total_orders,
           SUM(o.status = 'DELIVERED')                               AS delivered_orders,
           ROUND(SUM(CASE WHEN o.status = 'DELIVERED' THEN o.final_amount END), 2) AS lifetime_value,
           ROUND(AVG(CASE WHEN o.status = 'DELIVERED' THEN o.final_amount END), 2) AS avg_order_value,
           MAX(o.order_time)                                         AS last_order,
           (SELECT r.cuisine_type
              FROM orders o2 JOIN restaurants r ON r.restaurant_id = o2.restaurant_id
             WHERE o2.customer_id = p_customer_id
             GROUP BY r.cuisine_type ORDER BY COUNT(*) DESC LIMIT 1) AS favourite_cuisine
    FROM customers cu
    JOIN zones z   ON z.zone_id = cu.zone_id
    JOIN cities ci ON ci.city_id = z.city_id
    LEFT JOIN orders o ON o.customer_id = cu.customer_id
    WHERE cu.customer_id = p_customer_id
    GROUP BY cu.customer_id, cu.full_name, ci.city_name, cu.signup_date;
END$$

DELIMITER ;


-- =====================================================================
-- DEMO - run the section below to see everything working
-- (customer 31 Karthik Rao and restaurant 86 'Hyderabadi Biryani House'
--  are both in Hyderabad)
-- =====================================================================

-- remember where the real data ends, so the cleanup removes only the demo orders
SET @demo_start = (SELECT MAX(order_id) FROM orders);

-- D1. Stock before
SELECT item_id, item_name, price, stock_qty FROM menu_items WHERE item_id IN (724, 726, 723, 728);

-- D2. SUCCESS: 2 x Hyderabadi Chicken Biryani + 2 x Raita with coupon SAVE20, paid by UPI
CALL sp_place_order(31, 86, '[{"item_id": 728, "qty": 2}, {"item_id": 726, "qty": 2}]', 'SAVE20', 'UPI', @order1, @msg1);
SELECT @order1 AS order_id, @msg1 AS message;
SELECT order_id, status, partner_id, item_total, discount_amount, delivery_fee, final_amount
FROM orders WHERE order_id = @order1;

-- D3. FAILURE -> ROLLBACK: asks for 500 Double ka Meetha (only 103 in stock)
CALL sp_place_order(31, 86, '[{"item_id": 728, "qty": 1}, {"item_id": 723, "qty": 500}]', NULL, 'UPI', @order2, @msg2);
SELECT @order2 AS order_id, @msg2 AS message;

-- D4. FAILURE -> ROLLBACK: DIWALI25 expired on 31 Oct 2025
CALL sp_place_order(31, 86, '[{"item_id": 724, "qty": 1}]', 'DIWALI25', 'CARD', @order3, @msg3);
SELECT @order3 AS order_id, @msg3 AS message;

-- D5. FAILURE -> ROLLBACK: item 1119 belongs to a different restaurant
CALL sp_place_order(31, 86, '[{"item_id": 1119, "qty": 1}]', NULL, 'UPI', @order4, @msg4);
SELECT @order4 AS order_id, @msg4 AS message;

-- D6. Stock after: only the successful order reduced stock (item 728: -2, item 726: -2)
SELECT item_id, item_name, price, stock_qty FROM menu_items WHERE item_id IN (724, 726, 723, 728);

-- D7. Order life cycle -> every change is logged by the trigger
CALL sp_update_order_status(@order1, 'PREPARING', @s1);
CALL sp_update_order_status(@order1, 'PICKED_UP', @s2);
CALL sp_update_order_status(@order1, 'DELIVERED', @s3);
CALL sp_update_order_status(@order1, 'PREPARING', @s4);          -- not allowed
SELECT @s1, @s2, @s3, @s4;
SELECT * FROM order_status_log WHERE order_id = @order1 ORDER BY log_id;

-- D8. Partner was freed by the trigger after delivery
SELECT o.partner_id, p.full_name, p.is_available, o.delivered_time
FROM orders o JOIN delivery_partners p ON p.partner_id = o.partner_id
WHERE o.order_id = @order1;

-- D9. Rating trigger updates the restaurant's average
SELECT restaurant_id, restaurant_name, avg_rating, total_ratings FROM restaurants WHERE restaurant_id = 86;
INSERT INTO ratings (order_id, customer_id, restaurant_id, food_rating, delivery_rating, review_text, rated_at)
VALUES (@order1, 31, 86, 5, 5, 'Demo: hot and fresh!', NOW());
SELECT restaurant_id, restaurant_name, avg_rating, total_ratings FROM restaurants WHERE restaurant_id = 86;

-- D10. Cancel flow: place an order, then cancel it -> stock back, payment refunded
CALL sp_place_order(31, 86, '[{"item_id": 724, "qty": 1}]', NULL, 'CARD', @order5, @msg5);
SELECT stock_qty AS stock_after_order FROM menu_items WHERE item_id = 724;
CALL sp_cancel_order(@order5, @msg6);
SELECT @msg5, @msg6;
SELECT stock_qty AS stock_after_cancel FROM menu_items WHERE item_id = 724;
SELECT payment_status FROM payments WHERE order_id = @order5;

-- D11. Customer 360
CALL sp_customer_summary(31);


-- =====================================================================
-- CLEANUP - remove the demo orders so the analysis numbers stay the same
-- (demo orders are the ones above @demo_start, saved at the start of the demo)
-- =====================================================================
START TRANSACTION;
UPDATE menu_items m
JOIN (SELECT oi.item_id, SUM(oi.quantity) AS q
      FROM order_items oi JOIN orders o ON o.order_id = oi.order_id
      WHERE o.order_id > @demo_start AND o.status <> 'CANCELLED'
      GROUP BY oi.item_id) x ON x.item_id = m.item_id
SET m.stock_qty = m.stock_qty + x.q;
DELETE FROM ratings          WHERE order_id > @demo_start;
DELETE FROM payments         WHERE order_id > @demo_start;
DELETE FROM order_status_log WHERE order_id > @demo_start;
DELETE FROM orders           WHERE order_id > @demo_start;     -- order_items deleted by ON DELETE CASCADE
UPDATE delivery_partners SET is_available = TRUE WHERE partner_id > 0;
COMMIT;
ALTER TABLE orders AUTO_INCREMENT = 1;            -- MySQL moves it to the next free id
SET SQL_SAFE_UPDATES = 1;

SELECT COUNT(*) AS orders_after_cleanup FROM orders;
