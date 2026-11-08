"""
IndiePulse - server.py
Multilingual voice billing, logistics matcher, wealth analytics, FMCG expiry desk,
quick-commerce kirana bridge and last-mile translation matrix.

Run:   pip install flask
       python server.py          -> http://localhost:5000

Config (environment variables, or a local .env file next to this script - never commit it):
  GNANI_API_KEY        Gnani Vachana key. Sent ONLY server-side as the X-API-Key-ID header.
  GNANI_BASE_URL       default https://api.vachana.ai
  GNANI_STT_MODEL      default prisma-v3.0
  GNANI_TRANSLATE_URL  optional: plug in a translation endpoint when you have one (see gnani_translate)
  QC_API_KEY           shared secret for the Blinkit/Zepto bridge (X-API-Key header)
  OTP_DEMO             "1" (default) = fixed demo OTP 1234. Set "0" once an SMS provider is wired in send_sms()
  LIBRETRANSLATE_URL   optional free-form translation fallback
  INDIEPULSE_DB        SQLite path (default ./indiepulse.db)
  HTTPS=1              serve over HTTPS with a throw-away self-signed certificate (needs: pip install cryptography).
                       REQUIRED for microphone/clipboard when you open the site from another device or a LAN IP.
  SSL_CERT, SSL_KEY    use your own certificate files instead (e.g. made with mkcert)
  PORT                 default 5000

NOTE: Speech-to-text happens in the browser (Web Speech API). This server parses the
transcript, so it works with any STT you plug in later. All seed data (drivers, phones,
analytics, stock) is DEMO data - replace before production.
"""
import os
import re
import json
import math
import hmac
import hashlib
import secrets
import sqlite3
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

from flask import Flask, g, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_dotenv(path):
    """Tiny .env reader (no dependency). Real environment variables win over the file."""
    try:
        with open(path, encoding="utf-8-sig") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except FileNotFoundError:
        pass


_load_dotenv(os.path.join(BASE_DIR, ".env"))
DB_PATH = os.environ.get("INDIEPULSE_DB", os.path.join(BASE_DIR, "indiepulse.db"))
QC_API_KEY = os.environ.get("QC_API_KEY", "demo-qc-key")
LIBRETRANSLATE_URL = os.environ.get("LIBRETRANSLATE_URL", "").rstrip("/")

app = Flask(__name__, static_folder=None)
app.config["JSON_AS_ASCII"] = False
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024  # audio uploads
app.json.ensure_ascii = False  # keep Indic scripts readable in JSON

LANGS = ["en", "hi", "kn", "ta", "te", "mr"]

# --------------------------------------------------------------------------------------
# Reference data
# --------------------------------------------------------------------------------------
def C(mandi, retail, wholesale, gst, unit="kg"):
    return dict(mandi=mandi, retail=retail, wholesale=wholesale, gst=gst, unit=unit)

# Prices are demo defaults (INR). GST rates are demo defaults - verify before real invoicing.
CATALOG = {
    "tomato": C(24, 36, 30, 0), "onion": C(28, 40, 34, 0), "potato": C(22, 32, 27, 0),
    "rice": C(46, 62, 54, 0), "wheat": C(30, 42, 35, 0), "sugar": C(40, 48, 44, 5),
    "dal": C(105, 140, 122, 0), "milk": C(48, 56, 52, 0, "litre"),
    "curd": C(None, 30, 26, 5, "packet"), "bread": C(None, 40, 34, 0, "packet"),
    "biscuits": C(None, 10, 8.5, 18, "packet"), "chips": C(None, 20, 17, 12, "packet"),
    "noodles": C(None, 14, 12, 18, "packet"), "oil": C(None, 150, 132, 5, "litre"),
    "soap": C(None, 40, 34, 18, "pcs"),
}
ALIASES = {
    "tomatoes": "tomato", "potatoes": "potato", "onions": "onion", "biscuit": "biscuits",
    "chip": "chips", "noodle": "noodles", "aloo": "potato", "pyaz": "onion", "pyaaz": "onion",
    "chawal": "rice", "chini": "sugar", "doodh": "milk", "gehun": "wheat", "tel": "oil",
    "dahi": "curd", "dals": "dal",
}
UNIT_TO_KG = {"kg": 1, "quintal": 100, "ton": 1000}

# English key -> regional display names
ITEM_I18N = {
    "tomato": dict(hi="टमाटर", kn="ಟೊಮೇಟೊ", ta="தக்காளி", te="టమాటా", mr="टोमॅटो"),
    "onion": dict(hi="प्याज", kn="ಈರುಳ್ಳಿ", ta="வெங்காயம்", te="ఉల్లిపాయ", mr="कांदा"),
    "potato": dict(hi="आलू", kn="ಆಲೂಗಡ್ಡೆ", ta="உருளைக்கிழங்கு", te="బంగాళాదుంప", mr="बटाटा"),
    "rice": dict(hi="चावल", kn="ಅಕ್ಕಿ", ta="அரிசி", te="బియ్యం", mr="तांदूळ"),
    "wheat": dict(hi="गेहूं", kn="ಗೋಧಿ", ta="கோதுமை", te="గోధుమ", mr="गहू"),
    "sugar": dict(hi="चीनी", kn="ಸಕ್ಕರೆ", ta="சர்க்கரை", te="చక్కెర", mr="साखर"),
    "milk": dict(hi="दूध", kn="ಹಾಲು", ta="பால்", te="పాలు", mr="दूध"),
    "curd": dict(hi="दही", kn="ಮೊಸರು", ta="தயிர்", te="పెరుగు", mr="दही"),
    "oil": dict(hi="तेल", kn="ಎಣ್ಣೆ", ta="எண்ணெய்", te="నూనె", mr="तेल"),
    "dal": dict(hi="दाल", kn="ಬೇಳೆ", ta="பருப்பு", te="పప్పు", mr="डाळ"),
    "biscuits": dict(hi="बिस्कुट", kn="ಬಿಸ್ಕತ್ತು", ta="பிஸ்கட்", te="బిస్కెట్", mr="बिस्कीट"),
    "chips": dict(hi="चिप्स", kn="ಚಿಪ್ಸ್", ta="சிப்ஸ்", te="చిప్స్", mr="चिप्स"),
    "noodles": dict(hi="नूडल्स", kn="ನೂಡಲ್ಸ್", ta="நூடுல்ஸ்", te="నూడుల్స్", mr="नूडल्स"),
    "soap": dict(hi="साबुन", kn="ಸಾಬೂನು", ta="சோப்பு", te="సబ్బు", mr="साबण"),
    "bread": dict(hi="ब्रेड", kn="ಬ್ರೆಡ್", ta="பிரெட்", te="బ్రెడ్", mr="ब्रेड"),
}
EXTRA_ITEM_WORDS = {"ಟೊಮೆಟೊ": "tomato", "ಟೊಮ್ಯಾಟೊ": "tomato", "ಟೊಮೆಟೋ": "tomato", "ಈರುಳ್ಳಿಯನ್ನು": "onion", "ಹಾಲನ್ನು": "milk", "गेहूँ": "wheat", "शक्कर": "sugar", "प्याज़": "onion"}

UNIT_I18N = {
    "kg": ["किलोग्राम", "किलो", "केजी", "के.जी", "ಕೆ.ಜಿ", "ಕಿಲೋಗ್ರಾಂ", "ಕೆಜಿ", "ಕಿಲೋ", "கிலோ", "కిలో", "కేజీ", "किग्रा"],
    "quintal": ["क्विंटल", "ಕ್ವಿಂಟಾಲ್", "குவிண்டால்", "క్వింటాల్"],
    "ton": ["टन", "ಟನ್", "டன்", "టన్"],
    "litre": ["लीटर", "ಲೀಟರ್", "லிட்டர்", "లీటర్"],
    "packet": ["पैकेट", "ಪ್ಯಾಕೆಟ್", "பாக்கெட்", "ప్యాకెట్"],
    "pcs": ["नग", "ಪೀಸ್"],
}
RUPEE_WORDS = ["रुपये", "रुपए", "रुपया", "ರೂಪಾಯಿ", "ರೂ", "ரூபாய்", "ரூ", "రూపాయలు", "రూపాయి", "రూ"]
AND_WORDS = ["और", "ಮತ್ತು", "மற்றும்", "మరియు", "आणि"]
AT_WORDS = ["भाव", "दर", "ದರ", "ರೇಟ್", "விலை", "ధర"]

REGIONAL_NUMS = {}
for _n, _ws in {
    1: "एक ಒಂದು ಒಂದ ஒன்று ஒரு ఒకటి ఒక", 2: "दो दोन ಎರಡು ಎರಡ இரண்டு రెండు", 3: "तीन ಮೂರು மூன்று మూడు",
    4: "चार ನಾಲ್ಕು நான்கு నాలుగు", 5: "पांच पाँच पाच ಐದು ஐந்து ఐదు", 6: "छह छः छे सहा ಆರು ஆறு ఆరు",
    7: "सात ಏಳು ஏழு ఏడు", 8: "आठ ಎಂಟು எட்டு ఎనిమిది", 9: "नौ नऊ ಒಂಬತ್ತು ஒன்பது తొమ్మిది",
    10: "दस दहा ಹತ್ತು பத்து పది", 20: "बीस वीस ಇಪ್ಪತ್ತು இருபது ఇరవై", 30: "तीस ಮೂವತ್ತು முப்பது ముప్పై",
    40: "चालीस चाळीस ನಲವತ್ತು நாற்பது నలభై", 50: "पचास पन्नास ಐವತ್ತು ஐம்பது యాభై",
    100: "सौ शंभर ನೂರು நூறு వంద"}.items():
    for _w in _ws.split():
        REGIONAL_NUMS[_w] = _n

REGIONAL_LOOKUP = []
for _k, _tr in ITEM_I18N.items():
    for _v in _tr.values():
        REGIONAL_LOOKUP.append((_v, _k))
REGIONAL_LOOKUP += [(w, k) for w, k in EXTRA_ITEM_WORDS.items()]
for _canon, _words in UNIT_I18N.items():
    REGIONAL_LOOKUP += [(w, _canon) for w in _words]
REGIONAL_LOOKUP += [(w, "rs") for w in RUPEE_WORDS] + [(w, "and") for w in AND_WORDS] + [(w, "at") for w in AT_WORDS]
REGIONAL_LOOKUP.sort(key=lambda x: -len(x[0]))

LATIN_UNIT_MAP = {
    "kilo": "kg", "kilos": "kg", "kilogram": "kg", "kilograms": "kg", "kgs": "kg",
    "quintals": "quintal", "tonne": "ton", "tonnes": "ton", "tons": "ton",
    "litres": "litre", "liter": "litre", "liters": "litre", "ltr": "litre",
    "packets": "packet", "pkt": "packet", "pieces": "pcs", "piece": "pcs", "pc": "pcs",
    "rupees": "rs", "rupee": "rs", "rupaye": "rs", "rupaiye": "rs",
}
UNITS = {"kg", "quintal", "ton", "litre", "packet", "pcs", "dozen", "box"}
NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
             "nine": 9, "ten": 10, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "hundred": 100}
STOP = {"rs", "at", "for", "per", "of", "and", "rate", "each", "the", "a", "an", "to", "sold", "sell", "bill",
        "invoice", "i", "please", "make", "bought", "buy", "with", "total", "is", "was", "price", "from",
        "me", "my", "give", "gave", "customer", "buyer", "party", "farmer", "seller", "trader"}
