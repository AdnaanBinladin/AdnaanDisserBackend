import os
import traceback
from flask import Blueprint, request, jsonify
from datetime import datetime, timedelta, date
from supabase import create_client

# =====================================================
# Blueprint
# =====================================================
ngo_dashboard_bp = Blueprint("ngo_dashboard", __name__)

# =====================================================
# Supabase client
# =====================================================
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

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

        # -------------------------------------------------
        # Fetch all donations
        # -------------------------------------------------
        response = supabase.table("food_donations").select("*").execute()
        rows = response.data or []

        log("Total donations fetched", len(rows))

        today = date.today()
        urgent_limit = today + timedelta(days=2)

        available = []
        urgent = []
        expired = []
        claimed = []
        completed = []
        cancelled = []

        # -------------------------------------------------
        # Process each donation
        # -------------------------------------------------
        for row in rows:
            status = (row.get("status") or "").lower()
            claimed_by = row.get("claimed_by")
            donor_id = row.get("donor_id")

            expiry = safe_parse_date(row.get("expiry_date"))

            # -----------------------------
            # Fetch donor ONLY if needed
            # -----------------------------
            donor = None
            if donor_id:
                try:
                    donor_res = (
                        supabase.table("users")
                        .select("full_name, phone")
                        .eq("id", donor_id)
                        .single()
                        .execute()
                    )
                    donor = donor_res.data
                except Exception:
                    donor = None

            donation = map_donation(row, donor)

            # -----------------------------
            # STATUS HANDLING
            # -----------------------------
            if status == "cancelled":
               if str(claimed_by) == str(ngo_id):
                  cancelled.append(donation)
                  continue


            if status in ["claimed"]:
                # Only show claimed/completed if THIS NGO claimed it
                if str(claimed_by) == str(ngo_id):
                    claimed.append(donation)
                continue

            if status == "completed":
               if str(claimed_by) == str(ngo_id):
                  completed.append(donation)
                  continue

            # -----------------------------
            # EXPIRY HANDLING
            # -----------------------------
            if not expiry:
                expired.append(donation)
                continue

            if expiry < today:
                expired.append(donation)
                continue

            if today <= expiry <= urgent_limit:
                urgent.append(donation)
                

            # -----------------------------
            # DEFAULT → AVAILABLE
            # -----------------------------
            available.append(donation)

        result = {
    "available": available,
    "urgent": urgent,
    "claimed": claimed,
    "completed": completed,
    "cancelled": cancelled,
    "expired": expired,
}


        log("NGO DASHBOARD RESULT", {
            "available": len(available),
            "urgent": len(urgent),
            "claimed": len(claimed),
            "expired": len(expired),
            "cancelled": len(cancelled),
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
        # Fetch donation
        # -------------------------------------------------
        res = (
            supabase.table("food_donations")
            .select("*")
            .eq("id", donation_id)
            .execute()
        )

        if not res.data:
            return jsonify({"error": "Donation not found"}), 404

        donation = res.data[0]
        status = (donation.get("status") or "").lower()

        if status != "available":
            return jsonify({
                "error": "Donation cannot be claimed",
                "current_status": status
            }), 409

        # -------------------------------------------------
        # Update donation
        # -------------------------------------------------
        update = (
            supabase.table("food_donations")
            .update({
                "status": "claimed",
                "claimed_by": ngo_id,
                "claimed_date": datetime.utcnow().isoformat(),
                "updated_at": datetime.utcnow().isoformat(),
            })
            .eq("id", donation_id)
            .execute()
        )

        log("DONATION CLAIMED SUCCESS", update.data)

        return jsonify({
            "message": "Donation claimed successfully",
            "donation_id": donation_id,
            "claimed_by": ngo_id,
        }), 200

    except Exception as e:
        print("\n🔥 CLAIM DONATION ERROR 🔥")
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
