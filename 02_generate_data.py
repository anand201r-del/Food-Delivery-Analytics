"""
Food Delivery Platform - Realistic Data Generator
File   : 02_generate_data.py
Phase  : 2 - Fill the food_delivery database with realistic data
Author : Anand Kumar

What it creates (default sizes):
    49 cities in every Indian state plus Delhi, Chandigarh, Jammu & Kashmir and Puducherry,
    ~160 real localities as delivery zones, 20,000 customers, 900 restaurants, ~8,000 menu items,
    2,200 delivery partners, 13 coupons, 1,00,000 orders (~1.9 lakh order items),
    1,00,000 payments and ~36,000 ratings.

How it is made realistic:
    * Metros and NCR take ~70% of orders; smaller cities grow faster from a smaller base
    * Average order value lands near Rs 430, close to what Swiggy (Rs 436) and Zomato (Rs 425)
      reported for food delivery in Q1 FY25; smaller cities have cheaper menus
    * UPI is the main payment method; cash on delivery is more common in smaller cities
    * Regional food: dosa and idli in the south, thalis in Gujarat and Rajasthan, fish and
      momos in the east and north-east, plus breakfast, evening-snack and late-night habits
    * Restaurants only receive orders while they are open; ~4% are open 24x7
    * Lunch (12-2 PM) and dinner (7-10 PM) peaks, weekend spikes, IPL evenings
    * Festivals: New Year's Eve, Diwali eve, Durga Puja in the east, Onam in Kerala,
      Ganesh Chaturthi in Maharashtra, Pongal in Tamil Nadu, Eid, Bihu, Christmas
    * Monsoon months differ by region; rain slows riders, adds a fee and causes cancellations
    * Hill cities (Shimla, Gangtok, Aizawl ...) and traffic-heavy metros deliver slower
    * Names follow regional traditions (e.g. Khasi names in Shillong, Meitei names in Imphal)
    * A few loyal customers order a lot, many order once and never return (churn)

City shares and payment mix are informed estimates, not official figures.
Swiggy and Zomato do not publish order-level data, so every order is synthetic.

Usage:
    pip install mysql-connector-python            (only needed to load MySQL directly)
    python 02_generate_data.py                    (asks for your MySQL root password)
    python 02_generate_data.py --dry-run          (only generates in memory, no DB)
    python 02_generate_data.py --sql-out 02_data.sql
                                                  (writes INSERT statements to a .sql file instead)

Run 01_schema.sql first. Re-running this script wipes and refills all tables.
The output is the same every time (fixed random seed), so it matches the shipped 02_data.sql.
"""

import argparse
import bisect
import getpass
import math
import random
import sys
import time
from datetime import date, datetime, timedelta

# ----------------------------------------------------------------------
# REFERENCE DATA
# ----------------------------------------------------------------------
# name, state, lat, lng, order share (%), tier (1 metro / 2 / 3), region, traffic factor, localities
CITIES = [
    # ---- metros (tier 1) ----
    ("Bengaluru", "Karnataka", 12.9716, 77.5946, 13.5, 1, "south", 1.12,
     ["Koramangala", "Indiranagar", "HSR Layout", "Whitefield", "Jayanagar", "Electronic City"]),
    ("Mumbai", "Maharashtra", 19.0760, 72.8777, 11.5, 1, "west", 1.10,
     ["Andheri", "Bandra", "Powai", "Lower Parel", "Thane", "Navi Mumbai"]),
    ("Delhi", "Delhi", 28.6139, 77.2090, 10.5, 1, "north", 1.06,
     ["Connaught Place", "Saket", "Dwarka", "Rajouri Garden", "Lajpat Nagar", "Rohini"]),
    ("Hyderabad", "Telangana", 17.3850, 78.4867, 9.0, 1, "south", 1.04,
     ["Gachibowli", "Hitech City", "Banjara Hills", "Kukatpally", "Secunderabad", "Madhapur"]),
    ("Chennai", "Tamil Nadu", 13.0827, 80.2707, 6.5, 1, "south", 1.03,
     ["T Nagar", "Adyar", "Velachery", "Anna Nagar", "OMR", "Tambaram"]),
    ("Pune", "Maharashtra", 18.5204, 73.8567, 6.0, 1, "west", 1.05,
     ["Kothrud", "Hinjewadi", "Viman Nagar", "Baner", "Koregaon Park", "Wakad"]),
    ("Kolkata", "West Bengal", 22.5726, 88.3639, 4.5, 1, "east", 1.05,
     ["Salt Lake", "Park Street", "New Town", "Ballygunge", "Howrah", "Dum Dum"]),
    ("Gurugram", "Haryana", 28.4595, 77.0266, 3.5, 1, "north", 1.06,
     ["DLF Phase 1", "Sohna Road", "Golf Course Road", "Sector 56"]),
    ("Ahmedabad", "Gujarat", 23.0225, 72.5714, 3.0, 1, "west", 1.00,
     ["Navrangpura", "Satellite", "Bodakdev", "Maninagar", "Chandkheda"]),
    ("Noida", "Uttar Pradesh", 28.5355, 77.3910, 2.2, 1, "north", 1.02,
     ["Sector 18", "Sector 62", "Sector 137", "Greater Noida West"]),
    # ---- large tier-2 ----
    ("Jaipur", "Rajasthan", 26.9124, 75.7873, 1.6, 2, "west", 0.97,
     ["C-Scheme", "Malviya Nagar", "Vaishali Nagar", "Mansarovar"]),
    ("Lucknow", "Uttar Pradesh", 26.8467, 80.9462, 1.4, 2, "north", 0.97,
     ["Hazratganj", "Gomti Nagar", "Aliganj", "Indira Nagar"]),
    ("Kochi", "Kerala", 9.9312, 76.2673, 1.3, 2, "south", 0.98,
     ["Edappally", "Kakkanad", "Fort Kochi", "Kaloor"]),
    ("Chandigarh", "Chandigarh", 30.7333, 76.7794, 1.2, 2, "north", 0.92,
     ["Sector 17", "Sector 35", "Manimajra"]),
    ("Indore", "Madhya Pradesh", 22.7196, 75.8577, 1.1, 2, "west", 0.95,
     ["Vijay Nagar", "Palasia", "Rajwada", "Bhawarkuan"]),
    ("Surat", "Gujarat", 21.1702, 72.8311, 1.0, 2, "west", 0.95,
     ["Adajan", "Vesu", "Athwa"]),
    ("Coimbatore", "Tamil Nadu", 11.0168, 76.9558, 0.9, 2, "south", 0.94,
     ["RS Puram", "Gandhipuram", "Peelamedu"]),
    ("Bhubaneswar", "Odisha", 20.2961, 85.8245, 0.9, 2, "east", 0.94,
     ["Saheed Nagar", "Patia", "Jaydev Vihar", "Old Town"]),
    ("Guwahati", "Assam", 26.1445, 91.7362, 0.8, 2, "northeast", 0.98,
     ["Paltan Bazaar", "Zoo Road", "Dispur", "Beltola", "Chandmari", "Six Mile"]),
    ("Nagpur", "Maharashtra", 21.1458, 79.0882, 0.8, 2, "west", 0.93,
     ["Dharampeth", "Sitabuldi", "Manish Nagar"]),
    ("Visakhapatnam", "Andhra Pradesh", 17.6868, 83.2185, 0.8, 2, "south", 0.94,
     ["MVP Colony", "Dwaraka Nagar", "Gajuwaka", "Madhurawada"]),
    ("Vadodara", "Gujarat", 22.3072, 73.1812, 0.7, 2, "west", 0.93,
     ["Alkapuri", "Gotri", "Manjalpur"]),
    ("Bhopal", "Madhya Pradesh", 23.2599, 77.4126, 0.7, 2, "west", 0.93,
     ["MP Nagar", "Arera Colony", "Kolar Road"]),
    ("Patna", "Bihar", 25.5941, 85.1376, 0.7, 2, "east", 1.00,
     ["Boring Road", "Kankarbagh", "Bailey Road", "Rajendra Nagar"]),
    ("Ludhiana", "Punjab", 30.9010, 75.8573, 0.6, 2, "north", 0.96,
     ["Sarabha Nagar", "Model Town", "Ferozepur Road"]),
    ("Dehradun", "Uttarakhand", 30.3165, 78.0322, 0.6, 2, "north", 0.96,
     ["Rajpur Road", "Clement Town", "Prem Nagar"]),
    ("Thiruvananthapuram", "Kerala", 8.5241, 76.9366, 0.6, 2, "south", 0.94,
     ["Kowdiar", "Pattom", "Kazhakkoottam"]),
    # ---- smaller cities (tier 3) ----
    ("Mysuru", "Karnataka", 12.2958, 76.6394, 0.5, 3, "south", 0.90,
     ["Vijayanagar", "Kuvempunagar", "Saraswathipuram"]),
    ("Vijayawada", "Andhra Pradesh", 16.5062, 80.6480, 0.5, 3, "south", 0.92,
     ["Benz Circle", "Governorpet", "Patamata"]),
    ("Kanpur", "Uttar Pradesh", 26.4499, 80.3319, 0.5, 3, "north", 0.96,
     ["Swaroop Nagar", "Kakadeo", "Civil Lines"]),
    ("Raipur", "Chhattisgarh", 21.2514, 81.6296, 0.5, 3, "west", 0.90,
     ["Shankar Nagar", "Telibandha", "Pandri"]),
    ("Ranchi", "Jharkhand", 23.3441, 85.3096, 0.5, 3, "east", 0.92,
     ["Lalpur", "Harmu", "Kanke Road"]),
    ("Mangaluru", "Karnataka", 12.9141, 74.8560, 0.4, 3, "south", 0.90,
     ["Hampankatta", "Kadri", "Bejai"]),
    ("Madurai", "Tamil Nadu", 9.9252, 78.1198, 0.4, 3, "south", 0.90,
     ["KK Nagar", "Tallakulam", "Anna Nagar"]),
    ("Amritsar", "Punjab", 31.6340, 74.8723, 0.4, 3, "north", 0.92,
     ["Ranjit Avenue", "Lawrence Road", "Golden Temple Road"]),
    ("Varanasi", "Uttar Pradesh", 25.3176, 82.9739, 0.4, 3, "north", 1.00,
     ["Lanka", "Sigra", "Godowlia"]),
    ("Panaji", "Goa", 15.4909, 73.8278, 0.4, 3, "west", 0.90,
     ["Panjim", "Porvorim", "Calangute"]),
    ("Jodhpur", "Rajasthan", 26.2389, 73.0243, 0.3, 3, "west", 0.90,
     ["Sardarpura", "Ratanada", "Paota"]),
    ("Siliguri", "West Bengal", 26.7271, 88.3953, 0.3, 3, "east", 0.92,
     ["Sevoke Road", "Pradhan Nagar", "Hakim Para"]),
    ("Puducherry", "Puducherry", 11.9416, 79.8083, 0.25, 3, "south", 0.88,
     ["White Town", "Lawspet", "Muthialpet"]),
    ("Srinagar", "Jammu and Kashmir", 34.0837, 74.7973, 0.2, 3, "north", 0.95,
     ["Lal Chowk", "Rajbagh", "Hyderpora"]),
    ("Shimla", "Himachal Pradesh", 31.1048, 77.1734, 0.15, 3, "north", 1.10,
     ["Mall Road", "Sanjauli", "Chotta Shimla"]),
    ("Shillong", "Meghalaya", 25.5788, 91.8933, 0.15, 3, "northeast", 1.08,
     ["Police Bazar", "Laitumkhrah", "Mawlai"]),
    ("Agartala", "Tripura", 23.8315, 91.2868, 0.12, 3, "northeast", 0.92,
     ["Krishnanagar", "Banamalipur", "Dhaleswar"]),
    ("Imphal", "Manipur", 24.8170, 93.9368, 0.1, 3, "northeast", 0.95,
     ["Thangal Bazar", "Singjamei", "Uripok"]),
    ("Gangtok", "Sikkim", 27.3389, 88.6065, 0.1, 3, "northeast", 1.12,
     ["MG Marg", "Tadong", "Development Area"]),
    ("Dimapur", "Nagaland", 25.9063, 93.7276, 0.08, 3, "northeast", 0.95,
     ["Duncan Basti", "Circular Road", "Purana Bazar"]),
    ("Aizawl", "Mizoram", 23.7271, 92.7176, 0.08, 3, "northeast", 1.12,
     ["Zarkawt", "Chanmari", "Dawrpui"]),
    ("Itanagar", "Arunachal Pradesh", 27.0844, 93.6053, 0.06, 3, "northeast", 1.10,
     ["Ganga Market", "Naharlagun", "Chandranagar"]),
]

