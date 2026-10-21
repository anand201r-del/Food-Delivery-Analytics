-- =====================================================================
--  Food Delivery Platform Database (Swiggy / Zomato style)
--  File   : 01_schema.sql
--  Phase  : 1 - Schema design (3NF, PK/FK, constraints)
--  Engine : MySQL 8.0+
--  Author : Anand Kumar
-- =====================================================================

DROP DATABASE IF EXISTS food_delivery;
CREATE DATABASE food_delivery CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE food_delivery;

-- ---------------------------------------------------------------------
-- 1. LOCATION TABLES
-- ---------------------------------------------------------------------
CREATE TABLE cities (
    city_id      INT AUTO_INCREMENT PRIMARY KEY,
    city_name    VARCHAR(50)  NOT NULL UNIQUE,
    state        VARCHAR(50)  NOT NULL,
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE zones (
    zone_id      INT AUTO_INCREMENT PRIMARY KEY,
    city_id      INT          NOT NULL,
    zone_name    VARCHAR(60)  NOT NULL,
    latitude     DECIMAL(9,6) NOT NULL,
    longitude    DECIMAL(9,6) NOT NULL,
    UNIQUE KEY uq_zone_city (city_id, zone_name),
    CONSTRAINT fk_zone_city FOREIGN KEY (city_id) REFERENCES cities(city_id)
);

-- ---------------------------------------------------------------------
-- 2. PEOPLE & BUSINESSES
-- ---------------------------------------------------------------------
CREATE TABLE customers (
    customer_id   INT AUTO_INCREMENT PRIMARY KEY,
    full_name     VARCHAR(100) NOT NULL,
    email         VARCHAR(120) NOT NULL UNIQUE,
    phone         VARCHAR(15)  NOT NULL UNIQUE,
    zone_id       INT          NOT NULL,
    signup_date   DATE         NOT NULL,
    is_active     BOOLEAN      NOT NULL DEFAULT TRUE,
    CONSTRAINT fk_customer_zone FOREIGN KEY (zone_id) REFERENCES zones(zone_id)
);

CREATE TABLE restaurants (
    restaurant_id   INT AUTO_INCREMENT PRIMARY KEY,
    restaurant_name VARCHAR(120) NOT NULL,
    zone_id         INT          NOT NULL,
    cuisine_type    VARCHAR(50)  NOT NULL,
    is_veg_only     BOOLEAN      NOT NULL DEFAULT FALSE,
    avg_rating      DECIMAL(2,1) NOT NULL DEFAULT 0.0,   -- maintained by trigger (Phase 3)
    total_ratings   INT          NOT NULL DEFAULT 0,
    opening_time    TIME         NOT NULL,
    closing_time    TIME         NOT NULL,
    onboarded_on    DATE         NOT NULL,
    is_active       BOOLEAN      NOT NULL DEFAULT TRUE,
    CONSTRAINT fk_rest_zone FOREIGN KEY (zone_id) REFERENCES zones(zone_id),
    CONSTRAINT chk_rest_rating CHECK (avg_rating BETWEEN 0 AND 5)
);

CREATE TABLE menu_items (
    item_id        INT AUTO_INCREMENT PRIMARY KEY,
    restaurant_id  INT           NOT NULL,
    item_name      VARCHAR(120)  NOT NULL,
    category       VARCHAR(50)   NOT NULL,          -- Starter, Main Course, Dessert, Beverage...
    price          DECIMAL(8,2)  NOT NULL,
    is_veg         BOOLEAN       NOT NULL,
    stock_qty      INT           NOT NULL DEFAULT 100,
    is_available   BOOLEAN       NOT NULL DEFAULT TRUE,
    CONSTRAINT fk_item_rest FOREIGN KEY (restaurant_id) REFERENCES restaurants(restaurant_id),
    CONSTRAINT chk_item_price CHECK (price > 0),
    CONSTRAINT chk_item_stock CHECK (stock_qty >= 0)
);

CREATE TABLE delivery_partners (
    partner_id     INT AUTO_INCREMENT PRIMARY KEY,
    full_name      VARCHAR(100) NOT NULL,
    phone          VARCHAR(15)  NOT NULL UNIQUE,
    zone_id        INT          NOT NULL,              -- home zone
    vehicle_type   ENUM('Bike','Scooter','Bicycle','EV') NOT NULL,
    current_lat    DECIMAL(9,6),
    current_lng    DECIMAL(9,6),
    is_available   BOOLEAN      NOT NULL DEFAULT TRUE,
    joined_on      DATE         NOT NULL,
    CONSTRAINT fk_partner_zone FOREIGN KEY (zone_id) REFERENCES zones(zone_id)
);

-- ---------------------------------------------------------------------
-- 3. COUPONS
-- ---------------------------------------------------------------------
CREATE TABLE coupons (
    coupon_id        INT AUTO_INCREMENT PRIMARY KEY,
    coupon_code      VARCHAR(20)  NOT NULL UNIQUE,
    discount_type    ENUM('PERCENT','FLAT') NOT NULL,
    discount_value   DECIMAL(6,2) NOT NULL,
    max_discount     DECIMAL(8,2),                   -- cap for PERCENT coupons
    min_order_value  DECIMAL(8,2) NOT NULL DEFAULT 0,
    valid_from       DATE         NOT NULL,
    valid_to         DATE         NOT NULL,
    is_active        BOOLEAN      NOT NULL DEFAULT TRUE,
    CONSTRAINT chk_coupon_dates CHECK (valid_to >= valid_from),
    CONSTRAINT chk_coupon_value CHECK (discount_value > 0)
);

-- ---------------------------------------------------------------------
-- 4. ORDERS (transaction tables)
-- ---------------------------------------------------------------------
CREATE TABLE orders (
    order_id           BIGINT AUTO_INCREMENT PRIMARY KEY,
    customer_id        INT          NOT NULL,
    restaurant_id      INT          NOT NULL,
    partner_id         INT,                          -- NULL until assigned
    coupon_id          INT,                          -- NULL if no coupon
    order_time         DATETIME     NOT NULL,
    status             ENUM('PLACED','CONFIRMED','PREPARING','PICKED_UP',
                            'DELIVERED','CANCELLED') NOT NULL DEFAULT 'PLACED',
    item_total         DECIMAL(10,2) NOT NULL DEFAULT 0,
    discount_amount    DECIMAL(10,2) NOT NULL DEFAULT 0,
    delivery_fee       DECIMAL(6,2)  NOT NULL DEFAULT 0,
    final_amount       DECIMAL(10,2) NOT NULL DEFAULT 0,
    promised_minutes   SMALLINT      NOT NULL DEFAULT 35,
    delivered_time     DATETIME,
    CONSTRAINT fk_order_customer   FOREIGN KEY (customer_id)   REFERENCES customers(customer_id),
    CONSTRAINT fk_order_restaurant FOREIGN KEY (restaurant_id) REFERENCES restaurants(restaurant_id),
    CONSTRAINT fk_order_partner    FOREIGN KEY (partner_id)    REFERENCES delivery_partners(partner_id),
    CONSTRAINT fk_order_coupon     FOREIGN KEY (coupon_id)     REFERENCES coupons(coupon_id),
    CONSTRAINT chk_order_amounts   CHECK (final_amount >= 0 AND discount_amount >= 0)
);

CREATE TABLE order_items (
    order_item_id  BIGINT AUTO_INCREMENT PRIMARY KEY,
    order_id       BIGINT        NOT NULL,
    item_id        INT           NOT NULL,
    quantity       SMALLINT      NOT NULL,
    unit_price     DECIMAL(8,2)  NOT NULL,           -- price at time of order (history)
    UNIQUE KEY uq_order_item (order_id, item_id),
    CONSTRAINT fk_oi_order FOREIGN KEY (order_id) REFERENCES orders(order_id) ON DELETE CASCADE,
    CONSTRAINT fk_oi_item  FOREIGN KEY (item_id)  REFERENCES menu_items(item_id),
    CONSTRAINT chk_oi_qty  CHECK (quantity > 0)
);

CREATE TABLE payments (
    payment_id      BIGINT AUTO_INCREMENT PRIMARY KEY,
    order_id        BIGINT       NOT NULL UNIQUE,    -- one payment per order
    payment_method  ENUM('UPI','CARD','WALLET','NETBANKING','COD') NOT NULL,
    amount          DECIMAL(10,2) NOT NULL,
    payment_status  ENUM('SUCCESS','FAILED','REFUNDED','PENDING') NOT NULL,
    paid_at         DATETIME     NOT NULL,
    CONSTRAINT fk_pay_order FOREIGN KEY (order_id) REFERENCES orders(order_id)
);

CREATE TABLE ratings (
    rating_id          BIGINT AUTO_INCREMENT PRIMARY KEY,
    order_id           BIGINT   NOT NULL UNIQUE,     -- one rating per order
    customer_id        INT      NOT NULL,
    restaurant_id      INT      NOT NULL,
    food_rating        TINYINT  NOT NULL,
    delivery_rating    TINYINT,
    review_text        VARCHAR(500),
    rated_at           DATETIME NOT NULL,
    CONSTRAINT fk_rating_order      FOREIGN KEY (order_id)      REFERENCES orders(order_id),
    CONSTRAINT fk_rating_customer   FOREIGN KEY (customer_id)   REFERENCES customers(customer_id),
    CONSTRAINT fk_rating_restaurant FOREIGN KEY (restaurant_id) REFERENCES restaurants(restaurant_id),
    CONSTRAINT chk_food_rating      CHECK (food_rating BETWEEN 1 AND 5),
    CONSTRAINT chk_delivery_rating  CHECK (delivery_rating BETWEEN 1 AND 5)
);

-- Audit table: every status change is logged here by a trigger (Phase 3)
CREATE TABLE order_status_log (
    log_id       BIGINT AUTO_INCREMENT PRIMARY KEY,
    order_id     BIGINT      NOT NULL,
    old_status   VARCHAR(20),
    new_status   VARCHAR(20) NOT NULL,
    changed_at   DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_log_order FOREIGN KEY (order_id) REFERENCES orders(order_id)
);

-- Quick check
SHOW TABLES;
