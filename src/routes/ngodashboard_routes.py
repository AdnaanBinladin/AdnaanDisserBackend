import os
import os
import traceback
import time
from flask import Blueprint, current_app, request, jsonify
from datetime import datetime, timedelta, date
from supabase import create_client
from flask_mail import Message
from src.utils.audit_log import log_audit
from src.utils.jwt import decode_request_token
from src.utils.mail_instance import mail

ngo_dashboard_bp = Blueprint("ngo_dashboard", __name__)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

supabase = create_client(
    SUPABASE_URL,
    SUPABASE_KEY
)

# Helpers
def log(title, data=None):
    return None


def _execute_with_retry(factory, retries: int = 2, delay_seconds: float = 0.35):
    last_exc = None
    for attempt in range(retries + 1):
        try:
            return factory().execute()
        except Exception as exc:
            last_exc = exc
            msg = str(exc)
            transient = (
                "WinError 10035" in msg
                or "httpx.ReadError" in msg
                or "httpcore.ReadError" in msg
            )
            if transient and attempt < retries:
                time.sleep(delay_seconds)
                continue
            raise
    raise last_exc


def _send_claim_confirmation_email(ngo_id, donation_id, donation, claimed_at_iso):
    ngo_user = (
        supabase.table("users")
        .select("full_name, email")
        .eq("id", ngo_id)
        .single()
        .execute()
    )

    ngo_data = ngo_user.data or {}
    ngo_email = ngo_data.get("email")
    ngo_name = ngo_data.get("full_name") or "NGO partner"

    if not ngo_email:
        current_app.logger.warning(
            "Skipping claim confirmation email for ngo_id=%s: no email found",
            ngo_id,
        )
        return

    donation_title = donation.get("title") or "claimed donation"
    pickup_address = donation.get("pickup_address") or "the agreed pickup location"

    msg = Message(
        subject="FoodShare Claim Confirmation - Show This at Pickup",
        recipients=[ngo_email],
    )
    msg.body = f"""
Hello {ngo_name},

This email confirms that your organization has successfully claimed the donation "{donation_title}" on FoodShare.

When you go to collect the donation, please show this email to the donor as proof that your NGO is the one assigned to the pickup.

Claim details:
- Donation: {donation_title}
- Pickup location: {pickup_address}
- Claimed at: {claimed_at_iso}
- Claim reference: {donation_id}

Thank you for helping reduce food waste.

Warm regards,
FoodShare Team
"""
    mail.send(msg)


def _require_ngo_payload(ngo_id: str | None = None):
    payload = decode_request_token(request)
    if not payload:
        return None, (jsonify({"error": "Invalid or expired token"}), 401)

    role = payload.get("role")
    user_id = payload.get("user_id")
    if role == "admin":
        if ngo_id:
            return payload, None
        return None, (jsonify({"error": "Missing ngoId"}), 400)
    if role != "ngo":
        return None, (jsonify({"error": "NGO access required"}), 403)
    if ngo_id and user_id != ngo_id:
        return None, (jsonify({"error": "Forbidden"}), 403)
    return payload, None


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