# cuisine -> (probability a restaurant of this cuisine is veg-only, dishes)
# dish = (name, category, is_veg, min_price, max_price)   prices in INR at a typical metro restaurant
CUISINES = {
    "North Indian": (0.25, [
        ("Paneer Butter Masala", "Main Course", 1, 200, 290), ("Dal Makhani", "Main Course", 1, 170, 240),
        ("Butter Chicken", "Main Course", 0, 260, 360), ("Chicken Tikka", "Starter", 0, 230, 320),
        ("Paneer Tikka", "Starter", 1, 210, 290), ("Butter Naan", "Bread", 1, 35, 60),
        ("Tandoori Roti", "Bread", 1, 15, 35), ("Jeera Rice", "Rice", 1, 110, 160),
        ("Mutton Rogan Josh", "Main Course", 0, 320, 440), ("Gulab Jamun", "Dessert", 1, 60, 100),
        ("Sweet Lassi", "Beverage", 1, 50, 90), ("Chole Bhature", "Main Course", 1, 120, 190),
        ("Rajma Chawal", "Main Course", 1, 130, 180), ("Amritsari Kulcha", "Main Course", 1, 110, 160)]),
    "South Indian": (0.65, [
        ("Masala Dosa", "Main Course", 1, 80, 140), ("Idli Sambar", "Main Course", 1, 50, 90),
        ("Medu Vada", "Starter", 1, 50, 90), ("Rava Dosa", "Main Course", 1, 90, 150),
        ("Uttapam", "Main Course", 1, 90, 140), ("Filter Coffee", "Beverage", 1, 30, 60),
        ("Chicken Chettinad", "Main Course", 0, 240, 340), ("Curd Rice", "Rice", 1, 80, 130),
        ("Kesari Bath", "Dessert", 1, 50, 90), ("Pongal", "Main Course", 1, 80, 130),
        ("Mini Meals", "Thali", 1, 120, 180), ("Andhra Chicken Curry", "Main Course", 0, 230, 320)]),
    "Chinese": (0.1, [
        ("Veg Hakka Noodles", "Main Course", 1, 140, 210), ("Chicken Hakka Noodles", "Main Course", 0, 170, 240),
        ("Veg Manchurian", "Starter", 1, 150, 220), ("Chilli Chicken", "Starter", 0, 210, 290),
        ("Chicken Fried Rice", "Rice", 0, 170, 240), ("Veg Fried Rice", "Rice", 1, 140, 200),
        ("Spring Rolls", "Starter", 1, 130, 190), ("Hot and Sour Soup", "Soup", 1, 100, 150),
        ("Chicken Momos", "Starter", 0, 110, 170), ("Veg Momos", "Starter", 1, 90, 140)]),
    "Biryani": (0.05, [
        ("Chicken Dum Biryani", "Biryani", 0, 220, 320), ("Mutton Biryani", "Biryani", 0, 300, 420),
        ("Veg Biryani", "Biryani", 1, 170, 240), ("Egg Biryani", "Biryani", 0, 180, 250),
        ("Hyderabadi Chicken Biryani", "Biryani", 0, 260, 360), ("Kolkata Chicken Biryani", "Biryani", 0, 230, 320),
        ("Paneer Biryani", "Biryani", 1, 200, 280), ("Raita", "Side", 1, 35, 60),
        ("Mirchi ka Salan", "Side", 1, 50, 90), ("Double ka Meetha", "Dessert", 1, 80, 130),
        ("Chicken 65", "Starter", 0, 200, 280)]),
    "Pizza": (0.2, [
        ("Margherita Pizza", "Pizza", 1, 179, 299), ("Farmhouse Pizza", "Pizza", 1, 279, 449),
        ("Pepperoni Pizza", "Pizza", 0, 329, 499), ("BBQ Chicken Pizza", "Pizza", 0, 329, 499),
        ("Paneer Tikka Pizza", "Pizza", 1, 279, 449), ("Garlic Bread", "Side", 1, 99, 159),
        ("Cheesy Dip", "Side", 1, 25, 50), ("Choco Lava Cake", "Dessert", 1, 99, 129),
        ("Pasta Alfredo", "Main Course", 1, 189, 279), ("Coke 500ml", "Beverage", 1, 45, 60)]),
    "Burgers & Fast Food": (0.15, [
        ("Veg Burger", "Burger", 1, 79, 149), ("Chicken Burger", "Burger", 0, 129, 209),
        ("Cheese Burger", "Burger", 1, 109, 179), ("French Fries", "Side", 1, 79, 129),
        ("Peri Peri Fries", "Side", 1, 99, 149), ("Chicken Wings", "Starter", 0, 189, 279),
        ("Cold Coffee", "Beverage", 1, 89, 149), ("Chocolate Shake", "Beverage", 1, 119, 179),
        ("Chicken Wrap", "Wrap", 0, 149, 219), ("Paneer Wrap", "Wrap", 1, 129, 189)]),
    "Street Food & Chaat": (0.7, [
        ("Pav Bhaji", "Main Course", 1, 100, 160), ("Vada Pav", "Snack", 1, 25, 45),
        ("Pani Puri", "Snack", 1, 40, 70), ("Aloo Tikki Chaat", "Snack", 1, 60, 100),
        ("Dahi Puri", "Snack", 1, 60, 100), ("Samosa (2 pcs)", "Snack", 1, 30, 50),
        ("Chicken Kathi Roll", "Wrap", 0, 110, 160), ("Egg Roll", "Wrap", 0, 60, 100),
        ("Misal Pav", "Main Course", 1, 80, 120), ("Kulfi", "Dessert", 1, 40, 70)]),
    "Gujarati & Rajasthani": (0.95, [
        ("Gujarati Thali", "Thali", 1, 200, 300), ("Dhokla", "Snack", 1, 60, 100),
        ("Khandvi", "Snack", 1, 70, 110), ("Dal Baati Churma", "Main Course", 1, 170, 240),
        ("Undhiyu", "Main Course", 1, 150, 210), ("Methi Thepla", "Bread", 1, 70, 110),
        ("Pyaaz Kachori", "Snack", 1, 30, 60), ("Gatte ki Sabzi", "Main Course", 1, 150, 210),
        ("Shrikhand", "Dessert", 1, 70, 110), ("Chaas", "Beverage", 1, 25, 45)]),
    "Kerala & Coastal": (0.15, [
        ("Kerala Parotta", "Bread", 1, 25, 45), ("Appam with Stew", "Main Course", 1, 130, 190),
        ("Fish Moilee", "Main Course", 0, 240, 340), ("Kerala Beef Fry", "Main Course", 0, 200, 280),
        ("Prawn Ghee Roast", "Main Course", 0, 300, 420), ("Chicken Sukka", "Main Course", 0, 220, 300),
        ("Neer Dosa", "Main Course", 1, 50, 80), ("Puttu Kadala", "Main Course", 1, 90, 130),
        ("Malabar Chicken Biryani", "Biryani", 0, 210, 290), ("Palada Payasam", "Dessert", 1, 60, 100)]),
    "Bengali & Northeast": (0.1, [
        ("Fish Curry Rice", "Main Course", 0, 200, 290), ("Kosha Mangsho", "Main Course", 0, 300, 420),
        ("Luchi Aloo Dum", "Main Course", 1, 100, 160), ("Pork Momos", "Starter", 0, 120, 180),
        ("Thukpa", "Soup", 0, 140, 210), ("Masor Tenga", "Main Course", 0, 220, 310),
        ("Rosogolla", "Dessert", 1, 50, 90), ("Mishti Doi", "Dessert", 1, 50, 90),
        ("Veg Thali", "Thali", 1, 160, 240), ("Smoked Pork with Bamboo Shoot", "Main Course", 0, 260, 360)]),
    "Desserts & Bakery": (0.9, [
        ("Chocolate Truffle Cake (500 g)", "Dessert", 1, 449, 699), ("Red Velvet Pastry", "Dessert", 1, 100, 150),
        ("Brownie", "Dessert", 1, 80, 130), ("Ice Cream Sundae", "Dessert", 1, 130, 200),
        ("Blueberry Cheesecake", "Dessert", 1, 170, 250), ("Rasmalai", "Dessert", 1, 80, 130),
        ("Cold Coffee", "Beverage", 1, 99, 159), ("Waffle", "Dessert", 1, 139, 219),
        ("Donut", "Dessert", 1, 60, 110), ("Cupcake", "Dessert", 1, 50, 90)]),
    "Healthy & Salads": (0.5, [
        ("Greek Salad", "Salad", 1, 200, 300), ("Chicken Caesar Salad", "Salad", 0, 240, 340),
        ("Quinoa Bowl", "Bowl", 1, 240, 340), ("Grilled Chicken Bowl", "Bowl", 0, 280, 380),
        ("Fruit Bowl", "Salad", 1, 130, 210), ("Smoothie", "Beverage", 1, 130, 210),
        ("Paneer Salad", "Salad", 1, 200, 280), ("Sprouts Chaat", "Starter", 1, 100, 160),
        ("Oats Upma", "Main Course", 1, 120, 180), ("Detox Juice", "Beverage", 1, 100, 160)]),
}

