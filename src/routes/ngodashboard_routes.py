import os
import os
import traceback
from flask import Blueprint, request, jsonify
from datetime import datetime, timedelta, date
from supabase import create_client

ngo_dashboard_bp = Blueprint("ngo_dashboard", __name__)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

supabase = create_client(
    SUPABASE_URL,
    SUPABASE_KEY
)

# =====================================================
# Helpers
# =====================================================
def log(title, data=None):
    print(f"\n🟩 {title}")
    if data is not None:
        print("   →", data)


def safe_parse_date(value):
    """Safely parse YYYY-MM-DD into date or return None"""
    try:
        return datetime.fromisoformat(value).date()
    except Exception:
        return None


def map_donation(row, donor=None):
    return {
        "id": row.get("id"),
        "title": row.get("title"),
        "category": row.get("category"),
        "quantity": row.get("quantity"),
        "unit": row.get("unit"),
        "expiry_date": row.get("expiry_date"),
        "pickup_address": row.get("pickup_address"),
        "status": (row.get("status") or "").lower(),
        "final_state": row.get("final_state"),
        "urgency": row.get("urgency"),
        "qr_code": row.get("qr_code"),
        "claimed_date": row.get("claimed_date"),
        "donor_phone": donor.get("phone") if donor else None,
        "donor_name": donor.get("full_name") if donor else None,
    }




# =====================================================
# GET NGO DASHBOARD
# =====================================================
@ngo_dashboard_bp.route("/ngo-dashboard/", methods=["GET"])
def ngo_dashboard():
    try:
        log("NGO DASHBOARD REQUEST", request.args)

        ngo_id = request.args.get("ngoId")
        if not ngo_id:
            return jsonify({"error": "Missing ngoId"}), 400

        today = date.today()
        urgent_limit = today + timedelta(days=2)

        # -------------------------------------------------
        # 1️⃣ Fetch ALL donations
        # -------------------------------------------------
        donations_res = (
            supabase.table("food_donations")
            .select("*")
            .execute()
        )
        rows = donations_res.data or []

        log("Total donations fetched", len(rows))

        if not rows:
            return jsonify({
                "available": [],
                "urgent": [],
                "claimed": [],
                "completed": [],
            }), 200

        # -------------------------------------------------
        # 2️⃣ Fetch ALL donors in ONE query (FIXED)
        # -------------------------------------------------
        donor_ids = list({
            row["donor_id"]
            for row in rows
            if row.get("donor_id")
        })

        donors_map = {}

        if donor_ids:
            donors_res = (
                supabase.table("users")
                .select("id, full_name, phone")
                .in_("id", donor_ids)
                .execute()
            )

            donors_map = {
                d["id"]: d for d in (donors_res.data or [])
            }

        # -------------------------------------------------
        # 3️⃣ Fetch NGO claims (SOURCE OF TRUTH)
        # -------------------------------------------------
        claims_res = (
            supabase.table("ngo_claims")
            .select("donation_id, status, claimed_at")
            .eq("ngo_id", ngo_id)
            .execute()
        )

        ngo_claims = {
            c["donation_id"]: c
            for c in (claims_res.data or [])
        }

        available = []
        urgent = []
        claimed = []
        completed = []

        # -------------------------------------------------
        # 4️⃣ Process donations
        # -------------------------------------------------
        for row in rows:
            donation_id = row.get("id")
            status = (row.get("status") or "").lower()
            final_state = row.get("final_state")
            donor_id = row.get("donor_id")

            expiry = safe_parse_date(row.get("expiry_date"))
            claim = ngo_claims.get(donation_id)

            donor = donors_map.get(donor_id)

            donation = map_donation(row, donor)

            # =================================================
            # CLAIMED / COMPLETED (THIS NGO ONLY)
            # =================================================
            if claim:
                if claim["status"] == "claimed":
                    donation["claimed_date"] = claim.get("claimed_at")
                    claimed.append(donation)
                    continue

                if claim["status"] == "completed":
                    completed.append(donation)
                    continue

            # =================================================
            # AVAILABLE (VISIBILITY RULES)
            # =================================================
            if status != "available":
                continue

            # ❌ Donor cancelled → NEVER visible
            if final_state == "cancelled_by_donor":
                continue

            # ✅ NGO cancelled → visible again
            if final_state == "cancelled_by_ngo":
                available.append(donation)
                continue

            # ⏰ System-expired donation
            if final_state == "expired":
                if expiry and expiry >= today:
                    available.append(donation)
                continue

            # ✅ Normal available donation
            if final_state is None:
                available.append(donation)

                # 🚨 Urgent window
                if expiry and today <= expiry <= urgent_limit:
                    urgent.append(donation)

        result = {
            "available": available,
            "urgent": urgent,
            "claimed": claimed,
            "completed": completed,
        }

        log("NGO DASHBOARD RESULT", {
            "available": len(available),
            "urgent": len(urgent),
            "claimed": len(claimed),
            "completed": len(completed),
        })

        return jsonify(result), 200

    except Exception as e:
        print("\n🔥 NGO DASHBOARD CRASH 🔥")
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500