NAME_STOP = STOP | UNITS | {"vehicle", "truck", "lorry", "number", "no", "load", "fare", "carrying", "weight",
                            "driver", "name", "named", "called", "freight", "charge", "charges", "rent", "then",
                            "plus", "transport", "trip"}

# --------------------------------------------------------------------------------------
# Phrase book (offline translation for delivery / quick-commerce). Review with native speakers.
# --------------------------------------------------------------------------------------
PHRASES = {
    "where_are_you": dict(en="Where are you?", hi="आप कहाँ हैं?", kn="ನೀವು ಎಲ್ಲಿದ್ದೀರಿ?", ta="நீங்கள் எங்கே இருக்கிறீர்கள்?", te="మీరు ఎక్కడ ఉన్నారు?", mr="तुम्ही कुठे आहात?"),
    "coming_5": dict(en="I will reach in 5 minutes.", hi="मैं 5 मिनट में पहुँच रहा हूँ।", kn="ನಾನು 5 ನಿಮಿಷದಲ್ಲಿ ತಲುಪುತ್ತೇನೆ.", ta="நான் 5 நிமிடத்தில் வந்து சேர்வேன்.", te="నేను 5 నిమిషాల్లో చేరుకుంటాను.", mr="मी 5 मिनिटांत पोहोचतो."),
    "reached": dict(en="I have reached your location.", hi="मैं आपकी जगह पर पहुँच गया हूँ।", kn="ನಾನು ನಿಮ್ಮ ಸ್ಥಳಕ್ಕೆ ತಲುಪಿದ್ದೇನೆ.", ta="நான் உங்கள் இடத்திற்கு வந்துவிட்டேன்.", te="నేను మీ చోటుకు చేరుకున్నాను.", mr="मी तुमच्या ठिकाणी पोहोचलो आहे."),
    "please_wait": dict(en="Please wait a moment.", hi="कृपया थोड़ा इंतज़ार करें।", kn="ದಯವಿಟ್ಟು ಸ್ವಲ್ಪ ಕಾಯಿರಿ.", ta="தயவுசெய்து சிறிது காத்திருங்கள்.", te="దయచేసి కొంచెం వేచి ఉండండి.", mr="कृपया थोडा वेळ थांबा."),
    "call_me": dict(en="Please call me.", hi="कृपया मुझे फ़ोन करें।", kn="ದಯವಿಟ್ಟು ನನಗೆ ಕರೆ ಮಾಡಿ.", ta="தயவுசெய்து எனக்கு அழைக்கவும்.", te="దయచేసి నాకు కాల్ చేయండి.", mr="कृपया मला फोन करा."),
    "share_address": dict(en="Please share the exact address.", hi="कृपया सही पता बताइए।", kn="ದಯವಿಟ್ಟು ನಿಖರ ವಿಳಾಸ ತಿಳಿಸಿ.", ta="தயவுசெய்து சரியான முகவரியைச் சொல்லுங்கள்.", te="దయచేసి ఖచ్చితమైన చిరునామా చెప్పండి.", mr="कृपया अचूक पत्ता सांगा."),
    "need_otp": dict(en="Please tell me the OTP.", hi="कृपया OTP बताइए।", kn="ದಯವಿಟ್ಟು OTP ಹೇಳಿ.", ta="தயவுசெய்து OTP சொல்லுங்கள்.", te="దయచేసి OTP చెప్పండి.", mr="कृपया OTP सांगा."),
    "cash_online": dict(en="Will you pay cash or online?", hi="आप कैश देंगे या ऑनलाइन?", kn="ನೀವು ನಗದು ಕೊಡುತ್ತೀರಾ ಅಥವಾ ಆನ್‌ಲೈನ್?", ta="ரொக்கமா அல்லது ஆன்லைனா?", te="మీరు నగదు ఇస్తారా లేక ఆన్‌లైన్?", mr="तुम्ही रोख द्याल की ऑनलाइन?"),
    "thank_you": dict(en="Thank you!", hi="धन्यवाद!", kn="ಧನ್ಯವಾದಗಳು!", ta="நன்றி!", te="ధన్యవాదాలు!", mr="धन्यवाद!"),
    "sorry_late": dict(en="Sorry for the delay.", hi="देरी के लिए क्षमा करें।", kn="ತಡವಾಗಿದ್ದಕ್ಕೆ ಕ್ಷಮಿಸಿ.", ta="தாமதத்திற்கு மன்னிக்கவும்.", te="ఆలస్యానికి క్షమించండి.", mr="उशीर झाल्याबद्दल क्षमस्व."),
    "order_confirmed": dict(en="Your order is confirmed.", hi="आपका ऑर्डर पक्का हो गया है।", kn="ನಿಮ್ಮ ಆರ್ಡರ್ ದೃಢಪಟ್ಟಿದೆ.", ta="உங்கள் ஆர்டர் உறுதி செய்யப்பட்டது.", te="మీ ఆర్డర్ ధృవీకరించబడింది.", mr="तुमची ऑर्डर निश्चित झाली आहे."),
    "out_of_stock": dict(en="Sorry, this item is out of stock.", hi="क्षमा करें, यह सामान स्टॉक में नहीं है।", kn="ಕ್ಷಮಿಸಿ, ಈ ವಸ್ತು ಸ್ಟಾಕ್‌ನಲ್ಲಿ ಇಲ್ಲ.", ta="மன்னிக்கவும், இந்த பொருள் கையிருப்பில் இல்லை.", te="క్షమించండి, ఈ వస్తువు స్టాక్‌లో లేదు.", mr="क्षमस्व, हा माल स्टॉकमध्ये नाही."),
}
TEMPLATES = {
    "new_order": dict(
        en="New {platform} order {ref}: {items}. Total ₹{total}. Accept?",
        hi="नया {platform} ऑर्डर {ref}: {items}। कुल ₹{total}। स्वीकार करें?",
        kn="ಹೊಸ {platform} ಆರ್ಡರ್ {ref}: {items}. ಒಟ್ಟು ₹{total}. ಒಪ್ಪಿಕೊಳ್ಳುವಿರಾ?",
        ta="புதிய {platform} ஆர்டர் {ref}: {items}. மொத்தம் ₹{total}. ஏற்கிறீர்களா?",
        te="కొత్త {platform} ఆర్డర్ {ref}: {items}. మొత్తం ₹{total}. అంగీకరిస్తారా?",
        mr="नवीन {platform} ऑर्डर {ref}: {items}. एकूण ₹{total}. स्वीकारता का?"),
    "accepted": dict(en="Order accepted.", hi="ऑर्डर स्वीकार किया गया।", kn="ಆರ್ಡರ್ ಒಪ್ಪಿಕೊಳ್ಳಲಾಗಿದೆ.", ta="ஆர்டர் ஏற்கப்பட்டது.", te="ఆర్డర్ అంగీకరించబడింది.", mr="ऑर्डर स्वीकारली."),
    "rejected": dict(en="Order rejected.", hi="ऑर्डर अस्वीकार किया गया।", kn="ಆರ್ಡರ್ ತಿರಸ್ಕರಿಸಲಾಗಿದೆ.", ta="ஆர்டர் நிராகரிக்கப்பட்டது.", te="ఆర్డర్ తిరస్కరించబడింది.", mr="ऑर्डर नाकारली."),
    "unclear": dict(en="Please say yes or no.", hi="कृपया हाँ या नहीं कहें।", kn="ದಯವಿಟ್ಟು ಹೌದು ಅಥವಾ ಇಲ್ಲ ಎನ್ನಿ.", ta="தயவுசெய்து ஆம் அல்லது இல்லை என்று சொல்லுங்கள்.", te="దయచేసి అవును లేదా కాదు అనండి.", mr="कृपया हो किंवा नाही म्हणा."),
}
ACCEPT_WORDS = ["yes", "ok", "okay", "accept", "confirm", "sure", "haan", "han", "sari", "sare", "avunu", "aam",
                "हाँ", "हां", "ठीक", "स्वीकार", "हो", "ಹೌದು", "ಸರಿ", "ಒಪ್ಪಿ", "ஆம்", "சரி", "ஏற்", "అవును", "సరే", "అంగీకర"]
REJECT_WORDS = ["no", "reject", "cancel", "nahi", "nahin", "illa", "beda", "vendam", "kadu", "vaddu", "nahi",
                "नहीं", "नही", "रद्द", "नाही", "ಇಲ್ಲ", "ಬೇಡ", "இல்லை", "வேண்டாம்", "కాదు", "వద్దు", "లేదు"]

BILL_TYPES = {
    "farmer_invoice": dict(role="farmer", prefix="FRM", title="Farm Produce Sales Receipt"),
    "transport_waybill": dict(role="transporter", prefix="WAY", title="Transport Waybill / Trip Receipt"),
    "b2b_invoice": dict(role="wholesaler", prefix="B2B", title="Tax Invoice (B2B)"),
    "kirana_bill": dict(role="kirana", prefix="KIR", title="Retail Bill"),
}
META_FIELDS = {
    "farmer_invoice": ["seller", "mandi"],
    "transport_waybill": ["driver_name", "vehicle_no", "origin", "destination", "load_kg"],
    "b2b_invoice": ["buyer_gstin", "interstate"],
    "kirana_bill": ["customer_phone"],
}
ROLE_LABELS = {"farmer": "Farmer", "driver": "Driver / Transporter", "wholesaler": "Wholesaler",
               "fmcg": "FMCG Distributor", "kirana": "Kirana Store"}

CITIES = {  # lat, lon
    "Bengaluru": (12.9716, 77.5946), "Mysuru": (12.2958, 76.6394), "Kolar": (13.1360, 78.1291),
    "Tumakuru": (13.3379, 77.1173), "Mandya": (12.5218, 76.8951), "Hosur": (12.7409, 77.8253),
    "Chikkaballapur": (13.4355, 77.7315), "Hubballi": (15.3647, 75.1240), "Davangere": (14.4644, 75.9218),
    "Chennai": (13.0827, 80.2707), "Hyderabad": (17.3850, 78.4867), "Pune": (18.5204, 73.8567),
}

# --------------------------------------------------------------------------------------
# DB
# --------------------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS bills(id INTEGER PRIMARY KEY AUTOINCREMENT, bill_no TEXT UNIQUE, role TEXT, bill_type TEXT,
  party TEXT, subtotal REAL, tax REAL, total REAL, meta TEXT, language TEXT, raw_transcript TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS bill_items(id INTEGER PRIMARY KEY AUTOINCREMENT, bill_id INTEGER, name TEXT, qty REAL,
  unit TEXT, rate REAL, gst_rate REAL, amount REAL);
CREATE TABLE IF NOT EXISTS drivers(id INTEGER PRIMARY KEY, name TEXT, vehicle_type TEXT, plate TEXT, phone TEXT,
  city TEXT, capacity_kg REAL, rate_per_km REAL, lanes TEXT, rating REAL, languages TEXT);