# national base weight of each cuisine (share of restaurants), then a multiplier per region
CUISINE_BASE = {"North Indian": 20, "South Indian": 10, "Chinese": 13, "Biryani": 14, "Pizza": 9,
                "Burgers & Fast Food": 10, "Street Food & Chaat": 6, "Gujarati & Rajasthani": 3,
                "Kerala & Coastal": 3, "Bengali & Northeast": 3, "Desserts & Bakery": 6, "Healthy & Salads": 3}
REGION_CUISINE = {
    "north": {"North Indian": 1.7, "Street Food & Chaat": 1.4, "South Indian": 0.5, "Kerala & Coastal": 0.15,
              "Bengali & Northeast": 0.3, "Gujarati & Rajasthani": 0.5},
    "west": {"Gujarati & Rajasthani": 2.5, "Street Food & Chaat": 1.6, "North Indian": 1.1, "Kerala & Coastal": 0.4,
             "Bengali & Northeast": 0.2, "South Indian": 0.8},
    "south": {"South Indian": 2.6, "Biryani": 1.4, "Kerala & Coastal": 1.8, "North Indian": 0.6,
              "Bengali & Northeast": 0.15, "Gujarati & Rajasthani": 0.2, "Street Food & Chaat": 0.7},
    "east": {"Bengali & Northeast": 6.5, "Chinese": 1.3, "Biryani": 1.2, "South Indian": 0.5,
             "Kerala & Coastal": 0.2, "Gujarati & Rajasthani": 0.3},
    "northeast": {"Bengali & Northeast": 8.0, "Chinese": 1.5, "South Indian": 0.4, "Kerala & Coastal": 0.15,
                  "Gujarati & Rajasthani": 0.2, "Healthy & Salads": 0.5},
}
# a few state-level tweaks on top of the region
STATE_CUISINE = {"Gujarat": {"Gujarati & Rajasthani": 2.6}, "Rajasthan": {"Gujarati & Rajasthani": 1.5},
                 "Kerala": {"Kerala & Coastal": 2.0}, "Goa": {"Kerala & Coastal": 2.5},
                 "Telangana": {"Biryani": 1.6}, "Punjab": {"North Indian": 1.3}, "Maharashtra": {"Street Food & Chaat": 1.3}}

# restaurant names: words that fit the cuisine (generic words, no real brands)
# cuisine -> (prefixes, suffixes); "{city}" and region words are filled in per restaurant
REST_NAMES = {
    "North Indian": (["Punjabi", "Amritsari", "Dilli", "Shahi", "Tandoor", "Desi", "Royal", "Masala", "Pind"],
                     ["Dhaba", "Rasoi", "Tadka", "Darbar", "Kitchen", "Junction", "Zaika"]),
    "South Indian": (["Sri Lakshmi", "Udupi", "Mylapore", "Kaveri", "Sri Ganesh", "Sri Sai", "Sri Venkatesha"],
                     ["Bhavan", "Tiffins", "Mess", "Cafe", "Upahar", "Idli House"]),
    "Chinese": (["Wok", "Golden Dragon", "Bamboo", "Red Lantern", "Jade", "Chopsticks", "Lucky"],
                ["Wok", "Kitchen", "House", "Express", "Garden", "Bowl"]),
    "Biryani": (["Shahi", "Nawabi", "Royal", "Dum", "Zaffrani", "Degh"],
                ["Biryani House", "Biryani Point", "Biryani Darbar", "Biryani Co.", "Handi"]),
    "Pizza": (["Urban", "Crust", "Fire", "Brick Oven", "Cheesy", "Stone", "Slice of"],
              ["Pizzeria", "Pizza Co.", "Pizza House", "Slice", "Oven"]),
    "Burgers & Fast Food": (["Urban", "Big", "Hungry", "Happy", "Street", "Loaded", "Smash"],
                            ["Burgers", "Bites", "Grill", "Snack Shack", "Diner", "Express"]),
    "Street Food & Chaat": (["Chatpata", "Chaat", "Gali", "Nukkad", "Desi", "Tapri"],
                            ["Chaat Corner", "Chaat House", "Pav Bhaji Centre", "Snacks", "Thela"]),
    "Gujarati & Rajasthani": (["Rajwadi", "Marwari", "Kathiyawadi", "Jaipuri", "Gujarati", "Rajasthani"],
                              ["Thali", "Bhojanalaya", "Rasoi", "Thal", "Kitchen"]),
    "Kerala & Coastal": (["Malabar", "Konkan", "Coastal", "Karavali", "Kerala", "Backwater"],
                         ["Kitchen", "Spice", "Fish House", "Mess", "Cafe"]),
    "Bengali & Northeast": (["Bong", "Calcutta", "Bengal", "Momo", "Hill", "Seven Sisters", "Brahmaputra"],
                            ["Kitchen", "Rannaghar", "Hotel", "Cafe", "Momo House"]),
    "Desserts & Bakery": (["Sweet", "Sugar", "Cocoa", "Honey", "Cake", "Butter"],
                          ["Bakery", "Patisserie", "Cakes", "Treats", "Desserts", "Creamery"]),
    "Healthy & Salads": (["Green", "Fresh", "Lean", "Fit", "Leafy", "Nourish"],
                         ["Bowl", "Salads", "Kitchen", "Eats", "Cafe"]),
}
# region-specific words only used where they make sense
REST_NAME_REGION = {"Bengali & Northeast": {"east": ["Bong", "Calcutta", "Bengal"],
                                            "northeast": ["Momo", "Hill", "Seven Sisters", "Brahmaputra"]},
                    "Biryani": {"south": ["Hyderabadi", "Malabar", "Ambur"], "north": ["Lucknowi", "Awadhi"],
                                "east": ["Kolkata"], "west": ["Bombay"], "northeast": ["Royal"]}}

