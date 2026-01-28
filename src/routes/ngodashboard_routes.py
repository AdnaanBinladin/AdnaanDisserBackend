import os
from flask import Blueprint, request, jsonify
from datetime import datetime, timedelta
from supabase import create_client

ngo_dashboard_bp = Blueprint("ngo_dashboard", __name__)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


def log(title, data=None):
    print(f"\n🟩 [{title}]")
    if data is not None:
        print("   →", data)


def map_donation(row, donor=None):
    return {
        "id": row.get("id"),
        "title": row.get("title"),
        "category": row.get("category"),
        "quantity": row.get("quantity"),
        "unit": row.get("unit"),
        "expiry_date": row.get("expiry_date"),
        "pickup_address": row.get("pickup_address"),
        "status": row.get("status"),
        "urgency": row.get("urgency"),
        "qr_code": row.get("qr_code"),
        "claimed_date": row.get("claimed_date"),
        "donor_phone": donor.get("phone") if donor else None,
        "donor_name": donor.get("full_name") if donor else None,
    }


@ngo_dashboard_bp.route("/api/ngo-dashboard/", methods=["GET"])
def ngo_dashboard():

    log("NGO-DASHBOARD REQUEST", request.args)

    ngo_id = request.args.get("ngoId")
    if not ngo_id:
        return jsonify({"error": "Missing ngoId"}), 400

    response = supabase.table("food_donations").select("*").execute()
    rows = response.data or []

    log("Rows retrieved", len(rows))

    today = datetime.today().date()
    urgent_limit = today + timedelta(days=2)

    available = []
    urgent = []
    expired = []
    claimed = []
    cancelled = []

    for idx, row in enumerate(rows):
        log(f"Processing row #{idx + 1}", row)

        # 1️⃣ Extract core fields FIRST
        status = (row.get("status") or "").lower()
        claimed_by = row.get("claimed_by")
        donor_id = row.get("donor_id")
        expiry_raw = row.get("expiry_date")

        donor = None

        if status == "claimed":
            donor_id = row.get("donor_id")
        if donor_id:
            donor_response = supabase.table("users") \
                .select("full_name, phone") \
                .eq("id", donor_id) \
                .single() \
                .execute()

            donor = donor_response.data
        donation = map_donation(row, donor)
        status = (row.get("status") or "").lower()
        claimed_by = row.get("claimed_by")
        expiry_raw = row.get("expiry_date")

        # Parse expiry date
        try:
            expiry = datetime.strptime(expiry_raw, "%Y-%m-%d").date()
        except Exception:
            expiry = None

        # ─────────────────────────
        # STATUS FILTERING
        # ─────────────────────────

        if status == "cancelled":
            cancelled.append(donation)
            continue

        if status == "claimed":
            # ✅ Only show claimed donations for THIS NGO
            if str(claimed_by) == str(ngo_id):
                claimed.append(donation)
            continue

        # ─────────────────────────
        # EXPIRY FILTERING
        # ─────────────────────────

        if expiry is None:
            expired.append(donation)
            continue

        if expiry < today:
            expired.append(donation)
            continue

        if today <= expiry <= urgent_limit:
            urgent.append(donation)
            continue

        # ─────────────────────────
        # DEFAULT → AVAILABLE
        # ─────────────────────────
        available.append(donation)

    result = {
        "available": available,
        "urgent": urgent,
        "expired": expired,
        "claimed": claimed,
        "cancelled": cancelled,
    }

    log("Final Output", {
        "available": len(available),
        "urgent": len(urgent),
        "claimed": len(claimed),
        "expired": len(expired),
        "cancelled": len(cancelled),
    })

    return jsonify(result), 200

@ngo_dashboard_bp.route("/api/donations/<donation_id>/claim", methods=["PUT"])
def claim_donation(donation_id):
    try:
        log("CLAIM DONATION REQUEST", donation_id)

        data = request.get_json()
        if not data or not data.get("ngo_id"):
            return jsonify({"error": "Missing ngo_id"}), 400

        ngo_id = data["ngo_id"]

        # 1️⃣ Fetch donation (SAFE)
        response = supabase.table("food_donations") \
            .select("*") \
            .eq("id", donation_id) \
            .execute()

        rows = response.data or []

        if len(rows) == 0:
            return jsonify({"error": "Donation not found"}), 404

        donation = rows[0]

        # 2️⃣ Ensure donation is available
        status = (donation.get("status") or "").lower()
        if status != "available":
            return jsonify({
                "error": "Donation cannot be claimed",
                "current_status": status
            }), 409

        # 3️⃣ Update donation
        update = supabase.table("food_donations") \
            .update({
                "status": "claimed",
                "claimed_by": ngo_id,
                "claimed_date": datetime.utcnow().isoformat()
            }) \
            .eq("id", donation_id) \
            .execute()

        log("DONATION CLAIMED", update.data)

        return jsonify({
            "message": "Donation claimed successfully",
            "donation_id": donation_id,
            "claimed_by": ngo_id
        }), 200

    except Exception as e:
        print("🔥 CLAIM DONATION ERROR:", str(e))
        return jsonify({"error": str(e)}), 500