CREATE TABLE IF NOT EXISTS dispatches(id INTEGER PRIMARY KEY AUTOINCREMENT, driver_id INTEGER, lane TEXT, origin TEXT,
  destination TEXT, weight_kg REAL, fare REAL, status TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS distributors(id INTEGER PRIMARY KEY, name TEXT, category TEXT, phone TEXT);
CREATE TABLE IF NOT EXISTS fmcg_inventory(id INTEGER PRIMARY KEY AUTOINCREMENT, owner_role TEXT, store_name TEXT, sku TEXT,
  name TEXT, brand TEXT, category TEXT, batch TEXT, qty INTEGER, reorder_level INTEGER, mrp REAL, cost REAL,
  expiry_date TEXT, distributor_id INTEGER);
CREATE TABLE IF NOT EXISTS kirana_stores(store_id TEXT PRIMARY KEY, name TEXT, pincode TEXT, lang TEXT, phone TEXT);
CREATE TABLE IF NOT EXISTS kirana_stock(id INTEGER PRIMARY KEY AUTOINCREMENT, store_id TEXT, sku TEXT, item_key TEXT,
  name TEXT, qty INTEGER, price REAL, updated_at TEXT, UNIQUE(store_id, sku));
CREATE TABLE IF NOT EXISTS qc_orders(id INTEGER PRIMARY KEY AUTOINCREMENT, order_ref TEXT, platform TEXT, store_id TEXT,
  customer_area TEXT, items TEXT, total REAL, status TEXT, created_at TEXT, decided_at TEXT);
CREATE TABLE IF NOT EXISTS role_metrics(role TEXT, idx INTEGER, profit_before REAL, profit_after REAL,
  idle_capital REAL, spoilage_value REAL, PRIMARY KEY(role, idx));
CREATE TABLE IF NOT EXISTS role_kpis(role TEXT, key TEXT, label TEXT, before REAL, after REAL, unit TEXT, lower_better INTEGER,
  PRIMARY KEY(role, key));
CREATE TABLE IF NOT EXISTS users(phone TEXT PRIMARY KEY, created_at TEXT, last_login TEXT);
CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY, phone TEXT, created_at INTEGER, expires_at INTEGER, role TEXT);
CREATE TABLE IF NOT EXISTS accounts(phone TEXT, role TEXT, created_at TEXT, last_login TEXT, PRIMARY KEY(phone, role));
CREATE TABLE IF NOT EXISTS otp_state(phone TEXT PRIMARY KEY, otp_hash TEXT, expires_at INTEGER, fails INTEGER DEFAULT 0, locked_until INTEGER DEFAULT 0, sent_at INTEGER DEFAULT 0);
"""

SEED_METRICS = {
    "farmer": dict(before=[18000, 17500, 19000, 18500, 18200, 18800], after=[19500, 22800, 25500, 28900, 31200, 33600],
                   idle=[60000, 52000, 44000, 36000, 30000, 25000], spoil=[9000, 7600, 6000, 4700, 3500, 2600]),
    "wholesaler": dict(before=[92000, 95000, 90000, 94000, 93000, 96000], after=[98000, 108000, 118000, 127000, 135000, 144000],
                       idle=[450000, 400000, 340000, 300000, 255000, 220000], spoil=[22000, 18000, 14000, 10500, 8000, 6000]),
    "fmcg": dict(before=[210000, 205000, 215000, 208000, 212000, 214000], after=[224000, 240000, 258000, 276000, 291000, 305000],
                 idle=[900000, 820000, 720000, 640000, 560000, 490000], spoil=[48000, 40000, 31000, 24000, 18000, 13000]),
    "kirana": dict(before=[26000, 25500, 26500, 26000, 25800, 26200], after=[27500, 30500, 34000, 37000, 39500, 42000],
                   idle=[120000, 104000, 90000, 78000, 66000, 56000], spoil=[6500, 5400, 4300, 3400, 2700, 2100]),
    "driver": dict(before=[24000, 23500, 24500, 24000, 23800, 24300], after=[25500, 28000, 30500, 33000, 35000, 36400],
                   idle=[0] * 6, spoil=[0] * 6),
}
SEED_KPIS = [
    ("farmer", "middleman_savings", "Middleman commission saved (per month)", 8200, 800, "inr", 1),
    ("wholesaler", "regional_growth", "Regional customer revenue growth", 0, 12.5, "pct", 0),
    ("fmcg", "regional_growth", "Regional customer revenue growth", 0, 9.8, "pct", 0),
    ("kirana", "regional_growth", "Regional customer revenue growth", 0, 18.4, "pct", 0),
    ("driver", "idle_days", "Idle days per month", 9, 3, "days", 1),
    ("driver", "return_load_fill", "Return-trip load fill", 20, 58, "pct", 0),
]


def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def rows(sql, args=()):
    return [dict(r) for r in db().execute(sql, args).fetchall()]


def one(sql, args=()):
    r = db().execute(sql, args).fetchone()
    return dict(r) if r else None


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    c = conn.cursor()
    if "role" not in [r[1] for r in c.execute("PRAGMA table_info(sessions)")]:   # DBs created by the previous version
        c.execute("ALTER TABLE sessions ADD COLUMN role TEXT")
    if c.execute("SELECT COUNT(*) FROM drivers").fetchone()[0] == 0:
        c.executemany("INSERT INTO drivers VALUES(?,?,?,?,?,?,?,?,?,?,?)", [
            (1, "Ramesh Gowda", "Tata Ace (mini truck)", "KA05MN2201", "9000000001", "Kolar", 1000, 14, "farmer-wholesaler,wholesaler-kirana", 4.6, "kn,hi,en"),
            (2, "Manjunath R", "Eicher 14ft", "KA09AB4410", "9000000002", "Tumakuru", 4000, 22, "farmer-wholesaler,fmcg-wholesaler", 4.4, "kn,en"),
            (3, "Syed Imran", "Bolero Pickup", "KA03CD7788", "9000000003", "Bengaluru", 1500, 16, "wholesaler-kirana,fmcg-wholesaler", 4.7, "hi,kn,en"),
            (4, "Venkatesh P", "Tata 407", "KA51EF9012", "9000000004", "Hosur", 2500, 19, "wholesaler-kirana,farmer-wholesaler", 4.3, "ta,kn,te"),
            (5, "Lokesh Kumar", "3-Wheeler Cargo", "KA02GH3345", "9000000005", "Bengaluru", 600, 11, "wholesaler-kirana", 4.8, "kn,hi"),
            (6, "Prakash Naik", "10-wheel truck", "KA25JK5566", "9000000006", "Hubballi", 12000, 38, "fmcg-wholesaler,farmer-wholesaler", 4.5, "kn,mr,hi"),
        ])
    if c.execute("SELECT COUNT(*) FROM distributors").fetchone()[0] == 0:
        c.executemany("INSERT INTO distributors VALUES(?,?,?,?)", [
            (1, "Sri Lakshmi Dairy Agencies", "dairy", "9000000011"),
            (2, "Crunch Snacks Distribution", "snacks", "9000000012"),
            (3, "CoolSip Beverage Agencies", "beverages", "9000000013"),
            (4, "Prime Staples & FMCG", "general", "9000000014"),
        ])
    if c.execute("SELECT COUNT(*) FROM fmcg_inventory").fetchone()[0] == 0:
        today = date.today()
        inv = [
            ("MILK500", "Toned Milk 500ml", "FreshFarm", "dairy", "B2311A", 42, 20, 27, 24.5, 2, 1),
            ("CURD400", "Curd 400g", "FreshFarm", "dairy", "B2311C", 18, 12, 30, 26, 5, 1),
            ("PANEER200", "Paneer 200g", "FreshFarm", "dairy", "B2310P", 9, 6, 95, 82, 9, 1),
            ("BUTTER100", "Butter 100g", "FreshFarm", "dairy", "B2311T", 8, 6, 58, 51, 14, 1),
            ("BREAD400", "Sandwich Bread", "BakeWell", "bakery", "B2311B", 12, 10, 40, 34, 1, 4),
            ("CAKE60", "Sponge Cake Slice", "BakeWell", "bakery", "B2310K", 15, 10, 25, 20, -1, 4),
            ("BISC-FAM", "Family Biscuits", "CrunchO", "snacks", "B2308F", 35, 15, 10, 8.5, 20, 2),
            ("CHIPS52", "Potato Chips 52g", "CrunchO", "snacks", "B2309C", 60, 25, 20, 16.5, 12, 2),
            ("NOOD70", "Instant Noodles 70g", "QuickNoodle", "snacks", "B2307N", 80, 30, 14, 11.5, 75, 4),
            ("COLA750", "Cola 750ml", "FizzUp", "beverages", "B2310D", 24, 12, 40, 33, 28, 3),
            ("JUICE1L", "Fruit Juice 1L", "SunPress", "beverages", "B2310J", 6, 10, 110, 92, 8, 3),
        ]
        c.executemany(
            "INSERT INTO fmcg_inventory(owner_role,store_name,sku,name,brand,category,batch,qty,reorder_level,mrp,cost,expiry_date,distributor_id)"
            " VALUES('kirana','Ganesh Kirana',?,?,?,?,?,?,?,?,?,?,?)",
            [(s, n, b, cat, bt, q, rl, mrp, cost, (today + timedelta(days=d)).isoformat(), dist)
             for s, n, b, cat, bt, q, rl, mrp, cost, d, dist in inv])
    if c.execute("SELECT COUNT(*) FROM kirana_stores").fetchone()[0] == 0:
        stores = [("KIR-001", "Ganesh Kirana", "560034", "kn", "9000000021"),
                  ("KIR-002", "Meenakshi Stores", "600017", "ta", "9000000022"),
                  ("KIR-003", "Sri Sai Provisions", "500072", "te", "9000000023"),
                  ("KIR-004", "Shivaji Kirana", "411001", "mr", "9000000024"),
                  ("KIR-005", "Gupta General Store", "560034", "hi", "9000000025")]
        c.executemany("INSERT INTO kirana_stores VALUES(?,?,?,?,?)", stores)
        base_items = [("MILK500", "milk", "Milk 500ml", 27, 30), ("CURD400", "curd", "Curd 400g", 30, 18),
                      ("BREAD400", "bread", "Bread", 40, 12), ("BISC-FAM", "biscuits", "Biscuits", 10, 60),
                      ("CHIPS52", "chips", "Chips 52g", 20, 45), ("NOOD70", "noodles", "Noodles 70g", 14, 50),
                      ("OIL1L", "oil", "Cooking oil 1L", 150, 14), ("RICE1KG", "rice", "Rice 1kg", 62, 40),
                      ("SUGAR1KG", "sugar", "Sugar 1kg", 48, 35), ("ONION1KG", "onion", "Onion 1kg", 40, 25)]
        offs = {"KIR-001": 0, "KIR-002": 5, "KIR-003": -8, "KIR-004": 3, "KIR-005": -12}
        for sid, off in offs.items():
            for sku, key, nm, price, q in base_items:
                qty = max(0, q + off)
                if sid == "KIR-003" and key == "bread":
                    qty = 0
                if sid == "KIR-003" and key == "noodles":
                    qty = 2
                c.execute("INSERT INTO kirana_stock(store_id,sku,item_key,name,qty,price,updated_at) VALUES(?,?,?,?,?,?,?)",
                          (sid, sku, key, nm, qty, price, now_iso()))
    # per-role seeding so databases created by older versions also gain newly added roles
    for role, m in SEED_METRICS.items():
        if c.execute("SELECT COUNT(*) FROM role_metrics WHERE role=?", (role,)).fetchone()[0] == 0:
            for i in range(6):
                c.execute("INSERT INTO role_metrics VALUES(?,?,?,?,?,?)",
                          (role, i, m["before"][i], m["after"][i], m["idle"][i], m["spoil"][i]))
    c.executemany("INSERT OR IGNORE INTO role_kpis VALUES(?,?,?,?,?,?,?)", SEED_KPIS)
    conn.commit()
    conn.close()


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------
def bad(msg, code=400):
    return jsonify(error=msg), code


def num(x, default=0.0, lo=0.0, hi=1e9):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    if math.isnan(v) or math.isinf(v):
        return default
    return min(max(v, lo), hi)


def clean_str(x, n=60):
    return re.sub(r"[\x00-\x1f]", " ", str(x or "")).strip()[:n]


def r2(x):
    return round(float(x) + 1e-9, 2)


def wa_link(phone, text):
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 10:
        digits = "91" + digits
    base = f"https://wa.me/{digits}" if digits else "https://wa.me/"
    return f"{base}?text={urllib.parse.quote(text)}"


def haversine_km(a, b):
    (la1, lo1), (la2, lo2) = a, b
    p1, p2 = math.radians(la1), math.radians(la2)
    dphi, dl = p2 - p1, math.radians(lo2 - lo1)
    h = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def road_km(c1, c2):
    if c1 in CITIES and c2 in CITIES:
        return round(haversine_km(CITIES[c1], CITIES[c2]) * 1.3, 1)  # straight-line x road factor
    return None


@app.errorhandler(Exception)
def handle_err(e):
    if isinstance(e, HTTPException):
        return jsonify(error=e.description), e.code
    app.logger.exception("Unhandled error")
    return jsonify(error="Internal server error"), 500


# --------------------------------------------------------------------------------------
# Voice transcript parsing
# --------------------------------------------------------------------------------------
def to_ascii_digits(s):
    out = []
    for ch in s:
        if ch.isdigit() and not ch.isascii():
            try:
                out.append(str(unicodedata.digit(ch)))
                continue
            except ValueError:
                pass
        out.append(ch)
    return "".join(out)


def _regional_numbers(t):
    """Replace spoken number words (Hindi/Kannada/Tamil/Telugu/Marathi) by digits, whole words only."""
    parts = re.split(r"([\s,;.।]+)", t)
    return "".join(f" {REGIONAL_NUMS[p]} " if p in REGIONAL_NUMS else p for p in parts)


def normalize(text):
    """Transcript (any supported script) -> canonical English-ish token list."""
    t = to_ascii_digits(text).lower().replace("₹", " rs ")
    t = _regional_numbers(t)
    for word, canon in REGIONAL_LOOKUP:
        t = t.replace(word, f" {canon} ")
    t = re.sub(r"(\d+(?:\.\d+)?)", r" \1 ", t)
    raw = re.findall(r"[a-z]+|\d+(?:\.\d+)?|@|,|;", t)
    toks = []
    for w in raw:
        w = LATIN_UNIT_MAP.get(w, w)
        if w == "@":
            w = "at"
        if w in NUM_WORDS:
            v = NUM_WORDS[w]
            if toks and isinstance(toks[-1], float) and toks[-1] % 10 == 0 and toks[-1] >= 20 and v < 10 and toks[-1] < 100:
                toks[-1] += v
            else:
                toks.append(float(v))
            continue
        if re.fullmatch(r"\d+(?:\.\d+)?", w):
            toks.append(float(w))
        else:
            toks.append(w)
    return toks


def is_num(t):
    return isinstance(t, float)


def canon_key(word):
    w = (word or "").lower().strip()
    w = ALIASES.get(w, w)
    if w in CATALOG:
        return w
    if w.endswith("es") and w[:-2] in CATALOG:
        return w[:-2]
    if w.endswith("s") and w[:-1] in CATALOG:
        return w[:-1]
    return None


def take_name(toks, start, max_words=2):
    i = start
    while i < len(toks) and toks[i] in ("is", "named", "name", "called"):
        i += 1
    words = []
    while i < len(toks) and len(words) < max_words:
        t = toks[i]
        if is_num(t) or t in NAME_STOP or t in (",", ";") or canon_key(t):
            break
        words.append(t)
        i += 1
    return " ".join(words).title()


def parse_segment(seg):
    nums = [(i, t) for i, t in enumerate(seg) if is_num(t)]
    if not nums:
        return None
    qi, qty = nums[0]
    price = None
    for i, t in enumerate(seg):
        if t == "rs":
            if i + 1 < len(seg) and is_num(seg[i + 1]) and i + 1 != qi:
                price = seg[i + 1]
            elif i - 1 >= 0 and is_num(seg[i - 1]) and i - 1 != qi:
                price = seg[i - 1]
    if price is None:
        for i, t in enumerate(seg):
            if t in ("at", "for", "per") and i + 1 < len(seg) and is_num(seg[i + 1]) and i + 1 != qi:
                price = seg[i + 1]
    if price is None and len(nums) > 1:
        price = nums[1][1]
    unit = next((t for t in seg if t in UNITS), None)
    words = [t for t in seg if isinstance(t, str) and t.isalpha() and t not in STOP and t not in UNITS]
    key = next((canon_key(w) for w in words if canon_key(w)), None)
    name = key or " ".join(words).title()
    if not name:
        return None
    return dict(name=name.title() if not key else key.title(), item_key=key, qty=qty, unit=unit, rate=price)


def parse_items(toks):
    segs = [[]]
    for i, t in enumerate(toks):
        if t in (",", ";", "and", "then", "plus"):
            segs.append([])
            continue
        cur = segs[-1]
        if isinstance(t, str) and canon_key(t):            # item-first style: "tomato 3 kg rice 5 kg"
            fn = next((j for j, x in enumerate(cur) if is_num(x)), None)
            if fn is not None and any(isinstance(x, str) and canon_key(x) for x in cur[:fn]):
                segs.append(cur := [])
        if is_num(t) and any(is_num(x) for x in cur) and any(isinstance(x, str) and canon_key(x) for x in cur):
            nxt = [x for x in toks[i + 1:i + 3]]
            prev = cur[-1] if cur else None
            price_lead = prev in ("at", "for", "per") or (prev == "rs" and not (len(cur) > 1 and is_num(cur[-2])))   # "at 40" / "rs 40", but "40 rs" is a finished price
            if not price_lead and nxt[:1] != ["rs"] and any(x in UNITS or (isinstance(x, str) and canon_key(x)) for x in nxt):
                segs.append(cur := [])
        segs[-1].append(t)
    return [it for it in (parse_segment(s) for s in segs) if it]


def default_rate(key, bill_type):
    c = CATALOG.get(key)
    if not c:
        return None
    if bill_type == "farmer_invoice":
        return c["mandi"]
    if bill_type == "b2b_invoice":
        return c["wholesale"]
    return c["retail"]


def finalize_items(items, bill_type, interstate=False):
    """Authoritative amount/GST/benchmark calculation. Used for drafts AND confirmed bills."""
    out, subtotal, tax = [], 0.0, 0.0
    for it in items:
        key = it.get("item_key") or canon_key(str(it.get("name", "")).split(" ")[0]) or canon_key(it.get("name", ""))
        cat = CATALOG.get(key) if key else None
        qty, rate = num(it.get("qty")), num(it.get("rate"))
        unit = clean_str(it.get("unit"), 12) or (cat["unit"] if cat else "pcs")
        if bill_type == "b2b_invoice":
            gst = num(it.get("gst_rate"), default=(cat["gst"] if cat else 18), hi=40)
            if it.get("gst_rate") in (None, ""):
                gst = cat["gst"] if cat else 18
        else:
            gst = 0.0
        amount = r2(qty * rate)
        line = dict(name=clean_str(it.get("name"), 80), item_key=key, qty=qty, unit=unit, rate=rate, gst_rate=gst,
                    amount=amount, rate_source=it.get("rate_source", "edited"))
        if bill_type == "farmer_invoice" and cat and cat["mandi"] and (unit in UNIT_TO_KG or unit == cat["unit"]):
            bm = cat["mandi"] * UNIT_TO_KG.get(unit, 1)
            line["benchmark_rate"] = r2(bm)
            line["variance_pct"] = round((rate - bm) / bm * 100, 1) if bm else None
        subtotal += amount
        tax += amount * gst / 100
        out.append(line)
    totals = dict(subtotal=r2(subtotal), cgst=0.0, sgst=0.0, igst=0.0, tax=r2(tax), total=r2(subtotal + tax))
    if bill_type == "b2b_invoice":
        if interstate:
            totals["igst"] = r2(tax)
        else:
            totals["cgst"] = totals["sgst"] = r2(tax / 2)
    return out, totals


def parse_sale(bill_type, toks, warnings):
    items = parse_items(toks)
    for it in items:
        if it["unit"] is None:
            it["unit"] = CATALOG[it["item_key"]]["unit"] if it["item_key"] else "pcs"
        if it["rate"] is None:
            dr = default_rate(it["item_key"], bill_type)
            if dr is not None:
                it["rate"] = dr
                it["rate_source"] = "catalog-default"
                warnings.append(f"No price heard for {it['name']}: used reference rate ₹{dr}/{CATALOG[it['item_key']]['unit']}. Please verify.")
            else:
                it["rate"] = 0
                warnings.append(f"No price heard for {it['name']}. Please enter it.")
        else:
            it["rate_source"] = "spoken"
    if not items:
        warnings.append("Could not find any items in the transcript. Add them manually in the table below.")
    meta, party = {}, ""
    for i, t in enumerate(toks):
        if t in ("buyer", "customer", "party", "trader") and not party:
            party = take_name(toks, i + 1)
        if t in ("farmer", "seller") and "seller" not in meta:
            meta["seller"] = take_name(toks, i + 1)
    clean, totals = finalize_items(items, bill_type)
    return dict(party=party, meta=meta, items=clean, totals=totals)


def parse_waybill(raw, toks, warnings):
    up = to_ascii_digits(raw).upper()
    m = re.search(r"\b([A-Z]{2})[\s-]?(\d{1,2})[\s-]?([A-Z]{1,3})[\s-]?(\d{4})\b", up)
    vehicle = "".join(m.groups()) if m else ""
    driver = origin = dest = ""
    load_kg = fare = 0.0
    for i, t in enumerate(toks):
        if t == "driver" and not driver:
            driver = take_name(toks, i + 1)
        if t == "from" and not origin:
            origin = take_name(toks, i + 1)
        if t == "to" and not dest:
            dest = take_name(toks, i + 1)
        if is_num(t) and i + 1 < len(toks) and toks[i + 1] in UNIT_TO_KG and not load_kg:
            load_kg = t * UNIT_TO_KG[toks[i + 1]]
        if t in ("fare", "freight", "charge", "charges", "rent") and not fare:
            for j in range(i + 1, min(i + 4, len(toks))):
                if is_num(toks[j]):
                    fare = toks[j]
                    break
    if not fare:
        for i, t in enumerate(toks):
            if t == "rs":
                for j in (i + 1, i - 1):
                    if 0 <= j < len(toks) and is_num(toks[j]):
                        fare = toks[j]
                        break
                if fare:
                    break
    for label, val in (("driver name", driver), ("vehicle number", vehicle), ("load weight", load_kg), ("fare", fare)):
        if not val:
            warnings.append(f"Could not hear {label}. Please fill it in.")
    items = [dict(name="Transport fare", qty=1, unit="trip", rate=fare, rate_source="spoken" if fare else "edited")]
    clean, totals = finalize_items(items, "transport_waybill")
    meta = dict(driver_name=driver, vehicle_no=vehicle, origin=origin, destination=dest, load_kg=load_kg)
    return dict(party="", meta=meta, items=clean, totals=totals)


# --------------------------------------------------------------------------------------
# 1. Voice billing routes
# --------------------------------------------------------------------------------------
@app.post("/api/agent/parse-bill")
def parse_bill():
    p = request.get_json(silent=True) or {}
    bt = p.get("bill_type")
    if bt not in BILL_TYPES:
        return bad("bill_type must be one of " + ", ".join(BILL_TYPES))
    text = str(p.get("transcript", ""))[:2000].strip()
    if not text:
        return bad("Empty transcript")
    toks = normalize(text)
    warnings = []
    draft = parse_waybill(text, toks, warnings) if bt == "transport_waybill" else parse_sale(bt, toks, warnings)
    role = BILL_TYPES[bt]["role"]
    if bt == "b2b_invoice" and p.get("role") == "fmcg":
        role = "fmcg"
    draft.update(bill_type=bt, role=role, title=BILL_TYPES[bt]["title"], transcript=text,
                 language=clean_str(p.get("language"), 5) or "en", warnings=warnings)
    return jsonify(draft)


@app.post("/api/agent/confirm-bill")
def confirm_bill():
    """Called ONLY after the human has reviewed/edited the draft in the review modal."""
    p = request.get_json(silent=True) or {}
    bt = p.get("bill_type")
    if bt not in BILL_TYPES:
        return bad("Invalid bill_type")
    items_in = p.get("items")
    if not isinstance(items_in, list) or not 1 <= len(items_in) <= 100:
        return bad("Provide 1-100 items")
    for it in items_in:
        if not isinstance(it, dict) or not clean_str(it.get("name"), 80):
            return bad("Every item needs a name")
        if num(it.get("qty")) <= 0:
            return bad(f"Quantity must be greater than 0 for '{clean_str(it.get('name'), 40)}'")
        if num(it.get("rate"), default=-1) < 0:
            return bad(f"Invalid price for '{clean_str(it.get('name'), 40)}'")
    meta_in = p.get("meta") if isinstance(p.get("meta"), dict) else {}
    meta = {k: clean_str(meta_in.get(k)) for k in META_FIELDS[bt] if k not in ("interstate",)}
    interstate = bool(meta_in.get("interstate"))
    if "interstate" in META_FIELDS[bt]:
        meta["interstate"] = interstate
    gstin = meta.get("buyer_gstin", "")
    if gstin and not re.fullmatch(r"\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]", gstin.upper()):
        return bad("Buyer GSTIN format looks invalid")
    if gstin:
        meta["buyer_gstin"] = gstin.upper()
    if "load_kg" in meta:
        meta["load_kg"] = num(meta_in.get("load_kg"))
    items, totals = finalize_items(items_in, bt, interstate)
    role = BILL_TYPES[bt]["role"]
    if bt == "b2b_invoice" and p.get("role") == "fmcg":
        role = "fmcg"
    conn = db()
    cur = conn.execute(
        "INSERT INTO bills(bill_no,role,bill_type,party,subtotal,tax,total,meta,language,raw_transcript,created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("PENDING", role, bt, clean_str(p.get("party"), 80), totals["subtotal"], totals["tax"], totals["total"],
         json.dumps(meta, ensure_ascii=False), clean_str(p.get("language"), 5), clean_str(p.get("transcript"), 2000), now_iso()))
    bid = cur.lastrowid
    bill_no = f"{BILL_TYPES[bt]['prefix']}-{date.today():%y%m%d}-{bid:04d}"
    conn.execute("UPDATE bills SET bill_no=? WHERE id=?", (bill_no, bid))
    conn.executemany("INSERT INTO bill_items(bill_id,name,qty,unit,rate,gst_rate,amount) VALUES(?,?,?,?,?,?,?)",
                     [(bid, i["name"], i["qty"], i["unit"], i["rate"], i["gst_rate"], i["amount"]) for i in items])
    conn.commit()
    return jsonify(ok=True, bill=dict(id=bid, bill_no=bill_no, title=BILL_TYPES[bt]["title"], role=role, bill_type=bt,
                                      party=clean_str(p.get("party"), 80), meta=meta, items=items, totals=totals,
                                      created_at=now_iso()))


@app.get("/api/bills")
def list_bills():
    limit = int(num(request.args.get("limit"), 20, 1, 100))
    roles = ROLE_BILL_ROLES[g.role]                       # a login only ever sees its own role's bills
    q, args = "SELECT id,bill_no,role,bill_type,party,total,created_at FROM bills", []
    if roles:
        q += " WHERE role IN (%s)" % ",".join("?" * len(roles))
        args += roles
    return jsonify(bills=rows(q + " ORDER BY id DESC LIMIT ?", (*args, limit)))


# --------------------------------------------------------------------------------------
# 2. Logistics matcher
# --------------------------------------------------------------------------------------
@app.get("/api/logistics/cities")
def cities():
    return jsonify(cities=sorted(CITIES))


@app.get("/api/logistics/match")
def logistics_match():
    lane = request.args.get("lane", "")
    if lane not in ("farmer-wholesaler", "fmcg-wholesaler", "wholesaler-kirana"):
        return bad("lane must be farmer-wholesaler, fmcg-wholesaler or wholesaler-kirana")
    origin, dest = request.args.get("origin", ""), request.args.get("destination", "")
    weight = num(request.args.get("weight_kg"), 500, 1, 50000)
    goods = clean_str(request.args.get("goods"), 40) or "goods"
    if origin == dest and origin in CITIES:      # intra-city leg, e.g. wholesaler -> kirana in Bengaluru
        dist = num(request.args.get("distance_km"), 15, 1, 100)
    else:
        dist = road_km(origin, dest) or num(request.args.get("distance_km"), 0, 0, 3000)
    if not dist:
        return bad("Unknown city pair - pick cities from the list or pass distance_km")
    out = []
    for d in rows("SELECT * FROM drivers WHERE capacity_kg>=?", (weight,)):
        if lane not in d["lanes"].split(","):
            continue
        pickup = road_km(d["city"], origin) if road_km(d["city"], origin) is not None else 0
        fare = round(250 + dist * d["rate_per_km"] + weight * 0.15)
        msg = (f"IndiePulse load request\nGoods: {goods} ({int(weight)} kg)\nFrom: {origin}\nTo: {dest} (~{dist} km)\n"
               f"Offered fare: Rs {fare}\nVehicle needed: {d['vehicle_type']}. Please confirm availability.")
        out.append(dict(id=d["id"], name=d["name"], vehicle_type=d["vehicle_type"], plate=d["plate"], phone=d["phone"],
                        tel="tel:+91" + d["phone"], rating=d["rating"], languages=d["languages"].split(","),
                        capacity_kg=d["capacity_kg"], pickup_km=pickup, trip_km=dist, fare=fare,
                        driver_wage=round(fare * 0.7), whatsapp=wa_link(d["phone"], msg), score=fare + pickup * d["rate_per_km"] * 0.5))
    out.sort(key=lambda x: (x["score"], -x["rating"]))
    return jsonify(lane=lane, origin=origin, destination=dest, distance_km=dist, weight_kg=weight, matches=out,
                   note="Fare is an estimate (250 base + km rate + handling). Driver wage assumes ~70% after fuel/tolls. Demo drivers.")


@app.post("/api/logistics/dispatch")
def logistics_dispatch():
    p = request.get_json(silent=True) or {}
    d = one("SELECT id FROM drivers WHERE id=?", (int(num(p.get("driver_id"), 0)),))
    if not d:
        return bad("Unknown driver", 404)
    cur = db().execute("INSERT INTO dispatches(driver_id,lane,origin,destination,weight_kg,fare,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
                       (d["id"], clean_str(p.get("lane")), clean_str(p.get("origin")), clean_str(p.get("destination")),
                        num(p.get("weight_kg")), num(p.get("fare")), "requested", now_iso()))
    db().commit()
    return jsonify(ok=True, dispatch_id=cur.lastrowid)


# --------------------------------------------------------------------------------------
# 3. Wealth & ROI analytics
# --------------------------------------------------------------------------------------
def kpi(key, label, before, after, unit="inr", lower_better=False):
    imp = (before - after) if lower_better else (after - before)
    return dict(key=key, label=label, before=r2(before), after=r2(after), unit=unit, improvement=r2(imp),
                improvement_pct=round(imp / abs(before) * 100, 1) if before else None, lower_better=lower_better)


@app.get("/api/agent/wealth-analytics")
def wealth_analytics():
    allowed = ROLE_ANALYTICS[g.role]
    wanted = [r for r in (request.args.get("roles") or ",".join(allowed)).split(",") if r in allowed]
    if not wanted:
        return bad("No analytics available for this account", 403)
    d0 = date.today().replace(day=1)
    months = []
    for i in range(5, -1, -1):
        y, m = d0.year, d0.month - i
        while m <= 0:
            m, y = m + 12, y - 1
        months.append(date(y, m, 1).strftime("%b %Y"))
    out = {}
    for role in wanted:
        m = rows("SELECT * FROM role_metrics WHERE role=? ORDER BY idx", (role,))
        if not m:
            continue
        first, last = m[0], m[-1]
        main_label = {"farmer": "Monthly net earnings", "driver": "Monthly net earnings"}.get(role, "Monthly gross profit")
        k = [kpi("gross_profit", main_label, last["profit_before"], last["profit_after"])]
        if role != "driver":
            k.append(kpi("capital_unlocked", "Capital unlocked from dead stock", first["idle_capital"], last["idle_capital"], lower_better=True))
        if role in ("fmcg", "kirana", "farmer"):
            lbl = "FMCG expiry loss saved" if role in ("fmcg", "kirana") else "Spoilage loss saved"
            k.append(kpi("expiry_loss_saved", lbl, first["spoilage_value"], last["spoilage_value"], lower_better=True))
        for e in rows("SELECT * FROM role_kpis WHERE role=?", (role,)):
            k.append(kpi(e["key"], e["label"], e["before"], e["after"], e["unit"], bool(e["lower_better"])))
        out[role] = dict(label=ROLE_LABELS[role], kpis=k, series=dict(
            before=[x["profit_before"] for x in m], after=[x["profit_after"] for x in m],
            idle_capital=[x["idle_capital"] for x in m], spoilage=[x["spoilage_value"] for x in m]))
    return jsonify(months=months, roles=out, data_source="demo_seed",
                   note="Demo figures seeded in role_metrics/role_kpis. Replace with real before/after data per user.")


# --------------------------------------------------------------------------------------
# 4. FMCG inventory & expiry
# --------------------------------------------------------------------------------------
def severity(days_left):
    if days_left < 0:
        return "expired", 100
    if days_left <= 3:
        return "critical", 40
    if days_left <= 7:
        return "high", 30
    if days_left <= 15:
        return "medium", 20
    return "low", 10


def inv_rows(store=None):
    q = ("SELECT i.*, d.name AS distributor, d.phone AS distributor_phone FROM fmcg_inventory i "
         "LEFT JOIN distributors d ON d.id=i.distributor_id")
    return rows(q + (" WHERE i.store_name=?" if store else "") + " ORDER BY i.expiry_date", (store,) if store else ())


@app.get("/api/fmcg/inventory")
def fmcg_inventory():
    today = date.today()
    items = inv_rows(request.args.get("store"))
    for i in items:
        i["days_left"] = (date.fromisoformat(i["expiry_date"]) - today).days
    return jsonify(items=items)


@app.post("/api/fmcg/inventory")
def fmcg_add():
    p = request.get_json(silent=True) or {}
    try:
        exp = date.fromisoformat(str(p.get("expiry_date")))
    except ValueError:
        return bad("expiry_date must be YYYY-MM-DD")
    if not clean_str(p.get("name"), 80):
        return bad("name required")
    db().execute("INSERT INTO fmcg_inventory(owner_role,store_name,sku,name,brand,category,batch,qty,reorder_level,mrp,cost,expiry_date,distributor_id)"
                 " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (clean_str(p.get("owner_role"), 12) or "kirana", clean_str(p.get("store_name")) or "Ganesh Kirana",
                  clean_str(p.get("sku"), 20) or clean_str(p.get("name"), 12).upper(), clean_str(p.get("name"), 80),
                  clean_str(p.get("brand"), 40), clean_str(p.get("category"), 20), clean_str(p.get("batch"), 20),
                  int(num(p.get("qty"))), int(num(p.get("reorder_level"), 10)), num(p.get("mrp")), num(p.get("cost")),
                  exp.isoformat(), int(num(p.get("distributor_id"), 4))))
    db().commit()
    return jsonify(ok=True)


@app.get("/api/fmcg/expiry-alerts")
def expiry_alerts():
    days = int(num(request.args.get("days"), 30, 1, 365))
    today = date.today()
    alerts = []
    for i in inv_rows(request.args.get("store")):
        left = (date.fromisoformat(i["expiry_date"]) - today).days
        if left > days:
            continue
        sev, disc = severity(left)
        at_risk = round(i["qty"] * i["cost"], 2)
        alerts.append(dict(id=i["id"], sku=i["sku"], name=i["name"], brand=i["brand"], category=i["category"], batch=i["batch"],
                           qty=i["qty"], expiry_date=i["expiry_date"], days_left=left, severity=sev,
                           suggested_discount_pct=disc, sell_price=round(i["mrp"] * (1 - disc / 100), 2) if left >= 0 else 0,
                           value_at_risk=at_risk, distributor=i["distributor"]))
    alerts.sort(key=lambda a: a["days_left"])
    return jsonify(alerts=alerts, total_value_at_risk=round(sum(a["value_at_risk"] for a in alerts), 2))


@app.post("/api/fmcg/reorder-manifest")
def reorder_manifest():
    p = request.get_json(silent=True) or {}
    days = int(num(p.get("expiry_days"), 7, 1, 90))
    today = date.today()
    groups = {}
    for i in inv_rows(clean_str(p.get("store")) or None):
        left = (date.fromisoformat(i["expiry_date"]) - today).days
        lines = []
        if i["qty"] <= i["reorder_level"]:
            lines.append(f"REORDER {max(i['reorder_level'] * 2 - i['qty'], 1)} x {i['name']} ({i['brand']}) - stock {i['qty']}")
        if left <= days:
            lines.append(f"{'EXPIRED' if left < 0 else f'EXPIRING in {left}d'}: {i['qty']} x {i['name']} batch {i['batch']} - request replacement/credit note")
        if lines:
            g_ = groups.setdefault(i["distributor_id"], dict(name=i["distributor"], phone=i["distributor_phone"], lines=[]))
            g_["lines"] += lines
    store = clean_str(p.get("store")) or "Ganesh Kirana"
    out = []
    for did, gr in groups.items():
        text = f"Hello {gr['name']},\nRe-order manifest from {store} ({date.today():%d %b %Y}):\n" + "\n".join(f"- {l}" for l in gr["lines"]) + "\nPlease confirm availability & delivery date. Sent via IndiePulse."
        out.append(dict(distributor_id=did, distributor=gr["name"], phone=gr["phone"], lines=gr["lines"], text=text, whatsapp=wa_link(gr["phone"], text)))
    return jsonify(manifests=out)


# --------------------------------------------------------------------------------------
# 6. Translation matrix (also used by quick-commerce)
# --------------------------------------------------------------------------------------
ALL_NAME_TO_KEY = {}
for _k, _tr in ITEM_I18N.items():
    ALL_NAME_TO_KEY[_k] = _k
    for _v in _tr.values():
        ALL_NAME_TO_KEY[_v.lower()] = _k
ALL_NAME_TO_KEY.update({w: k for w, k in EXTRA_ITEM_WORDS.items()})


def item_name(key, lang):
    return key.title() if lang == "en" else ITEM_I18N.get(key, {}).get(lang, key.title())


def detect_lang(text):
    for ch in text:
        o = ord(ch)
        if 0x0900 <= o <= 0x097F:
            return "hi"
        if 0x0C80 <= o <= 0x0CFF:
            return "kn"
        if 0x0B80 <= o <= 0x0BFF:
            return "ta"
        if 0x0C00 <= o <= 0x0C7F:
            return "te"
    return "en"


def _norm_phrase(s):
    return re.sub(r"\s+", " ", re.sub(r"[.,!?।;:'\"“”‘’\-]", "", s.casefold())).strip()


def translate_text(text, src, tgt):
    src = detect_lang(text) if src in ("", "auto", None) else src
    if src == tgt:
        return dict(translation=text, translated=True, method="same-language", source=src, target=tgt)
    n = _norm_phrase(text)
    for key, tr in PHRASES.items():
        if any(_norm_phrase(v) == n for v in tr.values()):
            return dict(translation=tr[tgt], translated=True, method="phrasebook", key=key, source=src, target=tgt)
    gt = gnani_translate(text, src, tgt)
    if gt:
        return dict(translation=gt, translated=True, method="gnani", source=src, target=tgt)
    if LIBRETRANSLATE_URL:
        try:
            req = urllib.request.Request(LIBRETRANSLATE_URL + "/translate", method="POST",
                                         data=json.dumps(dict(q=text, source=src, target=tgt, format="text")).encode(),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=6) as resp:
                t = json.loads(resp.read().decode()).get("translatedText")
            if t:
                return dict(translation=t, translated=True, method="libretranslate", source=src, target=tgt)
        except Exception:  # fall through to glossary
            app.logger.warning("LibreTranslate call failed")
    out, found, done = text, [], set()
    for name in sorted(ALL_NAME_TO_KEY, key=len, reverse=True):
        if ALL_NAME_TO_KEY[name] in done:
            continue
        if name in out.lower() and re.search(re.escape(name), out, re.I):
            key = ALL_NAME_TO_KEY[name]
            done.add(key)
            found.append(dict(word=name, english=key))
            out = re.sub(re.escape(name), item_name(key, tgt), out, flags=re.I)
    return dict(translation=out, translated=False, method="glossary" if found else "none", glossary=found, source=src, target=tgt,
                note="Not in the offline phrasebook. Set LIBRETRANSLATE_URL (or plug in another MT API) for free-form text.")


@app.post("/api/translate")
def api_translate():
    p = request.get_json(silent=True) or {}
    text = str(p.get("text", ""))[:1000].strip()
    tgt = p.get("target", "en")
    if not text or tgt not in LANGS:
        return bad("text and a valid target language are required")
    return jsonify(translate_text(text, p.get("source", "auto"), tgt))


@app.get("/api/quick-replies")
def quick_replies():
    mine, peer = request.args.get("my", "en"), request.args.get("peer", "hi")
    if mine not in LANGS or peer not in LANGS:
        return bad("Unsupported language")
    return jsonify(replies=[dict(key=k, mine=v[mine], theirs=v[peer]) for k, v in PHRASES.items()], languages=LANGS)


# --------------------------------------------------------------------------------------
# 5. Quick-commerce / kirana bridge
# --------------------------------------------------------------------------------------
def require_key():
    k = request.headers.get("X-API-Key", "")
    if not hmac.compare_digest(k.encode(), QC_API_KEY.encode()):
        return bad("Invalid or missing X-API-Key", 401)
    return None


def qc_inventory_query(pincode="", sku=""):
    stores = rows("SELECT * FROM kirana_stores" + (" WHERE pincode=?" if pincode else ""), (pincode,) if pincode else ())
    for s in stores:
        stock = rows("SELECT sku,item_key,name,qty,price,updated_at FROM kirana_stock WHERE store_id=?" + (" AND sku=?" if sku else "") + " ORDER BY name",
                     (s["store_id"], sku) if sku else (s["store_id"],))
        for x in stock:
            x["status"] = "out" if x["qty"] == 0 else "low" if x["qty"] <= 5 else "ok"
        s["stock"] = stock
    return stores


def build_order_message(order, store, lang=None):
    lang = lang or store["lang"]
    items = ", ".join(f"{l['qty']} × {item_name(l['item_key'], lang)}" for l in order["lines"])
    return TEMPLATES["new_order"][lang].format(platform=order["platform"], ref=order["order_ref"], items=items, total=r2(order["total"]))


def order_view(r):
    store = one("SELECT * FROM kirana_stores WHERE store_id=?", (r["store_id"],)) or dict(lang="en", name="?")
    r["lines"] = json.loads(r["items"])
    del r["items"]
    r.update(store_name=store["name"], store_lang=store["lang"], message_local=build_order_message(r, store),
             message_en=build_order_message(r, store, "en"))
    return r


def create_qc_order(p):
    platform = clean_str(p.get("platform"), 20) or "Blinkit"
    ref = clean_str(p.get("order_ref"), 30) or f"QC{datetime.now():%H%M%S}"
    want = p.get("items")
    if not isinstance(want, list) or not want:
        return bad("items required")
    pincode = clean_str(p.get("pincode"), 10)
    candidates = [p.get("store_id")] if p.get("store_id") else [s["store_id"] for s in rows("SELECT store_id FROM kirana_stores" + (" WHERE pincode=?" if pincode else ""), (pincode,) if pincode else ())]
    best, shortages = None, []
    for sid in candidates:
        lines, ok, short = [], True, []
        for w in want:
            row = one("SELECT * FROM kirana_stock WHERE store_id=? AND sku=?", (sid, clean_str(w.get("sku"), 20)))
            q = int(num(w.get("qty"), 1, 1, 500))
            if not row or row["qty"] < q:
                ok = False
                short.append(dict(store_id=sid, sku=w.get("sku"), needed=q, available=row["qty"] if row else 0))
            else:
                lines.append(dict(sku=row["sku"], item_key=row["item_key"], name=row["name"], qty=q, price=row["price"]))
        if ok and (best is None or sum(l["qty"] for l in lines) >= 0):
            best = (sid, lines)
            break
        shortages += short
    if not best:
        return jsonify(error="No kirana hub in this area can fulfil the order", shortages=shortages), 409
    sid, lines = best
    total = r2(sum(l["qty"] * l["price"] for l in lines))
    cur = db().execute("INSERT INTO qc_orders(order_ref,platform,store_id,customer_area,items,total,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
                       (ref, platform, sid, clean_str(p.get("customer_area"), 60), json.dumps(lines, ensure_ascii=False), total, "pending", now_iso()))
    db().commit()
    return jsonify(ok=True, order=order_view(one("SELECT * FROM qc_orders WHERE id=?", (cur.lastrowid,)))), 201


@app.get("/api/qc/inventory")
def qc_inventory_api():
    if (e := require_key()):
        return e
    return jsonify(stores=qc_inventory_query(clean_str(request.args.get("pincode"), 10), clean_str(request.args.get("sku"), 20)))


@app.get("/api/qc/desk/inventory")
def qc_inventory_desk():
    return jsonify(stores=qc_inventory_query(clean_str(request.args.get("pincode"), 10), clean_str(request.args.get("sku"), 20)))


@app.post("/api/qc/stock-sync")
def qc_stock_sync():
    if (e := require_key()):
        return e
    p = request.get_json(silent=True) or {}
    if not one("SELECT 1 FROM kirana_stores WHERE store_id=?", (p.get("store_id"),)):
        return bad("Unknown store_id", 404)
    for it in p.get("items", [])[:500]:
        db().execute("UPDATE kirana_stock SET qty=?, price=COALESCE(?,price), updated_at=? WHERE store_id=? AND sku=?",
                     (int(num(it.get("qty"))), it.get("price"), now_iso(), p["store_id"], clean_str(it.get("sku"), 20)))
    db().commit()
    return jsonify(ok=True)


@app.post("/api/qc/stockout-check")
def qc_stockout_check():
    """Dark store reports shortages; we return kirana hubs that can fulfil them (micro-fulfilment)."""
    if request.headers.get("X-API-Key") and (e := require_key()):
        return e
    p = request.get_json(silent=True) or {}
    pincode = clean_str(p.get("pincode"), 10)
    res = []
    for s in p.get("shortages", [])[:50]:
        hubs = rows("SELECT k.store_id, st.name, st.phone, k.qty, k.price FROM kirana_stock k JOIN kirana_stores st USING(store_id) "
                    "WHERE k.sku=? AND k.qty>=? " + ("AND st.pincode=? " if pincode else "") + "ORDER BY k.qty DESC",
                    (clean_str(s.get("sku"), 20), int(num(s.get("qty_needed"), 1)), *([pincode] if pincode else [])))
        res.append(dict(sku=s.get("sku"), qty_needed=s.get("qty_needed"), hubs=hubs, fulfillable=bool(hubs)))
    return jsonify(results=res)


@app.post("/api/qc/orders")
def qc_orders_api():
    if (e := require_key()):
        return e
    return create_qc_order(request.get_json(silent=True) or {})


@app.post("/api/qc/desk/simulate-order")
def qc_simulate():
    p = request.get_json(silent=True) or {}
    platform = p.get("platform") if p.get("platform") in ("Blinkit", "Zepto") else "Blinkit"
    return create_qc_order(dict(platform=platform, pincode=p.get("pincode") or "560034", customer_area="Demo area",
                                items=[dict(sku="MILK500", qty=2), dict(sku="BREAD400", qty=1), dict(sku="CHIPS52", qty=3)]))


@app.get("/api/qc/desk/orders")
def qc_orders_list():
    status = request.args.get("status")
    r = rows("SELECT * FROM qc_orders" + (" WHERE status=?" if status else "") + " ORDER BY id DESC LIMIT 30", (status,) if status else ())
    return jsonify(orders=[order_view(x) for x in r])


def apply_decision(order_id, decision):
    o = one("SELECT * FROM qc_orders WHERE id=?", (order_id,))
    if not o:
        return bad("Order not found", 404)
    if o["status"] != "pending":
        return bad(f"Order already {o['status']}", 409)
    if decision == "accept":
        for l in json.loads(o["items"]):
            cur = db().execute("UPDATE kirana_stock SET qty=qty-?, updated_at=? WHERE store_id=? AND sku=? AND qty>=?",
                               (l["qty"], now_iso(), o["store_id"], l["sku"], l["qty"]))
            if cur.rowcount == 0:
                db().rollback()
                return bad(f"{l['name']} no longer in stock - reject this order", 409)
    db().execute("UPDATE qc_orders SET status=?, decided_at=? WHERE id=?", ("accepted" if decision == "accept" else "rejected", now_iso(), order_id))
    db().commit()
    return jsonify(ok=True, order=order_view(one("SELECT * FROM qc_orders WHERE id=?", (order_id,))))


@app.post("/api/qc/desk/orders/<int:order_id>/decision")
def qc_decision(order_id):
    d = (request.get_json(silent=True) or {}).get("decision")
    if d not in ("accept", "reject"):
        return bad("decision must be accept or reject")
    return apply_decision(order_id, d)


def detect_intent(text):
    t = to_ascii_digits(text).lower()
    latin = set(re.findall(r"[a-z]+", t))
    hit = lambda words: any((w in latin) if w.isascii() else (w in t) for w in words)
    if hit(REJECT_WORDS):
        return "reject"
    return "accept" if hit(ACCEPT_WORDS) else "unclear"


@app.post("/api/qc/desk/orders/<int:order_id>/voice-command")
def qc_voice(order_id):
    """Voice wrapper: transcript in shopkeeper's language -> intent + spoken feedback. Applies only if apply=true."""
    p = request.get_json(silent=True) or {}
    o = one("SELECT * FROM qc_orders WHERE id=?", (order_id,))
    if not o:
        return bad("Order not found", 404)
    lang = p.get("language") if p.get("language") in LANGS else (one("SELECT lang FROM kirana_stores WHERE store_id=?", (o["store_id"],)) or {}).get("lang", "en")
    intent = detect_intent(str(p.get("transcript", ""))[:300])
    fb = TEMPLATES["accepted" if intent == "accept" else "rejected" if intent == "reject" else "unclear"][lang]
    res = dict(intent=intent, feedback=fb, language=lang, applied=False)
    if p.get("apply") and intent in ("accept", "reject"):
        r = apply_decision(order_id, intent)
        if isinstance(r, tuple):
            return r
        res["applied"] = True
    return jsonify(res)


