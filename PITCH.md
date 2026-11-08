# IndiePulse — voice-first commerce for Bharat

**Built for The Great Indian Internship Challenge · Powered by Gnani.ai Vachana speech-to-text**

## The problem
India's farmers, transporters, wholesalers and kirana owners run a ₹-lakh-crore supply chain on memory, paper slips and phone calls — mostly in their own language. English-only billing apps, typed forms and GST-style invoices are a wall for them. The result: unrecorded sales, price exploitation, empty return trucks, and expired stock at the corner shop.

## The solution
**IndiePulse lets people run their business by talking, in Kannada, Hindi, Tamil, Telugu, Marathi or English.** Say *"ಎರಡು ಕೆಜಿ ಈರುಳ್ಳಿ, ಒಂದು ಕೆಜಿ ಸಕ್ಕರೆ"* and get an itemised, correctly priced bill.

One platform, four separate role-based logins (mobile OTP):

| Role | What they get |
|---|---|
| **Farmer** | Voice invoice with mandi-price benchmark, truck booking to wholesalers, savings analytics |
| **Driver / Transporter** | Voice waybill, trip log, earnings, translated delivery phrases and a giant-text driver card |
| **Wholesaler / FMCG** | GST-aware B2B invoice, logistics to kirana, near-expiry stock + WhatsApp re-order |
| **Kirana / Retailer** | Voice bill, expiry alerts, Blinkit/Zepto-style quick-commerce bridge with voice confirmation |

## How Gnani is used
Speech goes browser → our Flask server → **Gnani Vachana STT (`/stt/v3`)** with the key kept server-side (never in the browser). Languages: en-IN, hi-IN, kn-IN, ta-IN, te-IN, mr-IN. If Gnani is unreachable, the app falls back to browser speech so the user is never stuck.

## What makes it trustworthy
- **Human-in-the-loop:** every voice draft opens in an editable review window; nothing is saved until the user confirms.
- **Server-side role isolation:** a farmer login is refused (403) on kirana/driver APIs — not just hidden in the UI.
- **Honest data:** demo data is labelled; OTP is a demo code in this build.

## Impact (design goals)
Digital records for first-time sellers · fair-price visibility for farmers · fewer empty return trips · less stock wasted to expiry.

## Tech
Python/Flask · SQLite · vanilla HTML/JS · Chart.js · Gnani Vachana API. Runs with one command: `python server.py`.

## Next steps
Real SMS OTP, live mandi-price feeds, UPI payment links, Gnani translation/TTS for the driver card, offline-first mode.
