"""IndiePulse backend smoke test (throw-away DB + local mock of the Gnani STT API).   Run:  python smoke_test.py"""
import io, json, os, sqlite3, tempfile, threading, wave
from http.server import BaseHTTPRequestHandler, HTTPServer

os.environ["INDIEPULSE_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ.pop("GNANI_API_KEY", None)
import server

FAILS = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name, extra if not cond else "")
    if not cond: FAILS.append(name)

SEEN = {}
class Mock(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        SEEN.update(path=self.path, key=self.headers.get("X-API-Key-ID"), ctype=self.headers.get("Content-Type", ""), body=body)
        if self.headers.get("X-API-Key-ID") != "test-key":
            self.send_response(401); self.end_headers(); self.wfile.write(b"{}"); return
        out = json.dumps({"success": True, "request_id": "r1", "transcript": "दो किलो प्याज", "detected_language_code": "hi-IN"}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(out)
    def log_message(self, *a): pass
srv = HTTPServer(("127.0.0.1", 0), Mock); threading.Thread(target=srv.serve_forever, daemon=True).start()

c = server.app.test_client()
J = lambda r: r.get_json()
def _db(sql, *a):
    d = sqlite3.connect(os.environ["INDIEPULSE_DB"]); d.execute(sql, a); d.commit(); d.close()

def login(phone, role):
    _db("DELETE FROM otp_state WHERE phone=?", phone)
    assert c.post("/api/auth/send-otp", json=dict(phone=phone)).status_code == 200
    r = c.post("/api/auth/verify-otp", json=dict(phone=phone, otp="1234", role=role))
    assert r.status_code == 200, r.get_data(as_text=True)
    return {"Authorization": "Bearer " + J(r)["token"]}

# ---------------- auth ----------------
check("api blocked without login", c.get("/api/bills").status_code == 401)
check("cities public", len(J(c.get("/api/logistics/cities"))["cities"]) >= 10)
check("bad phone rejected", c.post("/api/auth/send-otp", json=dict(phone="12345")).status_code == 400)
r = c.post("/api/auth/send-otp", json=dict(phone="9876543210")); check("send otp (demo hint 1234)", r.status_code == 200 and "1234" in J(r)["hint"])
check("verify without role rejected", c.post("/api/auth/verify-otp", json=dict(phone="9876543210", otp="1234")).status_code == 400)
check("verify with unknown role rejected", c.post("/api/auth/verify-otp", json=dict(phone="9876543210", otp="1234", role="admin")).status_code == 400)
check("wrong otp 401", c.post("/api/auth/verify-otp", json=dict(phone="9876543210", otp="0000", role="farmer")).status_code == 401)
HF = login("9876543210", "farmer")
me = J(c.get("/api/auth/me", headers=HF)); check("me returns role", me["role"] == "farmer" and me["phone"] == "9876543210", me)
HD = login("9876543210", "driver")          # same number, second role => separate account/session
check("same phone can hold a second role", J(c.get("/api/auth/me", headers=HD))["role"] == "driver" and J(c.get("/api/auth/me", headers=HF))["role"] == "farmer")
HW = login("9000011111", "wholesaler"); HK = login("9000022222", "kirana")
d = sqlite3.connect(os.environ["INDIEPULSE_DB"]); n = d.execute("select count(*) from accounts where phone='9876543210'").fetchone()[0]; d.close()
check("two accounts stored for one phone", n == 2, n)
c.post("/api/auth/send-otp", json=dict(phone="9123456780"))
check("resend cooldown", c.post("/api/auth/send-otp", json=dict(phone="9123456780")).status_code == 429)
for _ in range(5): r = c.post("/api/auth/verify-otp", json=dict(phone="9123456780", otp="9999", role="farmer"))
check("lockout after 5 wrong", r.status_code == 401 and "locked" in J(r)["error"].lower(), J(r))
check("correct otp refused while locked", c.post("/api/auth/verify-otp", json=dict(phone="9123456780", otp="1234", role="farmer")).status_code == 429)
_db("UPDATE otp_state SET sent_at=0 WHERE phone='9123456780'")
check("resend cannot reset lockout", c.post("/api/auth/send-otp", json=dict(phone="9123456780")).status_code == 429)
_db("UPDATE sessions SET role=NULL WHERE token_hash=(SELECT token_hash FROM sessions WHERE role='kirana' LIMIT 1)")
check("session without role (old version) forced to re-login", c.get("/api/bills", headers=HK).status_code == 401)
HK = login("9000022222", "kirana")

def get(H, u): return c.get(u, headers=H)
def post(H, u, body): return c.post(u, json=body, headers=H)

# ---------------- role isolation (server-side) ----------------
check("farmer blocked from FMCG desk", get(HF, "/api/fmcg/expiry-alerts").status_code == 403)
check("farmer blocked from QC desk", get(HF, "/api/qc/desk/orders").status_code == 403)
check("farmer blocked from driver stats", get(HF, "/api/driver/trips").status_code == 403)
check("driver blocked from FMCG + QC", get(HD, "/api/fmcg/expiry-alerts").status_code == 403 and get(HD, "/api/qc/desk/inventory").status_code == 403)
check("driver cannot search loads", get(HD, "/api/logistics/match?lane=farmer-wholesaler&origin=Kolar&destination=Bengaluru").status_code == 403)
check("kirana blocked from driver stats", get(HK, "/api/driver/trips").status_code == 403)
check("farmer cannot use wholesaler lane", get(HF, "/api/logistics/match?lane=fmcg-wholesaler&origin=Kolar&destination=Bengaluru").status_code == 403)
check("farmer cannot post a B2B invoice", post(HF, "/api/agent/parse-bill", dict(bill_type="b2b_invoice", transcript="2 kg rice at 50")).status_code == 403)
check("kirana cannot post a farmer invoice", post(HK, "/api/agent/confirm-bill", dict(bill_type="farmer_invoice", items=[dict(name="x", qty=1, rate=1)])).status_code == 403)
a = J(get(HF, "/api/agent/wealth-analytics?roles=farmer,kirana,wholesaler")); check("analytics limited to own role", set(a["roles"]) == {"farmer"}, list(a["roles"]))
check("wholesaler analytics = wholesaler + fmcg", set(J(get(HW, "/api/agent/wealth-analytics"))["roles"]) == {"wholesaler", "fmcg"})
check("driver analytics = driver only, no dead-stock KPI", {k["key"] for k in J(get(HD, "/api/agent/wealth-analytics"))["roles"]["driver"]["kpis"]} == {"gross_profit", "idle_days", "return_load_fill"})

# ---------------- billing (each role its own bill type) ----------------
d = J(post(HF, "/api/agent/parse-bill", dict(bill_type="farmer_invoice", transcript="Farmer Ramu sold to buyer Krishna, 2 quintal tomato at 2400 and 50 kg onion at 28")))
check("farmer parse", d["totals"]["total"] == 6200 and d["items"][0]["benchmark_rate"] == 2400, d["totals"])
check("farmer confirm", post(HF, "/api/agent/confirm-bill", dict(bill_type="farmer_invoice", party="Krishna", meta={}, items=d["items"])).status_code == 200)
d = J(post(HK, "/api/agent/parse-bill", dict(bill_type="kirana_bill", transcript="२ किलो प्याज और १ किलो चीनी, ३ पैकेट चिप्स")))
check("hindi kirana parse", len(d["items"]) == 3, d["items"])
d = J(post(HD, "/api/agent/parse-bill", dict(bill_type="transport_waybill", transcript="Driver Ramesh, vehicle KA 05 MN 2201, from Kolar to Bengaluru, load 1200 kg, fare 3500 rupees")))
check("waybill parse", d["meta"]["vehicle_no"] == "KA05MN2201" and d["totals"]["total"] == 3500 and d["meta"]["load_kg"] == 1200, d["meta"])
check("waybill confirm", post(HD, "/api/agent/confirm-bill", dict(bill_type="transport_waybill", meta=d["meta"], items=d["items"])).status_code == 200)
d = J(post(HW, "/api/agent/parse-bill", dict(bill_type="b2b_invoice", role="fmcg", transcript="Buyer Sri Traders, 10 packet biscuits at 8, 20 packet chips at 17 and 5 litre oil at 130")))
r = post(HW, "/api/agent/confirm-bill", dict(bill_type="b2b_invoice", role="fmcg", party="Sri Traders", meta=dict(buyer_gstin="29ABCDE1234F1Z5"), items=d["items"]))
check("b2b confirm (GST total)", r.status_code == 200 and J(r)["bill"]["totals"]["total"] == 1157.7)
check("zero qty rejected", post(HK, "/api/agent/confirm-bill", dict(bill_type="kirana_bill", items=[dict(name="x", qty=0, rate=1)])).status_code == 400)
check("bills are scoped per role", [b["bill_type"] for b in J(get(HF, "/api/bills"))["bills"]] == ["farmer_invoice"] and [b["bill_type"] for b in J(get(HD, "/api/bills"))["bills"]] == ["transport_waybill"] and len(J(get(HW, "/api/bills?role=farmer"))["bills"]) == 1)

# ---------------- logistics / driver ----------------
check("farmer lane match", len(J(get(HF, "/api/logistics/match?lane=farmer-wholesaler&origin=Kolar&destination=Bengaluru&weight_kg=1200"))["matches"]) >= 1)
r = get(HW, "/api/logistics/match?lane=wholesaler-kirana&origin=Bengaluru&destination=Bengaluru&weight_kg=500&distance_km=12")
check("same-city wholesaler->kirana", r.status_code == 200 and J(r)["distance_km"] == 12 and len(J(r)["matches"]) >= 1)
t = J(get(HD, "/api/driver/trips")); check("driver live trips", t["stats"]["trips_month"] == 1 and t["stats"]["fare_month"] == 3500 and t["recent"][0]["vehicle_no"] == "KA05MN2201", t)

# ---------------- FMCG / QC (kirana) ----------------
check("expiry alerts", len(J(get(HK, "/api/fmcg/expiry-alerts?days=30"))["alerts"]) > 3)
check("manifest", len(J(post(HK, "/api/fmcg/reorder-manifest", dict(expiry_days=7, store="Ganesh Kirana")))["manifests"]) >= 1)
check("qc bridge needs key", c.get("/api/qc/inventory").status_code == 401 and c.get("/api/qc/inventory", headers=HK).status_code == 401)
check("qc bridge with key (no login)", c.get("/api/qc/inventory?pincode=560034", headers={"X-API-Key": "demo-qc-key"}).status_code == 200)
r = post(HK, "/api/qc/desk/simulate-order", dict(platform="Zepto", pincode="560034")); check("qc order", r.status_code == 201); oid = J(r)["order"]["id"]
check("voice kn accept", J(post(HK, f"/api/qc/desk/orders/{oid}/voice-command", dict(transcript="ಸರಿ", language="kn")))["intent"] == "accept")
check("voice apply reject", J(post(HK, f"/api/qc/desk/orders/{oid}/voice-command", dict(transcript="नहीं", language="hi", apply=True)))["applied"])
check("double decision 409", post(HK, f"/api/qc/desk/orders/{oid}/decision", dict(decision="accept")).status_code == 409)
check("stockout hubs", J(post(HK, "/api/qc/stockout-check", dict(pincode="560034", shortages=[dict(sku="MILK500", qty_needed=30)])))["results"][0]["fulfillable"])

# ---------------- translate (every role) ----------------
check("phrasebook hi->kn", J(post(HD, "/api/translate", dict(text="आप कहाँ हैं?", source="auto", target="kn")))["translation"].startswith("ನೀವು"))
check("glossary has no duplicate entries", len(J(post(HD, "/api/translate", dict(text="bring 2 milk", source="en", target="kn")))["glossary"]) == 1)
check("quick replies for every role", all(get(h, "/api/quick-replies?my=en&peer=ta").status_code == 200 for h in (HF, HD, HW, HK)))

# ---------------- Gnani ----------------
check("gnani status: not configured", J(get(HD, "/api/gnani/status"))["stt_configured"] is False)
wav = io.BytesIO(); w = wave.open(wav, "wb"); w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes(b"\x01\x02" * 8000); w.close()
def stt(lang="hi-IN", H=HD): return c.post("/api/gnani/stt", headers=H, data={"audio": (io.BytesIO(wav.getvalue()), "speech.wav"), "language_code": lang}, content_type="multipart/form-data")
r = stt(); check("stt without key -> 503 + fallback flag", r.status_code == 503 and J(r)["fallback"] is True)
os.environ.update(GNANI_API_KEY="test-key", GNANI_BASE_URL=f"http://127.0.0.1:{srv.server_port}")
check("gnani status ok, key never leaked", J(get(HD, "/api/gnani/status"))["stt_configured"] and "test-key" not in get(HD, "/api/gnani/status").get_data(as_text=True))
r = stt(); d = J(r); check("stt proxy ok (any role)", r.status_code == 200 and d["transcript"] == "दो किलो प्याज" and stt(H=HF).status_code == 200, d)
check("upstream /stt/v3 + X-API-Key-ID", SEEN["path"] == "/stt/v3" and SEEN["key"] == "test-key")
check("multipart fields", all(x in SEEN["body"] for x in (b'name="audio_file"', b'name="model"', b'prisma-v3.0', b'name="language_code"')) and "boundary=" in SEEN["ctype"])
check("bad language rejected", stt("xx-XX").status_code == 400)
os.environ["GNANI_API_KEY"] = "wrong"
r = stt(); check("upstream 401 -> clean 502, key not echoed", r.status_code == 502 and "wrong" not in r.get_data(as_text=True))
check("stt requires login", c.post("/api/gnani/stt", data={}, content_type="multipart/form-data").status_code == 401)

check("logout", post(HF, "/api/auth/logout", {}).status_code == 200 and get(HF, "/api/bills").status_code == 401 and get(HD, "/api/bills").status_code == 200)
check("index served", c.get("/").status_code == 200)
print("\nALL PASSED" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
raise SystemExit(1 if FAILS else 0)