# --------------------------------------------------------------------------------------

# --------------------------------------------------------------------------------------
# Auth: mobile OTP sign-in (demo OTP 1234) + bearer-token session gate for /api/*
# --------------------------------------------------------------------------------------
OTP_TTL, OTP_MAX_FAILS, OTP_LOCK_S, OTP_RESEND_S, SESSION_TTL = 300, 5, 600, 20, 30 * 24 * 3600
DEMO_OTP = "1234"
PUBLIC_PATHS = {"/api/auth/send-otp", "/api/auth/verify-otp", "/api/logistics/cities"}

# One login per role. The same mobile number may hold several accounts (e.g. a farmer who also runs a kirana).
LOGIN_ROLES = ("farmer", "driver", "wholesaler", "kirana")
ROLE_TITLES = {"farmer": "Farmer", "driver": "Driver / Transporter", "wholesaler": "Wholesaler / FMCG", "kirana": "Kirana / Retailer"}
ROLE_BILL_TYPE = {"farmer": "farmer_invoice", "driver": "transport_waybill", "wholesaler": "b2b_invoice", "kirana": "kirana_bill"}
ROLE_BILL_ROLES = {"farmer": ["farmer"], "driver": ["transporter"], "wholesaler": ["wholesaler", "fmcg"], "kirana": ["kirana"]}
ROLE_ANALYTICS = {"farmer": ["farmer"], "driver": ["driver"], "wholesaler": ["wholesaler", "fmcg"], "kirana": ["kirana"]}
ROLE_LANES = {"farmer": {"farmer-wholesaler"}, "driver": set(), "wholesaler": {"fmcg-wholesaler", "wholesaler-kirana"},
              "kirana": {"wholesaler-kirana"}}