# GET NGO DASHBOARD
@ngo_dashboard_bp.route("/ngo-dashboard/", methods=["GET"])
def ngo_dashboard():
    try:
        log("NGO DASHBOARD REQUEST", request.args)

        requested_ngo_id = request.args.get("ngoId")
        payload, auth_error = _require_ngo_payload(requested_ngo_id)
        if auth_error:
            return auth_error
        ngo_id = requested_ngo_id if payload.get("role") == "admin" else payload.get("user_id")

        today = date.today()
        urgent_limit = today + timedelta(days=2)

        # Fetch ALL donations
        donations_res = _execute_with_retry(lambda: (
            supabase.table("food_donations")
            .select("*")
        ))
        rows = donations_res.data or []

        log("Total donations fetched", len(rows))

        if not rows:
            return jsonify({
                "available": [],
                "urgent": [],
                "claimed": [],
                "completed": [],
            }), 200

        # Fetch ALL donors in ONE query (fixed)
        donor_ids = list({
            row["donor_id"]
            for row in rows
            if row.get("donor_id")
        })

        donors_map = {}

        if donor_ids:
            donors_res = _execute_with_retry(lambda: (
                supabase.table("users")
                .select("id, full_name, phone")
                .in_("id", donor_ids)
            ))

            donors_map = {
                d["id"]: d for d in (donors_res.data or [])
            }
 
        # Fetch NGO claims (source of truth)
        claims_res = (
            supabase.table("ngo_claims")
            .select("donation_id, status, claimed_at")
            .eq("ngo_id", ngo_id)
            .order("claimed_at", desc=True) # newest first
            .execute()
        )

        ngo_claims = {}
        for c in (claims_res.data or []):
            donation_id = c["donation_id"]
            if donation_id not in ngo_claims:
                ngo_claims[donation_id] = c



        available = []
        urgent = []
        claimed = []
        completed = []

        # Process donations
        for row in rows:
            donation_id = row.get("id")
            status = (row.get("status") or "").lower()
            final_state = row.get("final_state")
            donor_id = row.get("donor_id")

            expiry = safe_parse_date(row.get("expiry_date"))
            claim = ngo_claims.get(donation_id)

            donor = donors_map.get(donor_id)

            donation = map_donation(row, donor)

            # CLAIMED / COMPLETED (THIS NGO ONLY)
            if claim:
                if claim["status"] == "claimed":
                    donation["claimed_date"] = claim.get("claimed_at")
                    claimed.append(donation)
                    continue

                if claim["status"] == "completed":
                    completed.append(donation)
                    continue

            # AVAILABLE (VISIBILITY RULES)
            if status != "available":
                continue

            # Donor cancelled NEVER visible
            if final_state == "cancelled_by_donor":
                continue

            # NGO cancelled visible again
            if final_state == "cancelled_by_ngo":
                available.append(donation)
                continue

            # System-expired donation -> NEVER visible in available
            if final_state == "expired":
                continue

            # Date-expired (even if auto-expire hasn't run yet)
            if expiry and expiry <= today:
                continue

            # Normal available donation
            if final_state is None:
                available.append(donation)

                # Urgent window
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