# people names by region: male first names, female first names, surnames
NAMES = {
    "north": (["Aarav", "Rohan", "Rahul", "Amit", "Vikram", "Karan", "Nikhil", "Siddharth", "Manish", "Gaurav",
               "Harsh", "Kunal", "Varun", "Abhishek", "Ankit", "Mohit", "Tarun", "Yash", "Aditya", "Rajat",
               "Saurabh", "Prateek", "Deepak", "Arjun", "Ishaan"],
              ["Priya", "Ananya", "Neha", "Pooja", "Anjali", "Riya", "Shreya", "Divya", "Swati", "Simran",
               "Komal", "Megha", "Shruti", "Nidhi", "Tanya", "Kritika", "Aditi", "Muskan", "Sakshi", "Isha"],
              ["Sharma", "Verma", "Gupta", "Singh", "Kumar", "Agarwal", "Bansal", "Malhotra", "Kapoor", "Khanna",
               "Arora", "Bhatia", "Sethi", "Chopra", "Mishra", "Pandey", "Tiwari", "Yadav", "Chauhan", "Saxena",
               "Srivastava", "Rawat", "Negi", "Bisht", "Tyagi"]),
    "west": (["Rohan", "Aditya", "Omkar", "Tejas", "Parth", "Harsh", "Jay", "Dhruv", "Kunal", "Nikhil", "Pranav",
              "Siddhesh", "Chirag", "Hardik", "Vivek", "Saurabh", "Yash", "Amit", "Rahul", "Akshay"],
             ["Sneha", "Pooja", "Riddhi", "Siddhi", "Krupa", "Hetal", "Prachi", "Shruti", "Neha", "Rutuja",
              "Aishwarya", "Janhvi", "Mansi", "Khushi", "Disha", "Payal", "Bhakti", "Sakshi"],
             ["Patel", "Shah", "Mehta", "Desai", "Joshi", "Kulkarni", "Deshpande", "Patil", "Pawar", "Jadhav",
              "Shinde", "More", "Gaikwad", "Rathore", "Shekhawat", "Jain", "Trivedi", "Pandya", "Naik", "Sawant",
              "Chaudhari", "Bhatt", "Agrawal", "Soni"]),
    "south": (["Karthik", "Arjun", "Vignesh", "Sai", "Harish", "Srinivas", "Ramesh", "Anil", "Praveen", "Akhil",
               "Gokul", "Rahul", "Naveen", "Suresh", "Manoj", "Prasanna", "Varun", "Kiran", "Ajay", "Sanjay",
               "Vishnu", "Abhijith"],
              ["Lakshmi", "Divya", "Priya", "Aswathy", "Anjali", "Meenakshi", "Keerthana", "Swathi", "Deepa",
               "Sreeja", "Nithya", "Kavya", "Sowmya", "Harini", "Ramya", "Anusha", "Bhavana", "Revathi", "Gayathri"],
              ["Iyer", "Iyengar", "Reddy", "Rao", "Nair", "Menon", "Pillai", "Krishnan", "Subramanian", "Venkatesh",
               "Hegde", "Shetty", "Kamath", "Naidu", "Gowda", "Raju", "Varma", "Kurian", "Thomas", "Joseph",
               "Mathew", "Murthy", "Chettiar", "Rajan", "Prabhu"]),
    "east": (["Arnab", "Sourav", "Debjit", "Subhajit", "Rohit", "Abhishek", "Satyajit", "Tanmay", "Sayan",
              "Aniket", "Rajib", "Pritam", "Amit", "Saurav", "Biswajit", "Rakesh", "Manish", "Suman"],
             ["Ananya", "Rituparna", "Priyanka", "Ankita", "Sneha", "Moumita", "Payel", "Sohini", "Tanushree",
              "Riya", "Puja", "Swagata", "Madhumita", "Ishita", "Shreya", "Nandini"],
             ["Das", "Dutta", "Bose", "Chatterjee", "Banerjee", "Mukherjee", "Ghosh", "Sen", "Roy", "Mondal",
              "Sahoo", "Mohanty", "Patnaik", "Jha", "Sinha", "Prasad", "Mahato", "Paul", "Saha", "Pradhan"]),
    "northeast": (["Pranjal", "Bikash", "Rupjyoti", "Dhrubajyoti", "Manash", "Kaustav", "Anup", "Partha",
                   "Hiren", "Jyotirmoy"],
                  ["Nilakshi", "Jahnavi", "Ankita", "Priyanka", "Bidisha", "Pallavi", "Rimjhim", "Trishna"],
                  ["Bora", "Baruah", "Gogoi", "Saikia", "Kalita", "Deka", "Hazarika", "Das", "Sarma", "Choudhury"]),
}
# north-east states have their own naming traditions
STATE_NAMES = {
    "Meghalaya": (["Banshan", "Wanphrang", "Ibadahun", "Kyrshan", "Donbok", "Bankit"],
                  ["Daphisha", "Ibashisha", "Wanda", "Iohskhem", "Badarisha", "Phidari"],
                  ["Lyngdoh", "Kharkongor", "Syiem", "Marbaniang", "Nongrum", "Wahlang", "Kharbuli", "Shabong"]),
    "Mizoram": (["Lalremruata", "Lalthanzuala", "Vanlalruata", "Lalchhanhima", "Lalrinmawia", "Malsawma"],
                ["Zothanpuii", "Lalrinpuii", "Vanlalhriatpuii", "Lalmuanpuii", "Rebecca", "Mary"],
                ["Ralte", "Hmar", "Pachuau", "Sailo", "Chhangte", "Khiangte", "Fanai", "Renthlei"]),
    "Nagaland": (["Akum", "Imna", "Vikheto", "Hokheto", "Kevi", "Temsu", "Toshi", "Neiba"],
                 ["Atono", "Sentila", "Nungshi", "Akala", "Bendangla", "Vitono"],
                 ["Ao", "Jamir", "Sema", "Lotha", "Longkumer", "Chishi", "Zhimomi", "Kikon"]),
    "Manipur": (["Tomba", "Ibochou", "Premjit", "Sanjoy", "Bijen", "Romen", "Ibomcha"],
                ["Sanatombi", "Bembem", "Thoibi", "Linthoi", "Memcha", "Ngambi"],
                None),                                   # Meitei: Singh for men, Devi for women
    "Sikkim": (["Pemba", "Tashi", "Sonam", "Karma", "Bikram", "Rinzing", "Suraj"],
               ["Pema", "Dolma", "Yangchen", "Sushma", "Diki", "Anisha"],
               ["Bhutia", "Lepcha", "Rai", "Gurung", "Tamang", "Sherpa", "Pradhan", "Chettri"]),
    "Tripura": (["Bijoy", "Rajib", "Subrata", "Amit", "Pradip", "Rahul"],
                ["Mousumi", "Rumpa", "Swapna", "Rinku", "Tumpa", "Priya"],
                ["Debbarma", "Reang", "Saha", "Das", "Chakraborty", "Bhowmik", "Jamatia", "Nath"]),
    "Arunachal Pradesh": (["Tage", "Nabam", "Tana", "Kipa", "Techi", "Bamang"],
                          ["Yami", "Yater", "Mepung", "Rinchin", "Oyin", "Yamang"],
                          ["Tara", "Rebia", "Tok", "Taki", "Dolo", "Riba", "Tayeng", "Perme"]),
    "Punjab": (["Gurpreet", "Harpreet", "Jaspreet", "Manpreet", "Amandeep", "Simranjit", "Navjot", "Rajveer",
                "Karan", "Arjun"],
               ["Harleen", "Jasleen", "Simran", "Navneet", "Gurleen", "Manpreet", "Rupinder", "Kiran"],
               ["Gill", "Sandhu", "Dhillon", "Sidhu", "Grewal", "Brar", "Bajwa", "Arora", "Kapoor", "Sharma"]),
    "Jammu and Kashmir": (["Aamir", "Bilal", "Faisal", "Imran", "Owais", "Sajad", "Tariq", "Zubair"],
                          ["Aiman", "Insha", "Mehvish", "Rifat", "Saba", "Shazia", "Uzma", "Zainab"],
                          ["Bhat", "Dar", "Lone", "Mir", "Wani", "Shah", "Malik", "Rather"]),
    "Tamil Nadu": (["Karthik", "Vignesh", "Arun", "Prakash", "Saravanan", "Senthil", "Gokul", "Hari", "Bala",
                    "Dinesh", "Surya", "Ashwin"],
                   ["Keerthana", "Meenakshi", "Divya", "Nithya", "Harini", "Revathi", "Janani", "Priya", "Abirami",
                    "Kavitha"],
                   ["Krishnan", "Subramanian", "Rajan", "Murugan", "Selvam", "Pandian", "Ramasamy", "Iyer",
                    "Natarajan", "Sundaram"]),
}
SIKH_SHARE = 0.55       # share of Punjab names that use Singh / Kaur