def role_denied():
    """Server-side authorisation: a farmer login can never call kirana/driver endpoints, whatever the UI shows."""
    p, role = request.path, g.role

    def no():
        return jsonify(error=f"Not available for {ROLE_TITLES[role]} accounts", forbidden=True), 403

    if (p.startswith("/api/fmcg/") or p.startswith("/api/qc/")) and role != "kirana":
        return no()
    if p.startswith("/api/driver/") and role != "driver":
        return no()
    if p == "/api/logistics/match" and request.args.get("lane") not in ROLE_LANES[role]:
        return no()
    if p == "/api/logistics/dispatch":
        if role not in ("farmer", "wholesaler", "kirana") or (request.get_json(silent=True) or {}).get("lane") not in ROLE_LANES[role]:
            return no()
    if p in ("/api/agent/parse-bill", "/api/agent/confirm-bill") and (request.get_json(silent=True) or {}).get("bill_type") != ROLE_BILL_TYPE[role]:
        return no()
    return None


def _h(s):
    return hashlib.sha256(s.encode()).hexdigest()


def send_sms(phone, otp):
    """Hook for a real SMS/OTP provider (MSG91, Twilio Verify, ...). Demo mode never calls it."""
    raise NotImplementedError("Wire an SMS provider here, then set OTP_DEMO=0")