# CLAIM DONATION
@ngo_dashboard_bp.route("/donations/<donation_id>/claim", methods=["PUT"])
def claim_donation(donation_id):
    try:
        log("CLAIM DONATION", donation_id)

        data = request.get_json() or {}
        requested_ngo_id = data.get("ngo_id")
        payload, auth_error = _require_ngo_payload(requested_ngo_id)
        if auth_error:
            return auth_error
        ngo_id = requested_ngo_id if payload.get("role") == "admin" else payload.get("user_id")

        # Fetch donation (GUARDS)
        res = _execute_with_retry(lambda: (
            supabase.table("food_donations")
            .select("id, title, pickup_address, status, final_state, expiry_date")
            .eq("id", donation_id)
            .single()
        ))

        if not res.data:
            return jsonify({"error": "Donation not found"}), 404

        donation = res.data
        status = (donation.get("status") or "").lower()
        final_state = donation.get("final_state")
        expiry = safe_parse_date(donation.get("expiry_date"))

        # Block only truly finalized donations.
        # "cancelled_by_ngo" is intentionally reclaimable.
        if final_state in ("cancelled_by_donor", "expired"):
            return jsonify({"error": "Donation is no longer available"}), 409

        # Must be available
        if status != "available":
            return jsonify({
                "error": "Donation cannot be claimed",
                "current_status": status
            }), 409

        # Expired by date (same-day expiry is treated as expired)
        if expiry and expiry <= date.today():
            return jsonify({"error": "Donation has expired"}), 409

        now = datetime.utcnow().isoformat()

        # Create NGO claim (source of truth)
        payload = {
            "donation_id": donation_id,
            "ngo_id": ngo_id,
            "status": "claimed",     # ✅ matches DB constraint
            "claimed_at": now,
        }

        log("🧾 NGO CLAIM INSERT PAYLOAD", payload)

        _execute_with_retry(lambda: supabase.table("ngo_claims").insert(payload))

        # Update donation snapshot (NO claimed_date)
        _execute_with_retry(lambda: supabase.table("food_donations").update({
            "status": "claimed",
            "final_state": None,
            "updated_at": now,
        }).eq("id", donation_id))

        # Send NGO proof-of-claim email (best effort)
        try:
            _send_claim_confirmation_email(ngo_id, donation_id, donation, now)
        except Exception as email_err:
            current_app.logger.exception(
                "Claim confirmation email failed for ngo_id=%s donation_id=%s: %s",
                ngo_id,
                donation_id,
                email_err,
            )

        log("DONATION CLAIMED SUCCESS", donation_id)

        log_audit(
            "donation_claimed",
            user_id=ngo_id,
            user_role="ngo",
            entity_type="donation",
            entity_id=donation_id,
            metadata={"status": "claimed"},
            req=request,
        )
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
        requested_ngo_id = request.args.get("ngoId")
        payload, auth_error = _require_ngo_payload(requested_ngo_id)
        if auth_error:
            return auth_error
        ngo_id = requested_ngo_id if payload.get("role") == "admin" else payload.get("user_id")

        today = date.today()
        urgent_limit = today + timedelta(days=2)

        # BASE AVAILABLE ACTIVE DONATIONS ONLY
        # (this is WHY you used .is_(None))
        base_available_res = _execute_with_retry(lambda: (
            supabase.table("food_donations")
            .select("id, expiry_date, final_state")
            .eq("status", "available")
            .is_("final_state", None)
        ))
        base_available = base_available_res.data or []

        available = []
        urgent = []

        # APPLY DOMAIN RULES ON TOP
        for d in base_available:
            expiry = safe_parse_date(d.get("expiry_date"))

            # Skip date-expired rows (same-day expiry treated as expired)
            if expiry and expiry <= today:
                continue

            # Normal available
            available.append(d)

            # Urgent window
            if expiry and today <= expiry <= urgent_limit:
                urgent.append(d)

        # NGO CLAIM COUNTS (source of truth)
        claims_res = _execute_with_retry(lambda: (
            supabase.table("ngo_claims")
            .select("status")
            .eq("ngo_id", ngo_id)
        ))

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
        requested_ngo_id = request.args.get("ngoId")
        payload, auth_error = _require_ngo_payload(requested_ngo_id)
        if auth_error:
            return auth_error
        ngo_id = requested_ngo_id if payload.get("role") == "admin" else payload.get("user_id")

        # Fetch NGO claims (HISTORY = source of truth)
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

        # Fetch related donations IN BULK (SAFE)
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

        # Merge results (HISTORY IS NEVER FILTERED)
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


# NGO IMPACT ANALYTICS




@ngo_dashboard_bp.route("/ngo/impact", methods=["GET"])
def ngo_impact():
    try:
        requested_ngo_id = request.args.get("ngoId")
        payload, auth_error = _require_ngo_payload(requested_ngo_id)
        if auth_error:
            return auth_error
        ngo_id = requested_ngo_id if payload.get("role") == "admin" else payload.get("user_id")

        # UNIT KG CONVERSION (BACKEND source of truth)
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

        # FETCH NGO CLAIM HISTORY (source of truth)
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

        # COLLECT DONATION IDS
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

        # FETCH DONATIONS
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

        # AGGREGATION
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

            # COUNT CLAIMS (CLAIMED + COMPLETED)
            if status in ("claimed", "completed"):
                total_claims += 1

            # MONTHLY CLAIMS
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

            # IMPACT ONLY FROM COMPLETED
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

        # DERIVED METRICS
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

        # FINAL RESPONSE
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