# hour of day -> relative order volume (lunch + dinner peaks, quiet early morning)
HOUR_WEIGHTS = {0: 2.0, 1: 1.0, 2: 0.4, 3: 0.2, 4: 0.1, 5: 0.2, 6: 0.5, 7: 1.4, 8: 2.6, 9: 3.0,
                10: 2.8, 11: 5.0, 12: 10.0, 13: 12.0, 14: 8.0, 15: 4.0, 16: 3.5, 17: 4.5,
                18: 6.0, 19: 10.0, 20: 13.0, 21: 12.0, 22: 7.0, 23: 4.0}

# day of week (Mon=0) multiplier -> weekend spike
DOW_WEIGHTS = {0: 0.90, 1: 0.88, 2: 0.92, 3: 0.95, 4: 1.15, 5: 1.35, 6: 1.40}

# national days that change demand everywhere
SPECIAL_DAYS = {
    date(2025, 1, 1): 1.5, date(2025, 2, 14): 1.3,
    date(2025, 3, 9): 1.35,                       # Champions Trophy final (India v New Zealand)
    date(2025, 3, 14): 0.85,                      # Holi: people celebrate at home
    date(2025, 8, 15): 1.25, date(2025, 10, 2): 1.2,
    date(2025, 10, 19): 1.3, date(2025, 10, 20): 0.85,   # day before Diwali busy, Diwali night quiet
    date(2025, 12, 25): 1.35, date(2025, 12, 31): 1.8,
    date(2026, 1, 1): 1.5, date(2026, 2, 14): 1.3, date(2026, 3, 4): 0.85, date(2026, 8, 15): 1.25,
}
# regional festivals: (start, end, states, multiplier)
REGIONAL_FESTIVALS = [
    (date(2025, 1, 14), date(2025, 1, 16), {"Tamil Nadu", "Puducherry"}, 1.5),              # Pongal
    (date(2026, 1, 14), date(2026, 1, 16), {"Tamil Nadu", "Puducherry"}, 1.5),
    (date(2025, 3, 31), date(2025, 4, 1), {"Telangana", "Uttar Pradesh", "Jammu and Kashmir", "West Bengal", "Kerala"}, 1.6),  # Eid al-Fitr
    (date(2026, 3, 20), date(2026, 3, 21), {"Telangana", "Uttar Pradesh", "Jammu and Kashmir", "West Bengal", "Kerala"}, 1.6),
    (date(2025, 4, 14), date(2025, 4, 16), {"Assam"}, 1.6),                                 # Rongali Bihu
    (date(2026, 4, 14), date(2026, 4, 16), {"Assam"}, 1.6),
    (date(2025, 8, 27), date(2025, 9, 6), {"Maharashtra", "Goa"}, 1.35),                    # Ganesh Chaturthi
    (date(2025, 9, 5), date(2025, 9, 5), {"Kerala"}, 1.8),                                  # Onam (sadya orders)
    (date(2026, 8, 26), date(2026, 8, 26), {"Kerala"}, 1.8),
    (date(2025, 9, 28), date(2025, 10, 2), {"West Bengal", "Tripura", "Assam", "Odisha", "Bihar", "Jharkhand"}, 2.0),  # Durga Puja
    (date(2025, 12, 20), date(2025, 12, 31), {"Goa", "Meghalaya", "Mizoram", "Nagaland", "Kerala"}, 1.4),  # Christmas season
]
# IPL evenings (7-11 pm) see more orders everywhere
IPL_SEASONS = [(date(2025, 3, 22), date(2025, 6, 3)), (date(2026, 3, 28), date(2026, 5, 31))]

# code, type, value, max_discount, min_order, valid_from, valid_to, usage weight
COUPONS = [
    ("WELCOME50", "PERCENT", 50, 100, 149, date(2025, 1, 1), date(2026, 12, 31), 18),
    ("FLAT100", "FLAT", 100, None, 399, date(2025, 1, 1), date(2026, 12, 31), 10),
    ("SAVE20", "PERCENT", 20, 80, 249, date(2025, 1, 1), date(2026, 12, 31), 14),
    ("WEEKEND30", "PERCENT", 30, 120, 299, date(2025, 1, 1), date(2026, 12, 31), 10),
    ("LUNCH40", "PERCENT", 40, 75, 199, date(2025, 3, 1), date(2026, 12, 31), 9),
    ("BIGORDER150", "FLAT", 150, None, 699, date(2025, 1, 1), date(2026, 12, 31), 5),
    ("DIWALI25", "PERCENT", 25, 150, 299, date(2025, 10, 10), date(2025, 10, 31), 6),
    ("NEWYEAR60", "PERCENT", 60, 150, 249, date(2025, 12, 28), date(2026, 1, 3), 4),
    ("FREEDEL", "FLAT", 40, None, 149, date(2025, 1, 1), date(2026, 12, 31), 10),
    ("PARTY200", "FLAT", 200, None, 999, date(2025, 6, 1), date(2026, 12, 31), 3),
    ("MONSOON15", "PERCENT", 15, 60, 199, date(2025, 6, 15), date(2025, 9, 15), 5),
    ("IPL75", "FLAT", 75, None, 349, date(2026, 3, 28), date(2026, 5, 31), 4),
    ("PUJO30", "PERCENT", 30, 120, 249, date(2025, 9, 26), date(2025, 10, 3), 4),
]

# payment mix by city tier: UPI dominates; cash on delivery is more common in smaller cities
PAYMENT_METHODS = ["UPI", "CARD", "WALLET", "NETBANKING", "COD"]
PAYMENT_WEIGHTS = {1: [62, 16, 9, 2, 11], 2: [63, 11, 7, 2, 17], 3: [60, 7, 5, 2, 26]}

# ----------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------
SEED = 42
N_CUSTOMERS = 20_000
N_RESTAURANTS = 900
N_PARTNERS = 2_200
N_ORDERS = 100_000
START_DATE = date(2025, 1, 1)
END_DATE = date(2026, 9, 30)
BATCH = 5_000
RATING_SHARE = 0.38          # share of delivered orders that get a rating

COASTAL = {"Mumbai", "Panaji", "Mangaluru", "Kochi", "Thiruvananthapuram"}
NORTHEAST_STATES = {"Assam", "Meghalaya", "Mizoram", "Nagaland", "Manipur", "Tripura", "Sikkim",
                    "Arunachal Pradesh"}
REGION_OF = {c[0]: c[6] for c in CITIES}


# ----------------------------------------------------------------------
# HELPERS
# ----------------------------------------------------------------------
def weighted_choice(items, weights):
    return random.choices(items, weights=weights, k=1)[0]


def rand_date(start, end):
    return start + timedelta(days=random.randint(0, (end - start).days))


def unique_phones(n, used):
    out = []
    while len(out) < n:
        p = str(random.choice([6, 7, 8, 9])) + "".join(random.choices("0123456789", k=9))
        if p not in used:
            used.add(p)
            out.append(p)
    return out


def jitter(lat, lng, km=4.0):
    d = km / 111.0
    return round(lat + random.uniform(-d, d), 6), round(lng + random.uniform(-d, d), 6)


def progress(msg):
    print(f"  {msg}", flush=True)


def allocate(total, minimum):
    """Split `total` across cities by order share, giving every city at least `minimum`."""
    shares = [c[4] for c in CITIES]
    s = sum(shares)
    counts = [max(minimum, round(total * x / s)) for x in shares]
    diff = total - sum(counts)
    counts[0] += diff                      # biggest city absorbs rounding
    return counts


def person_name(state, region, male=None):
    """A realistic Indian name for someone living in `state`."""
    if male is None:
        male = random.random() < 0.5
    if state in STATE_NAMES:
        mf, ff, sur = STATE_NAMES[state]
    else:
        mf, ff, sur = NAMES[region]
    first = random.choice(mf if male else ff)
    if state == "Manipur":
        last = "Singh" if male else "Devi"
    elif state == "Punjab" and random.random() < SIKH_SHARE:
        last = "Singh" if male else "Kaur"
    else:
        last = random.choice(sur)
    return f"{first} {last}"


def is_rainy(city, state, month):
    if city in COASTAL or state in {"Kerala", "Goa"}:
        return month in (6, 7, 8, 9)
    if state in {"Tamil Nadu", "Puducherry"}:
        return month in (10, 11, 12)
    if state in NORTHEAST_STATES or state == "West Bengal":
        return month in (5, 6, 7, 8, 9)
    return month in (7, 8)


def festival_boost(state, d):
    m = 1.0
    for a, b, states, x in REGIONAL_FESTIVALS:
        if a <= d <= b and state in states:
            m *= x
    return m


def is_open(rest, hour):
    o, c = rest["open"], rest["close"]     # hours; close may be past midnight (e.g. 26 = 2 AM)
    return o <= hour < c or o <= hour + 24 < c


# ----------------------------------------------------------------------
# GENERATORS
# ----------------------------------------------------------------------
name_rng = random.Random(SEED + 1)     # restaurant names use their own random stream
ZONE_NAMES = {}


def zone_name(zid):
    return ZONE_NAMES[zid]