def clean_phone(x):
    d = re.sub(r"\D", "", str(x or ""))
    d = d[-10:] if len(d) > 10 else d
    return d if re.fullmatch(r"[6-9]\d{9}", d) else ""


@app.before_request
def auth_gate():
    p = request.path
    if not p.startswith("/api/") or p in PUBLIC_PATHS or request.method == "OPTIONS":
        return None
    # Server-to-server quick-commerce bridge: the route handler validates X-API-Key itself.
    if p.startswith("/api/qc/") and not p.startswith("/api/qc/desk/") and request.headers.get("X-API-Key"):
        return None
    auth = request.headers.get("Authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    row = one("SELECT phone, role, expires_at FROM sessions WHERE token_hash=?", (_h(token),)) if token else None
    if not row or row["expires_at"] < int(datetime.now().timestamp()) or row["role"] not in LOGIN_ROLES:
        return jsonify(error="Please sign in", auth_required=True), 401
    g.phone, g.role, g.token_hash = row["phone"], row["role"], _h(token)
    return role_denied()


@app.post("/api/auth/send-otp")
def auth_send_otp():
    phone = clean_phone((request.get_json(silent=True) or {}).get("phone"))
    if not phone:
        return bad("Enter a valid 10-digit Indian mobile number")
    now = int(datetime.now().timestamp())
    row = one("SELECT * FROM otp_state WHERE phone=?", (phone,))
    if row and row["locked_until"] > now:
        return bad(f"Too many wrong attempts. Try again in {(row['locked_until'] - now) // 60 + 1} min", 429)
    if row and now - row["sent_at"] < OTP_RESEND_S:
        return bad(f"Please wait {OTP_RESEND_S - (now - row['sent_at'])}s before requesting another OTP", 429)
    demo = os.environ.get("OTP_DEMO", "1") == "1"
    otp = DEMO_OTP if demo else f"{secrets.randbelow(10000):04d}"
    if not demo:
        try:
            send_sms(phone, otp)
        except NotImplementedError as e:
            return bad(str(e), 501)
    # wrong-attempt counter deliberately survives re-sends (otherwise "resend" would reset the brute-force limit)
    db().execute("INSERT INTO otp_state(phone,otp_hash,expires_at,fails,locked_until,sent_at) VALUES(?,?,?,0,0,?) "
                 "ON CONFLICT(phone) DO UPDATE SET otp_hash=excluded.otp_hash, expires_at=excluded.expires_at, sent_at=excluded.sent_at, "
                 "fails=CASE WHEN otp_state.locked_until>0 AND otp_state.locked_until<=? THEN 0 ELSE otp_state.fails END, "
                 "locked_until=CASE WHEN otp_state.locked_until<=? THEN 0 ELSE otp_state.locked_until END",
                 (phone, _h(otp), now + OTP_TTL, now, now, now))
    db().commit()
    return jsonify(ok=True, demo=demo, hint="Demo mode: use OTP 1234" if demo else None, expires_in=OTP_TTL)


@app.post("/api/auth/verify-otp")
def auth_verify_otp():
    p = request.get_json(silent=True) or {}
    phone, otp, role = clean_phone(p.get("phone")), re.sub(r"\D", "", str(p.get("otp", ""))), p.get("role")
    if role not in LOGIN_ROLES:
        return bad("Choose how you use IndiePulse: farmer, driver, wholesaler or kirana")
    if not phone or len(otp) != 4:
        return bad("Enter your mobile number and the 4-digit OTP")
    now = int(datetime.now().timestamp())
    row = one("SELECT * FROM otp_state WHERE phone=?", (phone,))
    if not row or row["expires_at"] < now:
        return bad("OTP expired or not requested - tap Get OTP again", 400)
    if row["locked_until"] > now:
        return bad("Too many wrong attempts. Try again later", 429)
    if not hmac.compare_digest(row["otp_hash"], _h(otp)):
        fails = row["fails"] + 1
        locked = now + OTP_LOCK_S if fails >= OTP_MAX_FAILS else 0
        db().execute("UPDATE otp_state SET fails=?, locked_until=? WHERE phone=?", (fails, locked, phone))
        db().commit()
        left = max(OTP_MAX_FAILS - fails, 0)
        return bad("Incorrect OTP" + (f" ({left} tries left)" if left else " - locked for 10 minutes"), 401)
    token = secrets.token_urlsafe(32)
    conn = db()
    conn.execute("DELETE FROM otp_state WHERE phone=?", (phone,))
    conn.execute("INSERT INTO accounts(phone,role,created_at,last_login) VALUES(?,?,?,?) "
                 "ON CONFLICT(phone,role) DO UPDATE SET last_login=excluded.last_login", (phone, role, now_iso(), now_iso()))
    conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
    conn.execute("INSERT INTO sessions(token_hash,phone,created_at,expires_at,role) VALUES(?,?,?,?,?)", (_h(token), phone, now, now + SESSION_TTL, role))
    conn.commit()
    return jsonify(ok=True, token=token, phone=phone, role=role, role_title=ROLE_TITLES[role])


@app.get("/api/auth/me")
def auth_me():
    return jsonify(phone=g.phone, role=g.role, role_title=ROLE_TITLES[g.role])


@app.post("/api/auth/logout")
def auth_logout():
    db().execute("DELETE FROM sessions WHERE token_hash=?", (g.token_hash,))
    db().commit()
    return jsonify(ok=True)


# --------------------------------------------------------------------------------------
# Gnani.ai Vachana integration (https://docs.gnani.ai) - key stays on the server
#   STT (REST):  POST {base}/stt/v3   multipart: audio_file, model, language_code   header: X-API-Key-ID
#   Docs list WAV/MP3/OGG/FLAC/AAC/M4A, 8-44.1 kHz, <= 30 s for the v3 REST endpoint.
#   Realtime STT is a WebSocket (wss://api.vachana.ai/stt/v3/stream) - a good next step for streaming.
# --------------------------------------------------------------------------------------
GNANI_LANGS = {"en-IN", "hi-IN", "kn-IN", "ta-IN", "te-IN", "mr-IN", "indic-auto"}


# Python's default "Python-urllib/3.x" User-Agent is blocked by Gnani's firewall ("blocked access based on your browser's signature").
GNANI_UA = os.environ.get("GNANI_USER_AGENT", "IndiePulse/1.0 (+https://indiepulse.local) Mozilla/5.0")


def gnani_cfg():
    return dict(key=os.environ.get("GNANI_API_KEY", "").strip(),
                base=os.environ.get("GNANI_BASE_URL", "https://api.vachana.ai").rstrip("/"),
                model=os.environ.get("GNANI_STT_MODEL", "prisma-v3.0"),
                translate_url=os.environ.get("GNANI_TRANSLATE_URL", "").strip())


def _multipart(fields, files):
    boundary = "----IndiePulse" + secrets.token_hex(12)
    parts = []
    for k, v in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    for k, (fname, data, ctype) in files.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; filename="{fname}"\r\n'
                     f'Content-Type: {ctype}\r\n\r\n'.encode() + data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


@app.get("/api/gnani/status")
def gnani_status():
    c = gnani_cfg()
    return jsonify(stt_configured=bool(c["key"]), translate_configured=bool(c["translate_url"]),
                   base_url=c["base"], model=c["model"], languages=sorted(GNANI_LANGS))


@app.post("/api/gnani/stt")
def gnani_stt():
    """Browser uploads a short WAV; we forward it to Vachana STT and return the transcript."""
    c = gnani_cfg()
    if not c["key"]:
        return jsonify(error="Gnani STT is not configured on the server (set GNANI_API_KEY)", fallback=True), 503
    f = request.files.get("audio")
    if not f:
        return bad("Attach the recording as form field 'audio'")
    data = f.read()
    if len(data) < 1000:
        return bad("Recording was empty - please try again")
    lang = request.form.get("language_code", "indic-auto")
    if lang not in GNANI_LANGS:
        return bad("Unsupported language_code")
    body, ctype = _multipart({"model": c["model"], "language_code": lang},
                             {"audio_file": (f.filename or "speech.wav", data, f.mimetype or "audio/wav")})
    req = urllib.request.Request(c["base"] + "/stt/v3", data=body, method="POST",
                                 headers={"X-API-Key-ID": c["key"], "Content-Type": ctype, "Accept": "application/json", "User-Agent": GNANI_UA})
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            d = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        app.logger.warning("Gnani STT HTTP %s", e.code)           # never log the key or audio
        try:
            det = (json.loads(e.read().decode("utf-8", "replace")).get("detail") or {})
            up = f'{det.get("error_code", "")}: {det.get("message", "")}'.strip(": ") if isinstance(det, dict) else str(det)[:120]
        except Exception:
            up = ""
        app.logger.warning("Gnani STT HTTP %s %s", e.code, up)
        msg = ("Gnani rejected the API key" if e.code in (401, 403) else f"Gnani STT error (HTTP {e.code})") + (f" - {up}" if up else "")
        return jsonify(error=msg, upstream_status=e.code), 502
    except Exception as e:
        reason = getattr(e, "reason", e)                          # URLError wraps the real cause (DNS, TLS, proxy, timeout)
        kind = type(reason).__name__
        app.logger.warning("Gnani STT unreachable: %s: %s", kind, reason)
        hint = {"gaierror": "DNS lookup of the Gnani host failed", "timeout": "the request timed out",
                "TimeoutError": "the request timed out", "SSLCertVerificationError": "TLS certificate check failed (antivirus/proxy/old CA store?)",
                "SSLError": "TLS handshake failed", "ConnectionRefusedError": "connection refused (proxy/firewall?)"}.get(kind, kind)
        return jsonify(error=f"Could not reach Gnani STT from the server: {hint}. Run: python server.py --check-gnani"), 502
    if d.get("success") is False:
        return jsonify(error=str(d.get("message") or d.get("error") or "Gnani could not transcribe the audio")[:200]), 502
    return jsonify(transcript=str(d.get("transcript", "")).strip(), language=d.get("detected_language_code") or lang,
                   request_id=d.get("request_id"), engine="gnani-vachana")


def gnani_translate(text, src, tgt):
    """ADAPTER STUB: Gnani's public docs list STT/TTS but no text-translation endpoint, so this is inert
    until you set GNANI_TRANSLATE_URL. When you get the real request/response schema, adjust the payload
    and the response-field lookup below (and test it) - the rest of the app needs no change."""
    c = gnani_cfg()
    if not (c["key"] and c["translate_url"]):
        return None
    try:
        req = urllib.request.Request(c["translate_url"], method="POST",
                                     data=json.dumps(dict(text=text, source=src, target=tgt)).encode(),
                                     headers={"X-API-Key-ID": c["key"], "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            d = json.loads(resp.read().decode())
        return d.get("translation") or d.get("translated_text") or None
    except Exception:
        app.logger.warning("Gnani translate call failed")
        return None


# --------------------------------------------------------------------------------------
# Driver portal: live trip statistics from waybills saved through /api/agent/confirm-bill
# --------------------------------------------------------------------------------------
@app.get("/api/driver/trips")
def driver_trips():
    today = date.today()
    wk0 = today - timedelta(days=today.weekday())
    month0 = today.replace(day=1)
    prev_month0 = (month0 - timedelta(days=1)).replace(day=1)
    bills = rows("SELECT bill_no,total,meta,created_at FROM bills WHERE bill_type='transport_waybill' ORDER BY id DESC LIMIT 500")
    st = dict(trips_month=0, trips_prev_month=0, trips_week=0, trips_prev_week=0, fare_month=0.0, fare_prev_month=0.0, load_kg_month=0.0)
    recent = []
    for b in bills:
        d = date.fromisoformat(b["created_at"][:10])
        meta = json.loads(b["meta"] or "{}")
        if d >= month0:
            st["trips_month"] += 1
            st["fare_month"] += b["total"]
            st["load_kg_month"] += num(meta.get("load_kg"))
        elif d >= prev_month0:
            st["trips_prev_month"] += 1
            st["fare_prev_month"] += b["total"]
        if d >= wk0:
            st["trips_week"] += 1
        elif d >= wk0 - timedelta(days=7):
            st["trips_prev_week"] += 1
        if len(recent) < 6:
            recent.append(dict(bill_no=b["bill_no"], total=b["total"], origin=meta.get("origin", ""), destination=meta.get("destination", ""),
                               vehicle_no=meta.get("vehicle_no", ""), created_at=b["created_at"]))
    st["avg_fare"] = round(st["fare_month"] / st["trips_month"], 2) if st["trips_month"] else 0
    st["fare_month"], st["fare_prev_month"] = r2(st["fare_month"]), r2(st["fare_prev_month"])
    return jsonify(stats=st, recent=recent, source="waybills")


@app.get("/favicon.ico")
def favicon():
    return "", 204


@app.get("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


@app.get("/healthz")
def healthz():
    return jsonify(ok=True)


init_db()

def _lan_ip():
    import socket
    try:
        sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sk.connect(("10.255.255.255", 1))
        return sk.getsockname()[0]
    except Exception:
        return "127.0.0.1"


if __name__ == "__main__" and "--check-gnani" in __import__("sys").argv:
    import urllib.request, urllib.error
    c = gnani_cfg(); url = c["base"] + "/stt/v3"
    print("Key set:", bool(c["key"]), "| length:", len(c["key"]), "| URL:", url)
    print("Key source:", ".env file" if any(l.strip().startswith("GNANI_API_KEY") for l in open(os.path.join(BASE_DIR, ".env"), encoding="utf-8-sig")) else "Windows/system environment variable"
          if os.path.exists(os.path.join(BASE_DIR, ".env")) else "environment variable (no .env found)")
    try:
        urllib.request.urlopen(urllib.request.Request(url, method="POST", data=b"", headers={"X-API-Key-ID": c["key"] or "x", "User-Agent": GNANI_UA}), timeout=15)
    except urllib.error.HTTPError as e:
        print(f"Reached Gnani (HTTP {e.code}) - network OK. 401/403 would mean a bad key; 400/422 is expected for an empty request.")
    except Exception as e:
        r = getattr(e, "reason", e); print("CANNOT reach Gnani:", type(r).__name__, r)
    raise SystemExit

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    ssl_ctx, scheme = None, "http"
    cert, key = os.environ.get("SSL_CERT"), os.environ.get("SSL_KEY")
    if cert and key:
        ssl_ctx, scheme = (cert, key), "https"
    elif os.environ.get("HTTPS") == "1":
        try:
            import cryptography  # noqa: F401  (Werkzeug needs it to mint the self-signed cert)
            ssl_ctx, scheme = "adhoc", "https"
        except ImportError:
            raise SystemExit("HTTPS=1 needs one extra package:  pip install cryptography")
    ip = _lan_ip()
    print("\n  IndiePulse is starting")
    print(f"  This computer : {scheme}://localhost:{port}   <- microphone works here")
    if scheme == "https":
        print(f"  Phone / LAN   : https://{ip}:{port}   (accept the one-time 'not secure' warning for the self-signed certificate)")
    else:
        print(f"  Other devices : http://{ip}:{port}   <- page loads, but browsers BLOCK the microphone on plain http.")
        print("                  Restart with  HTTPS=1 python server.py  (Windows PowerShell:  $env:HTTPS=1; python server.py)\n")
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG") == "1", ssl_context=ssl_ctx)