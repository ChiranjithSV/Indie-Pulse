# IndiePulse
Voice-first, multilingual billing, logistics and retail platform (Flask + SQLite + vanilla JS).

## Run
    pip install flask
    copy .env.example .env      # then set GNANI_API_KEY=your key (never commit .env)
    python server.py            # open http://localhost:5000
Demo OTP: 1234 (OTP_DEMO=1). For phone/LAN use: `HTTPS=1 python server.py` (pip install cryptography) — browsers block the mic on plain http.
Check Gnani connectivity: `python server.py --check-gnani`. Tests: `python smoke_test.py`.

## Repo hygiene
Do NOT commit `.env` or `indiepulse.db`. See PITCH.md for the one-page solution pitch.