def gen_locations():
    cities, zones = [], []
    city_zone_ids = {}
    zid = 0
    for cid, c in enumerate(CITIES, start=1):
        name, state, lat, lng, _share, tier = c[:6]
        cities.append((cid, name, state))
        city_zone_ids[cid] = []
        spread = {1: 9, 2: 5, 3: 3}[tier]
        for zn in c[8]:
            zid += 1
            zlat, zlng = jitter(lat, lng, spread)
            zones.append((zid, cid, zn, zlat, zlng))
            ZONE_NAMES[zid] = zn
            city_zone_ids[cid].append(zid)
    return cities, zones, city_zone_ids


def city_list(total, minimum):
    out = []
    for cid, n in enumerate(allocate(total, minimum), start=1):
        out += [cid] * n
    random.shuffle(out)
    return out


def gen_customers(city_zone_ids, phones_used):
    rows, meta = [], []
    phones = unique_phones(N_CUSTOMERS, phones_used)
    email_used = set()
    for cid, city in enumerate(city_list(N_CUSTOMERS, 60), start=1):
        name_, state, *_rest = CITIES[city - 1]
        zone = random.choice(city_zone_ids[city])
        name = person_name(state, CITIES[city - 1][6])
        handle = name.lower().replace(" ", ".")
        email = f"{handle}{cid}@{random.choice(['gmail.com', 'gmail.com', 'gmail.com', 'yahoo.in', 'outlook.com', 'rediffmail.com'])}"
        # signups start before order data; growth => more recent signups.
        # smaller cities joined the apps later, so their signups skew later
        tier = CITIES[city - 1][5]
        span = (END_DATE - date(2024, 6, 1)).days
        signup = date(2024, 6, 1) + timedelta(days=int(span * (random.random() ** {1: 0.75, 2: 0.6, 3: 0.5}[tier])))
        signup = min(signup, END_DATE - timedelta(days=1))
        # activity weight: heavy-tailed -> few loyal customers, many one-timers
        activity = min(random.paretovariate(1.3), 25.0)
        if random.random() < 0.30:
            activity *= 0.05          # likely churners (order once, never return)
        rows.append((cid, name, email, phones[cid - 1], zone, signup, True))
        meta.append({"id": cid, "city": city, "zone": zone, "signup": signup, "w": activity})
    return rows, meta


def cuisine_weights(state, region):
    w = {}
    for cu, base in CUISINE_BASE.items():
        w[cu] = base * REGION_CUISINE[region].get(cu, 1.0) * STATE_CUISINE.get(state, {}).get(cu, 1.0)
    return w


def cuisine_mix(n, state, region):
    """Cuisines for a city's n restaurants, in proportion to local taste (largest remainder)."""
    cw = cuisine_weights(state, region)
    tot = sum(cw.values())
    exact = {c: n * w / tot for c, w in cw.items()}
    counts = {c: int(x) for c, x in exact.items()}
    for c in sorted(exact, key=lambda c: exact[c] - counts[c], reverse=True)[:n - sum(counts.values())]:
        counts[c] += 1
    mix = [c for c, k in counts.items() for _ in range(k)]
    random.shuffle(mix)
    return mix


def gen_restaurants(city_zone_ids):
    rows, meta = [], []
    used = set()
    cities = city_list(N_RESTAURANTS, 6)
    queues = {}
    for cid in set(cities):
        st, rg = CITIES[cid - 1][1], CITIES[cid - 1][6]
        queues[cid] = cuisine_mix(cities.count(cid), st, rg)
    for rid, city in enumerate(cities, start=1):
        name_, state, lat, lng, _s, tier, region = CITIES[city - 1][:7]
        zone = random.choice(city_zone_ids[city])
        cuisine = queues[city].pop()
        pre, suf = REST_NAMES[cuisine]
        pre = REST_NAME_REGION.get(cuisine, {}).get(region, []) * 2 + (pre if cuisine != "Bengali & Northeast" else [])
        pre = pre or REST_NAMES[cuisine][0]
        for _ in range(50):
            name = f"{name_rng.choice(pre)} {name_rng.choice(suf)}"
            if (name, city) not in used:
                break
        else:
            name = f"{name} {zone_name(zone)}"
        used.add((name, city))
        veg_only = random.random() < CUISINES[cuisine][0]
        # opening hours: breakfast places open early; ~22% stay open till 2 AM; ~4% are 24x7
        r = random.random()
        if r < 0.04 and cuisine in ("Burgers & Fast Food", "Biryani", "Pizza", "Chinese", "North Indian"):
            o, c = 0, 24
        else:
            early = cuisine in ("South Indian", "Street Food & Chaat", "Gujarati & Rajasthani", "Desserts & Bakery",
                                "Healthy & Salads")
            o = random.choice([6, 7, 7, 8]) if early else random.choice([10, 11, 11, 12])
            late = random.random() < (0.30 if tier == 1 else 0.12)
            c = 26 if late else random.choice([22, 23, 23])
        opening = f"{o:02d}:00:00"
        closing = "23:59:59" if c == 24 else f"{c % 24:02d}:{'00' if c != 23 or random.random() < 0.5 else '30'}:00"
        onboarded = rand_date(date(2019, 1, 1) if tier == 1 else date(2021, 1, 1), date(2024, 12, 31))
        quality = min(5.0, max(2.3, random.gauss(4.0, 0.42)))      # hidden true quality
        popularity = min(random.paretovariate(1.6), 6.0) * (1 + (quality - 3.5))   # capped: no single place dominates a city
        rows.append((rid, name, zone, cuisine, veg_only, 0.0, 0, opening, closing, onboarded, True))
        local_taste = cuisine_weights(state, region)[cuisine] / CUISINE_BASE[cuisine]   # e.g. biryani in Hyderabad
        meta.append({"id": rid, "city": city, "zone": zone, "cuisine": cuisine, "veg": veg_only,
                     "q": quality, "w": max(popularity, 0.05) * local_taste ** 0.6, "open": o, "close": c})
    return rows, meta


def gen_menu(rest_meta):
    rows = []
    menu_by_rest = {}
    iid = 0
    for r in rest_meta:
        dishes = CUISINES[r["cuisine"]][1]
        if r["veg"]:
            dishes = [d for d in dishes if d[2] == 1]
        k = random.randint(min(len(dishes), max(5, len(dishes) - 4)), len(dishes))
        chosen = random.sample(dishes, k)
        tier = CITIES[r["city"] - 1][5]
        # prices: smaller cities are cheaper; within a city some places are premium
        lo_hi = {1: (0.9, 1.25), 2: (0.8, 1.08), 3: (0.72, 0.98)}[tier]
        price_level = random.uniform(*lo_hi)
        menu_by_rest[r["id"]] = []
        for dish, cat, veg, lo, hi in chosen:
            iid += 1
            price = round(random.uniform(lo, hi) * price_level / 5) * 5 - 1   # 199, 249 style
            price = max(price, 19)
            pop = random.paretovariate(1.5)                  # bestsellers get higher weight
            rows.append((iid, r["id"], dish, cat, price, bool(veg), random.randint(50, 500), True))
            menu_by_rest[r["id"]].append((iid, price, pop))
    return rows, menu_by_rest


def gen_partners(city_zone_ids, phones_used):
    rows, by_city, speed = [], {}, {}
    phones = unique_phones(N_PARTNERS, phones_used)
    for pid, city in enumerate(city_list(N_PARTNERS, 8), start=1):
        name_, state, lat, lng, _s, tier, region, traffic = CITIES[city - 1][:8]
        zone = random.choice(city_zone_ids[city])
        plat, plng = jitter(lat, lng, {1: 10, 2: 6, 3: 3}[tier])
        hilly = traffic >= 1.08 and tier == 3
        vehicle = weighted_choice(["Bike", "Scooter", "Bicycle", "EV"],
                                  [70, 25, 1, 4] if hilly else [55, 25, 8, 12] if tier == 1 else [60, 28, 6, 6])
        joined = rand_date(date(2023, 1, 1), date(2025, 12, 31))
        male = random.random() < 0.95                         # delivery fleets are mostly men
        rows.append((pid, person_name(state, region, male), phones[pid - 1], zone, vehicle, plat, plng, True, joined))
        by_city.setdefault(city, []).append((pid, joined))
        base = {"Bike": 1.0, "Scooter": 1.05, "EV": 1.03, "Bicycle": 1.35}[vehicle]
        speed[pid] = base * random.uniform(0.82, 1.25)       # <1 fast, >1 slow
    return rows, by_city, speed


def gen_coupons():
    return [(i, code, typ, val, cap, mn, vf, vt, True)
            for i, (code, typ, val, cap, mn, vf, vt, _w) in enumerate(COUPONS, start=1)]


def trend(tier, t):
    """Business growth over the period; smaller cities grow faster from a smaller base."""
    return {1: 1.0 + 1.4 * t, 2: 1.0 + 2.2 * t, 3: 1.0 + 3.0 * t}[tier]


def day_weights():
    days, weights = [], []
    d = START_DATE
    total_days = (END_DATE - START_DATE).days
    while d <= END_DATE:
        t = (d - START_DATE).days / total_days
        seasonal = 1.0 + 0.08 * math.sin(2 * math.pi * (d.timetuple().tm_yday / 365.0) + 1.2)
        w = (1.0 + 1.6 * t) * seasonal * DOW_WEIGHTS[d.weekday()] * SPECIAL_DAYS.get(d, 1.0)
        days.append(d)
        weights.append(w)
        d += timedelta(days=1)
    return days, weights