# =====================================================
# CLAIM DONATION
# =====================================================
@ngo_dashboard_bp.route("/donations/<donation_id>/claim", methods=["PUT"])
def claim_donation(donation_id):
    try:
        log("CLAIM DONATION", donation_id)

        data = request.get_json() or {}
        ngo_id = data.get("ngo_id")

        if not ngo_id:
            return jsonify({"error": "Missing ngo_id"}), 400

        # -------------------------------------------------
        # 🔍 Fetch donation (GUARDS)
        # -------------------------------------------------
        res = (
            supabase.table("food_donations")
            .select("id, status, final_state, expiry_date")
            .eq("id", donation_id)
            .single()
            .execute()
        )

        if not res.data:
            return jsonify({"error": "Donation not found"}), 404

        donation = res.data
        status = (donation.get("status") or "").lower()
        final_state = donation.get("final_state")
        expiry = safe_parse_date(donation.get("expiry_date"))

        # ❌ Block finalized donations
        if final_state is not None:
            return jsonify({"error": "Donation is no longer available"}), 409

        # ❌ Must be available
        if status != "available":
            return jsonify({
                "error": "Donation cannot be claimed",
                "current_status": status
            }), 409

        # ❌ Expired by date
        if expiry and expiry < date.today():
            return jsonify({"error": "Donation has expired"}), 409

        now = datetime.utcnow().isoformat()

        # -------------------------------------------------
        # 1️⃣ Create NGO claim (SOURCE OF TRUTH)
        # -------------------------------------------------
        payload = {
            "donation_id": donation_id,
            "ngo_id": ngo_id,
            "status": "claimed",     # ✅ matches DB constraint
            "claimed_at": now,
        }

        log("🧾 NGO CLAIM INSERT PAYLOAD", payload)

        supabase.table("ngo_claims").insert(payload).execute()

        # -------------------------------------------------
        # 2️⃣ Update donation snapshot (NO claimed_date)
        # -------------------------------------------------
        supabase.table("food_donations").update({
            "status": "claimed",
            "updated_at": now,
        }).eq("id", donation_id).execute()

        log("DONATION CLAIMED SUCCESS", donation_id)

        return jsonify({
            "message": "Donation claimed successfully",
            "donation_id": donation_id,
        }), 200

    except Exception as e:
        print("\n🔥 CLAIM DONATION ERROR 🔥")
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500




@ngo_dashboard_bp.route("/ngo-dashboard/stats", methods=["GET"])
def ngo_dashboard_stats():
    try:
        ngo_id = request.args.get("ngoId")
        if not ngo_id:
            return jsonify({"error": "Missing ngoId"}), 400

        today = date.today()
        urgent_limit = today + timedelta(days=2)

        # -------------------------------------------------
        # 1️⃣ BASE AVAILABLE — ACTIVE DONATIONS ONLY
        # (this is WHY you used .is_(None))
        # -------------------------------------------------
        base_available = (
            supabase.table("food_donations")
            .select("id, expiry_date, final_state")
            .eq("status", "available")
            .is_("final_state", None)
            .execute()
            .data
            or []
        )

        available = []
        urgent = []

        # -------------------------------------------------
        # 2️⃣ APPLY DOMAIN RULES ON TOP
        # -------------------------------------------------
        for d in base_available:
            expiry = safe_parse_date(d.get("expiry_date"))

            # Normal available
            available.append(d)

            # Urgent window
            if expiry and today <= expiry <= urgent_limit:
                urgent.append(d)

        # -------------------------------------------------
        # 3️⃣ NGO CLAIM COUNTS (SOURCE OF TRUTH)
        # -------------------------------------------------
        claims_res = (
            supabase.table("ngo_claims")
            .select("status")
            .eq("ngo_id", ngo_id)
            .execute()
        )

        claims = claims_res.data or []

        claimed = 0
        completed = 0
        cancelled = 0

        for c in claims:
            if c["status"] == "claimed":
                claimed += 1
            elif c["status"] == "completed":
                completed += 1
            elif c["status"] == "cancelled":
                cancelled += 1

        return jsonify({
            "available": len(available),
            "urgent": len(urgent),
            "claimed": claimed,
            "completed": completed,
            "cancelled": cancelled,
        }), 200

    except Exception as e:
        print("\n🔥 NGO DASHBOARD STATS ERROR 🔥")
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@ngo_dashboard_bp.route("/ngo/claims/history", methods=["GET"])
def ngo_claims_history():
    try:
        ngo_id = request.args.get("ngoId")
        if not ngo_id:
            return jsonify({"error": "Missing ngoId"}), 400

        # -------------------------------------------------
        # 1️⃣ Fetch NGO claims (HISTORY = SOURCE OF TRUTH)
        # -------------------------------------------------
        claims_res = (
            supabase.table("ngo_claims")
            .select(
                "id, donation_id, status, claimed_at, completed_at, cancelled_at"
            )
            .eq("ngo_id", ngo_id)
            .order("claimed_at", desc=True)
            .execute()
        )

        claims = claims_res.data or []
        if not claims:
            return jsonify({"data": []}), 200

        # -------------------------------------------------
        # 2️⃣ Fetch related donations IN BULK (SAFE)
        # -------------------------------------------------
        donation_ids = list({
            c["donation_id"]
            for c in claims
            if c.get("donation_id")
        })

        donation_map = {}

        if donation_ids:
            donations_res = (
                supabase.table("food_donations")
                .select("id, title, category, quantity, unit, pickup_address")
                .in_("id", donation_ids)
                .execute()
            )

            donation_map = {
                d["id"]: d for d in (donations_res.data or [])
            }

        # -------------------------------------------------
        # 3️⃣ Merge results (HISTORY IS NEVER FILTERED)
        # -------------------------------------------------
        history = []

        for c in claims:
            donation = donation_map.get(c["donation_id"])

            history.append({
                "id": c["id"],
                "donation_id": c["donation_id"],

                # Donation snapshot (may be missing)
                "title": donation.get("title") if isinstance(donation, dict) else "Deleted donation",
                "category": donation.get("category") if isinstance(donation, dict) else None,
                "quantity": donation.get("quantity") if isinstance(donation, dict) else None,
                "unit": donation.get("unit") if isinstance(donation, dict) else None,
                "pickup_address": donation.get("pickup_address") if isinstance(donation, dict) else None,

                # Claim lifecycle
                "status": c.get("status"),
                "claimed_at": c.get("claimed_at"),
                "completed_at": c.get("completed_at"),
                "cancelled_at": c.get("cancelled_at"),
            })

        return jsonify({"data": history}), 200

    except Exception as e:
        print("\n🔥 NGO CLAIMS HISTORY ERROR 🔥")
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


# =====================================================
# NGO IMPACT ANALYTICS
# =====================================================