def compute_discount(coupon, item_total):
    _id, code, typ, val, cap, mn, vf, vt, _active = coupon
    if typ == "PERCENT":
        disc = item_total * val / 100.0
        if cap is not None:
            disc = min(disc, cap)
    else:
        disc = val
    return round(min(disc, item_total), 2)


def hour_weights_for(region, tier):
    w = dict(HOUR_WEIGHTS)
    if region == "south":                       # idli-dosa breakfast orders
        for h in (7, 8, 9, 10):
            w[h] *= 1.6
    if tier == 1:                               # metros order late at night
        for h in (23, 0, 1):
            w[h] *= 1.4
    else:
        for h in (23, 0, 1, 2, 3, 4):
            w[h] *= 0.55
    if region == "northeast":                   # earlier dinners
        w[18] *= 1.3; w[19] *= 1.2; w[22] *= 0.7; w[23] *= 0.6
    return w


CUISINE_HOUR = {   # which cuisines people order at which time
    "breakfast": {"South Indian": 3.0, "Street Food & Chaat": 2.0, "Gujarati & Rajasthani": 2.0,
                  "Desserts & Bakery": 1.5, "Healthy & Salads": 2.0, "Biryani": 0.3, "Pizza": 0.3,
                  "Chinese": 0.3, "North Indian": 0.6},
    "evening": {"Street Food & Chaat": 2.2, "Desserts & Bakery": 1.6, "Burgers & Fast Food": 1.5,
                "Chinese": 1.3, "Biryani": 0.7, "North Indian": 0.8},
    "late": {"Burgers & Fast Food": 2.0, "Biryani": 1.8, "Pizza": 1.6, "Desserts & Bakery": 1.3,
             "South Indian": 0.5, "Healthy & Salads": 0.3},
}


def slot(hour):
    if 6 <= hour <= 10:
        return "breakfast"
    if 16 <= hour <= 18:
        return "evening"
    if hour >= 23 or hour <= 4:
        return "late"
    return None


def gen_orders(cust_meta, rest_meta, menu_by_rest, partners_by_city, partner_speed, coupon_rows):
    ncity = len(CITIES)
    # customers per city, sorted by signup, with cumulative activity weights
    cust_city = {c: [] for c in range(1, ncity + 1)}
    for c in sorted(cust_meta, key=lambda c: c["signup"]):
        cust_city[c["city"]].append(c)
    cust_sign, cust_cum = {}, {}
    for city, lst in cust_city.items():
        cust_sign[city] = [c["signup"] for c in lst]
        acc, arr = 0.0, []
        for c in lst:
            acc += c["w"]
            arr.append(acc)
        cust_cum[city] = arr

    rest_by_city = {}
    for r in rest_meta:
        rest_by_city.setdefault(r["city"], []).append(r)

    menu_cum = {}
    for rid, items in menu_by_rest.items():
        acc, arr = 0.0, []
        for _iid, _p, pop in items:
            acc += pop
            arr.append(acc)
        menu_cum[rid] = arr

    coupon_weights = [c[7] for c in COUPONS]
    days, dweights = day_weights()
    total_days = (END_DATE - START_DATE).days
    shares = [c[4] for c in CITIES]
    hours = list(range(24))
    hw_city = {cid: [hour_weights_for(CITIES[cid - 1][6], CITIES[cid - 1][5])[h] for h in hours]
               for cid in range(1, ncity + 1)}

    order_dates = sorted(random.choices(days, weights=dweights, k=N_ORDERS))

    orders, items, payments, ratings = [], [], [], []
    oi_id = 0
    rating_id = 0
    city_w_cache = {}
    for oid, d in enumerate(order_dates, start=1):
        # city for this order: base share x growth of its tier x regional festivals that day
        if d not in city_w_cache:
            t = (d - START_DATE).days / total_days
            city_w_cache = {d: [shares[i] * trend(CITIES[i][5], t) / (1.0 + 1.6 * t)
                                * festival_boost(CITIES[i][1], d) for i in range(ncity)]}
        city = random.choices(range(1, ncity + 1), weights=city_w_cache[d], k=1)[0]
        cname, state, _lat, _lng, _s, tier, region, traffic = CITIES[city - 1][:8]
        ipl = any(a <= d <= b for a, b in IPL_SEASONS)

        hw = hw_city[city]
        if ipl:
            hw = [w * (1.18 if 19 <= h <= 23 else 1.0) for h, w in enumerate(hw)]
        hour = random.choices(hours, weights=hw, k=1)[0]

        # restaurants that are open at that hour (if none, the order moves to dinner time)
        open_r = [r for r in rest_by_city[city] if is_open(r, hour)]
        if not open_r:
            hour = random.choice([19, 20, 21])
            open_r = [r for r in rest_by_city[city] if is_open(r, hour)] or rest_by_city[city]
        ts = datetime(d.year, d.month, d.day, hour, random.randint(0, 59), random.randint(0, 59))

        # customer in that city who had signed up by this date (weighted by activity)
        k = bisect.bisect_right(cust_sign[city], d) or 1
        rr = random.random() * cust_cum[city][k - 1]
        cust = cust_city[city][bisect.bisect_left(cust_cum[city], rr, 0, k)]

        # restaurant: popularity x what people eat at this time of day
        sl = slot(hour)
        aff = CUISINE_HOUR.get(sl, {})
        rest = random.choices(open_r, weights=[r["w"] * aff.get(r["cuisine"], 1.0) for r in open_r], k=1)[0]

        # basket: 1-5 distinct items, bestsellers more likely
        menu = menu_by_rest[rest["id"]]
        n_items = min(len(menu), weighted_choice([1, 2, 3, 4, 5], [36, 35, 18, 8, 3]))
        picked = set()
        while len(picked) < n_items:
            x = random.random() * menu_cum[rest["id"]][-1]
            picked.add(bisect.bisect_left(menu_cum[rest["id"]], x))
        item_total = 0.0
        basket = []
        for idx in picked:
            iid, price, _pop = menu[idx]
            qty = weighted_choice([1, 2, 3], [80, 17, 3])
            item_total += price * qty
            oi_id += 1
            basket.append((oi_id, oid, iid, qty, price))
        item_total = round(item_total, 2)

        # coupon (more likely on weekends / festivals), must be valid and meet the minimum order
        coupon_id, discount = None, 0.0
        coupon_prob = 0.22 + (0.08 if d.weekday() >= 5 else 0) + (0.05 if d in SPECIAL_DAYS else 0)
        if random.random() < coupon_prob:
            east = state in {"West Bengal", "Tripura", "Assam", "Odisha"}
            pujo = coupon_rows[12]                       # PUJO30 is only offered in the east
            c = pujo if east and pujo[6] <= d <= pujo[7] and random.random() < 0.5 \
                else weighted_choice(coupon_rows, coupon_weights)
            if c[1] == "PUJO30" and not east:
                c = None
            if c and c[6] <= d <= c[7] and item_total >= c[5]:
                coupon_id = c[0]
                discount = compute_discount(c, item_total)

        rain = is_rainy(cname, state, d.month) and random.random() < 0.45     # not every monsoon day rains
        # delivery fee: free above Rs 199 for ~35% (membership); otherwise distance based; rain and late-night surcharge
        if random.random() < 0.35 and item_total >= 199:
            delivery_fee = 0.0
        else:
            delivery_fee = float(random.choice([19, 25, 29, 35, 39]) if tier == 1 else random.choice([15, 19, 25, 29]))
            if rain:
                delivery_fee += 20
            if hour >= 23 or hour <= 4:
                delivery_fee += 15
        final = round(max(item_total - discount, 0) + delivery_fee, 2)

        # delivery partner from the same city who had already joined
        plist = [p for p, j in partners_by_city.get(city, []) if j <= d] or \
                [p for p, _j in partners_by_city.get(city, [])]
        partner = random.choice(plist)

        method = weighted_choice(PAYMENT_METHODS, PAYMENT_WEIGHTS[tier])
        # cancellations: more with cash on delivery and in heavy rain
        cancelled = random.random() < 0.035 + (0.03 if method == "COD" else 0) + (0.02 if rain else 0)
        status = "CANCELLED" if cancelled else "DELIVERED"
        promised = random.choice([30, 35, 35, 40, 45] if tier == 1 else [25, 30, 30, 35, 40])
        delivered_time = None
        if not cancelled:
            base = random.gauss(24 if tier == 1 else 21, 6)
            peak = 1.18 if hour in (13, 20, 21) else 1.0
            weekend = 1.06 if d.weekday() >= 5 else 1.0
            wet = 1.22 if rain else 1.0
            minutes = max(10, base * partner_speed[partner] * traffic * peak * weekend * wet + random.gauss(0, 4))
            delivered_time = (ts + timedelta(minutes=round(minutes, 1))).replace(microsecond=0)

        orders.append((oid, cust["id"], rest["id"], partner, coupon_id, ts, status, item_total,
                       discount, delivery_fee, final, promised, delivered_time))
        items.extend(basket)

        pstatus = ("FAILED" if method == "COD" else "REFUNDED") if cancelled else "SUCCESS"
        payments.append((oid, oid, method, final, pstatus, ts))

        # rating for some delivered orders, driven by restaurant quality and lateness
        if not cancelled and random.random() < RATING_SHARE:
            rating_id += 1
            late_by = (delivered_time - ts).total_seconds() / 60 - promised
            food = round(min(5, max(1, random.gauss(rest["q"], 0.8))))
            deliv = 5 - (1 if late_by > 0 else 0) - (1 if late_by > 10 else 0) - (1 if late_by > 20 else 0)
            deliv = int(min(5, max(1, deliv + random.choice([-1, 0, 0, 0, 1]))))
            review = None
            if random.random() < 0.25:
                review = random.choice({
                    5: ["Amazing food!", "Loved it, will order again", "Best in town", "Super tasty and hot"],
                    4: ["Good food", "Tasty, quantity could be more", "Nice packaging", "Worth the price"],
                    3: ["Average", "Okay okay", "Food was cold", "Too oily"],
                    2: ["Not good", "Delivery was late", "Too salty", "Missing items"],
                    1: ["Terrible experience", "Wrong order delivered", "Very late and cold food"],
                }[food])
            rated_at = delivered_time + timedelta(minutes=random.randint(10, 600))
            ratings.append((rating_id, oid, cust["id"], rest["id"], food, deliv, review, rated_at))

        if oid % 20000 == 0:
            progress(f"generated {oid:,} orders")

    return orders, items, payments, ratings


# ----------------------------------------------------------------------
# DATABASE
# ----------------------------------------------------------------------
INSERTS = {
    "cities": "INSERT INTO cities (city_id, city_name, state) VALUES (%s,%s,%s)",
    "zones": "INSERT INTO zones (zone_id, city_id, zone_name, latitude, longitude) VALUES (%s,%s,%s,%s,%s)",
    "customers": "INSERT INTO customers (customer_id, full_name, email, phone, zone_id, signup_date, is_active) "
                 "VALUES (%s,%s,%s,%s,%s,%s,%s)",
    "restaurants": "INSERT INTO restaurants (restaurant_id, restaurant_name, zone_id, cuisine_type, is_veg_only, "
                   "avg_rating, total_ratings, opening_time, closing_time, onboarded_on, is_active) "
                   "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
    "menu_items": "INSERT INTO menu_items (item_id, restaurant_id, item_name, category, price, is_veg, stock_qty, "
                  "is_available) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
    "delivery_partners": "INSERT INTO delivery_partners (partner_id, full_name, phone, zone_id, vehicle_type, "
                         "current_lat, current_lng, is_available, joined_on) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
    "coupons": "INSERT INTO coupons (coupon_id, coupon_code, discount_type, discount_value, max_discount, "
               "min_order_value, valid_from, valid_to, is_active) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
    "orders": "INSERT INTO orders (order_id, customer_id, restaurant_id, partner_id, coupon_id, order_time, status, "
              "item_total, discount_amount, delivery_fee, final_amount, promised_minutes, delivered_time) "
              "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
    "order_items": "INSERT INTO order_items (order_item_id, order_id, item_id, quantity, unit_price) "
                   "VALUES (%s,%s,%s,%s,%s)",
    "payments": "INSERT INTO payments (payment_id, order_id, payment_method, amount, payment_status, paid_at) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
    "ratings": "INSERT INTO ratings (rating_id, order_id, customer_id, restaurant_id, food_rating, delivery_rating, "
               "review_text, rated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
}
LOAD_ORDER = ["cities", "zones", "customers", "restaurants", "menu_items", "delivery_partners",
              "coupons", "orders", "order_items", "payments", "ratings"]


def load(data, host, user, password, database):
    import mysql.connector

    conn = mysql.connector.connect(host=host, user=user, password=password, database=database)
    cur = conn.cursor()
    progress("clearing old data ...")
    cur.execute("SET FOREIGN_KEY_CHECKS = 0")
    for t in reversed(LOAD_ORDER + ["order_status_log"]):
        cur.execute(f"TRUNCATE TABLE {t}")
    cur.execute("SET FOREIGN_KEY_CHECKS = 1")
    conn.commit()

    for t in LOAD_ORDER:
        rows = data[t]
        start = time.time()
        for i in range(0, len(rows), BATCH):
            cur.executemany(INSERTS[t], rows[i:i + BATCH])
            conn.commit()
        progress(f"{t:<18} {len(rows):>9,} rows  ({time.time() - start:.1f}s)")

    # restaurants.avg_rating / total_ratings from the ratings we just loaded
    cur.execute("""
        UPDATE restaurants r
        JOIN (SELECT restaurant_id, ROUND(AVG(food_rating), 1) AS avg_r, COUNT(*) AS n
              FROM ratings GROUP BY restaurant_id) x ON x.restaurant_id = r.restaurant_id
        SET r.avg_rating = x.avg_r, r.total_ratings = x.n
    """)
    conn.commit()
    progress("restaurant ratings updated")
    cur.close()
    conn.close()


def sql_literal(v):
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, datetime):
        return "'" + v.strftime("%Y-%m-%d %H:%M:%S") + "'"
    if isinstance(v, date):
        return "'" + v.isoformat() + "'"
    return "'" + str(v).replace("\\", "\\\\").replace("'", "''") + "'"


def write_sql(data, path, database, rows_per_insert=1000):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("-- Generated by 02_generate_data.py (seed %d)\n" % SEED)
        fh.write(f"USE {database};\nSET FOREIGN_KEY_CHECKS = 0;\nSET UNIQUE_CHECKS = 0;\nSET autocommit = 0;\nSET SQL_SAFE_UPDATES = 0;\n")
        for t in reversed(LOAD_ORDER + ["order_status_log"]):
            fh.write(f"TRUNCATE TABLE {t};\n")
        for t in LOAD_ORDER:
            head = INSERTS[t].split(" VALUES")[0]
            rows = data[t]
            for i in range(0, len(rows), rows_per_insert):
                chunk = rows[i:i + rows_per_insert]
                vals = ",\n".join("(" + ",".join(sql_literal(v) for v in r) + ")" for r in chunk)
                fh.write(f"{head} VALUES\n{vals};\n")
            fh.write("COMMIT;\n")
        fh.write("""UPDATE restaurants r
JOIN (SELECT restaurant_id, ROUND(AVG(food_rating), 1) AS avg_r, COUNT(*) AS n
      FROM ratings GROUP BY restaurant_id) x ON x.restaurant_id = r.restaurant_id
SET r.avg_rating = x.avg_r, r.total_ratings = x.n;
COMMIT;
SET FOREIGN_KEY_CHECKS = 1;
SET UNIQUE_CHECKS = 1;
SET autocommit = 1;
SELECT 'orders' AS tbl, COUNT(*) AS n FROM orders
UNION ALL SELECT 'order_items', COUNT(*) FROM order_items
UNION ALL SELECT 'ratings', COUNT(*) FROM ratings;
""")


# ----------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Generate food delivery data")
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--user", default="root")
    ap.add_argument("--database", default="food_delivery")
    ap.add_argument("--dry-run", action="store_true", help="generate only, do not touch the database")
    ap.add_argument("--sql-out", metavar="FILE", help="write a .sql file of INSERTs instead of loading")
    args = ap.parse_args()

    password = None
    if not args.dry_run and not args.sql_out:
        password = getpass.getpass(f"MySQL password for {args.user}@{args.host}: ")

    random.seed(SEED)
    t0 = time.time()
    print("Generating data ...")
    phones_used = set()
    cities, zones, city_zone_ids = gen_locations()
    customers, cust_meta = gen_customers(city_zone_ids, phones_used)
    progress(f"customers: {len(customers):,}")
    restaurants, rest_meta = gen_restaurants(city_zone_ids)
    menu, menu_by_rest = gen_menu(rest_meta)
    progress(f"restaurants: {len(restaurants):,}, menu items: {len(menu):,}")
    partners, partners_by_city, partner_speed = gen_partners(city_zone_ids, phones_used)
    coupons = gen_coupons()
    orders, items, payments, ratings = gen_orders(cust_meta, rest_meta, menu_by_rest,
                                                  partners_by_city, partner_speed, coupons)

    data = {"cities": cities, "zones": zones, "customers": customers, "restaurants": restaurants,
            "menu_items": menu, "delivery_partners": partners, "coupons": coupons, "orders": orders,
            "order_items": items, "payments": payments, "ratings": ratings}
    print("Summary:")
    for t in LOAD_ORDER:
        progress(f"{t:<18} {len(data[t]):>9,}")
    print(f"Generated in {time.time() - t0:.1f}s")

    if args.sql_out:
        write_sql(data, args.sql_out, args.database)
        print(f"Wrote {args.sql_out}")
        return data
    if args.dry_run:
        return data

    print("Loading into MySQL ...")
    try:
        load(data, args.host, args.user, password, args.database)
    except Exception as e:  # noqa: BLE001
        print(f"\nERROR while loading: {e}")
        sys.exit(1)
    print(f"Done in {time.time() - t0:.1f}s. Open Workbench and run: SELECT COUNT(*) FROM orders;")
    return data


if __name__ == "__main__":
    main()