@ngo_dashboard_bp.route("/ngo/impact", methods=["GET"])
def ngo_impact():
    try:
        print("🚀 ENTERED NGO IMPACT ROUTE")

        ngo_id = request.args.get("ngoId")
        if not ngo_id:
            return jsonify({"error": "Missing ngoId"}), 400

        # =================================================
        # 🔁 UNIT → KG CONVERSION (BACKEND SOURCE OF TRUTH)
        # =================================================
        PIECE_TO_KG = {
            "Fruits": 0.18,
            "Vegetables": 0.25,
            "Meat": 0.30,
            "Dairy": 0.50,
            "Grains": 0.40,
            "Prepared Food": 0.40,
        }

        LITER_TO_KG = {
            "Dairy": 1.03,
            "Prepared Food": 1.00,
        }

        BOX_TO_KG = {
            "Fruits": 5.0,
            "Vegetables": 6.0,
            "Meat": 10.0,
            "Dairy": 8.0,
            "Prepared Food": 7.0,
        }

        def convert_to_kg(qty, unit, category):
            try:
                qty = float(qty)
            except (TypeError, ValueError):
                return 0.0

            unit = (unit or "").lower()
            category = category or "Other"

            if unit == "kg":
                return qty

            if unit == "pieces":
                return qty * PIECE_TO_KG.get(category, 0.25)

            if unit == "liters":
                return qty * LITER_TO_KG.get(category, 1.0)

            if unit == "boxes":
                return qty * BOX_TO_KG.get(category, 5.0)

            return 0.0

        # =================================================
        # 1️⃣ FETCH NGO CLAIM HISTORY (SOURCE OF TRUTH)
        # =================================================
        claims_res = (
            supabase.table("ngo_claims")
            .select("donation_id, status, claimed_at, completed_at")
            .eq("ngo_id", ngo_id)
            .execute()
        )

        claims = claims_res.data or []

        if not claims:
            return jsonify({
                "metrics": {
                    "food_saved": 0,
                    "waste_prevented": 0,
                    "co2_avoided": 0,
                },
                "success_rate": {
                    "value": 0,
                    "completed": 0,
                    "total": 0,
                },
                "monthly": [],
                "categories": [],
            }), 200

        # =================================================
        # 2️⃣ COLLECT DONATION IDS
        # =================================================
        donation_ids = list({
            c["donation_id"]
            for c in claims
            if c.get("donation_id")
        })

        if not donation_ids:
            return jsonify({
                "metrics": {
                    "food_saved": 0,
                    "waste_prevented": 0,
                    "co2_avoided": 0,
                },
                "success_rate": {
                    "value": 0,
                    "completed": 0,
                    "total": 0,
                },
                "monthly": [],
                "categories": [],
            }), 200

        # =================================================
        # 3️⃣ FETCH DONATIONS
        # =================================================
        donations_res = (
            supabase.table("food_donations")
            .select("id, quantity, unit, category")
            .in_("id", donation_ids)
            .execute()
        )

        donation_map = {
            d["id"]: d
            for d in (donations_res.data or [])
            if d.get("id")
        }

        # =================================================
        # 4️⃣ AGGREGATION
        # =================================================
        category_totals_kg = {}
        monthly_map = {}
        food_saved_kg = 0.0

        total_claims = 0
        completed_claims = 0

        for c in claims:
            donation = donation_map.get(c["donation_id"])
            if not donation:
                continue

            status = c.get("status")

            # -----------------------------
            # 📊 COUNT CLAIMS (CLAIMED + COMPLETED)
            # -----------------------------
            if status in ("claimed", "completed"):
                total_claims += 1

            # -----------------------------
            # 📅 MONTHLY CLAIMS
            # -----------------------------
            if status == "claimed":
                claimed_at = c.get("claimed_at")
                if isinstance(claimed_at, str):
                    month = claimed_at[:7]
                    monthly_map.setdefault(
                        month,
                        {
                            "month": month,
                            "claims": 0,
                            "completed": 0,
                            "food_saved_kg": 0,
                        },
                    )
                    monthly_map[month]["claims"] += 1

            # -----------------------------
            # 🌱 IMPACT ONLY FROM COMPLETED
            # -----------------------------
            if status != "completed":
                continue

            completed_claims += 1

            kg = convert_to_kg(
                donation.get("quantity"),
                donation.get("unit"),
                donation.get("category"),
            )

            if kg <= 0:
                continue

            food_saved_kg += kg

            category = donation.get("category") or "Other"
            category_totals_kg[category] = (
                category_totals_kg.get(category, 0) + kg
            )

            completed_at = c.get("completed_at")
            if isinstance(completed_at, str):
                month = completed_at[:7]
                monthly_map.setdefault(
                    month,
                    {
                        "month": month,
                        "claims": 0,
                        "completed": 0,
                        "food_saved_kg": 0,
                    },
                )
                monthly_map[month]["completed"] += 1
                monthly_map[month]["food_saved_kg"] += kg

        # =================================================
        # 5️⃣ DERIVED METRICS
        # =================================================
        success_rate = (
            round((completed_claims / total_claims) * 100, 1)
            if total_claims > 0
            else 0
        )

        monthly_list = sorted(monthly_map.values(), key=lambda x: x["month"])

        total_kg = food_saved_kg or 1
        category_colors = {
            "Vegetables": "hsl(142, 76%, 36%)",
            "Fruits": "hsl(25, 95%, 53%)",
            "Dairy": "hsl(48, 96%, 53%)",
            "Grains": "hsl(32, 95%, 44%)",
            "Prepared Food": "hsl(0, 84%, 60%)",
        }

        categories = [
            {
                "name": name,
                "value": round((kg / total_kg) * 100, 1),
                "color": category_colors.get(name, "hsl(215, 16%, 47%)"),
            }
            for name, kg in category_totals_kg.items()
        ]

        food_saved = round(food_saved_kg, 1)

        # =================================================
        # ✅ FINAL RESPONSE
        # =================================================
        return jsonify({
            "metrics": {
                "food_saved": food_saved,
                "waste_prevented": food_saved,
                "co2_avoided": round(food_saved * 2.5, 1),
            },
            "success_rate": {
                "value": success_rate,
                "completed": completed_claims,
                "total": total_claims,
            },
            "monthly": monthly_list,
            "categories": categories,
        }), 200

    except Exception:
        import traceback
        print("\n🔥 NGO IMPACT HARD CRASH 🔥")
        traceback.print_exc()
        return jsonify({"error": "Impact analytics failed"}), 500
