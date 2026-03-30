from flask import Blueprint, request, jsonify
from src.services.supabase_service import supabase
from flask_mail import Message
from src.utils.mail_instance import mail
from src.utils.audit_log import log_audit
from src.utils.jwt import decode_request_token
import traceback
import qrcode
import io
import base64
import time
from datetime import date, datetime, timedelta





donation_bp = Blueprint("donation", __name__)

ALLOWED_CATEGORIES = {
    "fruits",
    "vegetables",
    "dairy",
    "meat",
    "grains",
    "prepared_food",
    "other",
}
ALLOWED_UNITS = {"kg", "pieces", "liters", "boxes"}
ALLOWED_URGENCY = {"low", "medium", "high"}
MAX_DONATION_QUANTITY = 1000
MAX_TITLE_LENGTH = 120
MAX_DESCRIPTION_LENGTH = 1000
MAX_PICKUP_ADDRESS_LENGTH = 255
MAX_PICKUP_INSTRUCTIONS_LENGTH = 500


def _require_auth_payload():
    payload = decode_request_token(request)
    if not payload:
        return None, (jsonify({"error": "Invalid or expired token"}), 401)
    return payload, None


def _require_donor_access(donor_id: str | None = None):
    payload, auth_error = _require_auth_payload()
    if auth_error:
        return None, auth_error

    role = payload.get("role")
    user_id = payload.get("user_id")
    if role == "admin":
        return payload, None
    if role != "donor":
        return None, (jsonify({"error": "Donor access required"}), 403)
    if donor_id and user_id != donor_id:
        return None, (jsonify({"error": "Forbidden"}), 403)
    return payload, None


def _require_ngo_access(ngo_id: str | None = None):
    payload, auth_error = _require_auth_payload()
    if auth_error:
        return None, auth_error

    role = payload.get("role")
    user_id = payload.get("user_id")
    if role == "admin":
        return payload, None
    if role != "ngo":
        return None, (jsonify({"error": "NGO access required"}), 403)
    if ngo_id and user_id != ngo_id:
        return None, (jsonify({"error": "Forbidden"}), 403)
    return payload, None


def _execute_with_retry(factory, retries: int = 2, delay_seconds: float = 0.35):
    """
    Small retry wrapper for transient Supabase/httpx transport failures
    (common on Windows dev with HTTP/2).
    """
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


def _normalize_optional_text(value, max_length: int):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Invalid text value")

    normalized = value.strip()
    if not normalized:
        return None
    if len(normalized) > max_length:
        raise ValueError(f"Text exceeds max length of {max_length} characters")
    return normalized


def _normalize_required_text(value, field_name: str, max_length: int):
    if not isinstance(value, str):
        raise ValueError(f"{field_name} is required")

    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    if len(normalized) > max_length:
        raise ValueError(f"{field_name} cannot exceed {max_length} characters")
    return normalized


def _parse_quantity(raw_value):
    try:
        quantity = int(raw_value)
    except (ValueError, TypeError):
        raise ValueError("Invalid quantity value")

    if quantity <= 0:
        raise ValueError("Quantity must be greater than zero")
    if quantity > MAX_DONATION_QUANTITY:
        raise ValueError(f"Quantity cannot exceed {MAX_DONATION_QUANTITY}")
    return quantity


def _parse_future_expiry(raw_value):
    try:
        expiry_date = date.fromisoformat(raw_value)
    except (ValueError, TypeError):
        raise ValueError("Invalid expiry date format (YYYY-MM-DD required)")

    if expiry_date <= date.today():
        raise ValueError("Expiry date must be a future date")
    return expiry_date


def _parse_pickup_coordinates(data, require_both: bool):
    pickup_lat = data.get("pickup_lat")
    pickup_lng = data.get("pickup_lng")

    if pickup_lat is None and pickup_lng is None:
        if require_both:
            raise ValueError("Pickup location coordinates are required")
        return None, None

    if pickup_lat is None or pickup_lng is None:
        raise ValueError("Pickup latitude and longitude must both be provided")

    try:
        pickup_lat = float(pickup_lat)
        pickup_lng = float(pickup_lng)
    except (ValueError, TypeError):
        raise ValueError("Invalid latitude or longitude format")

    if not (-90 <= pickup_lat <= 90 and -180 <= pickup_lng <= 180):
        raise ValueError("Latitude or longitude out of range")

    return pickup_lat, pickup_lng




# Add a new donation (with QR code)
# POST /api/donations/add
@donation_bp.route("/donations/add", methods=["POST"])
def add_donation():
    try:
        data = request.get_json() or {}
        payload, auth_error = _require_donor_access(data.get("donor_id"))
        if auth_error:
            return auth_error

        effective_donor_id = (
            data.get("donor_id")
            if payload.get("role") == "admin"
            else payload.get("user_id")
        )

        # Required and normalized text fields
        try:
            title = _normalize_required_text(data.get("title"), "Title", MAX_TITLE_LENGTH)
            category = _normalize_required_text(data.get("category"), "Category", 40).lower()
            unit = _normalize_required_text(data.get("unit"), "Unit", 20).lower()
            pickup_address = _normalize_required_text(
                data.get("pickup_address"),
                "Pickup address",
                MAX_PICKUP_ADDRESS_LENGTH,
            )
            description = _normalize_optional_text(data.get("description"), MAX_DESCRIPTION_LENGTH)
            pickup_instructions = _normalize_optional_text(
                data.get("pickup_instructions"),
                MAX_PICKUP_INSTRUCTIONS_LENGTH,
            )
            urgency = _normalize_optional_text(data.get("urgency"), 20)
            urgency = urgency.lower() if urgency else "medium"

            quantity = _parse_quantity(data.get("quantity"))
            expiry_date = _parse_future_expiry(data.get("expiry_date"))
            pickup_lat, pickup_lng = _parse_pickup_coordinates(data, require_both=True)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        if category not in ALLOWED_CATEGORIES:
            return jsonify({"error": "Invalid category"}), 400

        if unit not in ALLOWED_UNITS:
            return jsonify({"error": "Invalid unit"}), 400

        if urgency not in ALLOWED_URGENCY:
            return jsonify({"error": "Invalid urgency"}), 400

        # Step 1: Insert new donation
        donation = {
            "donor_id": effective_donor_id,
            "title": title,
            "description": description,
            "category": category,
            "quantity": quantity,
            "unit": unit,
            "expiry_date": expiry_date.isoformat(),
            "pickup_address": pickup_address,
            "pickup_lat": pickup_lat,
            "pickup_lng": pickup_lng,
            "pickup_instructions": pickup_instructions,
            "status": "available",
            "urgency": urgency,
            "created_at": datetime.utcnow().isoformat(),
            "updated_at": datetime.utcnow().isoformat(),
        }

        result = _execute_with_retry(lambda: supabase.table("food_donations").insert(donation))
        if not result.data:
            return jsonify({"error": "Failed to insert donation"}), 500

        donation_id = result.data[0]["id"]

        pickup_url = f"https://da1b-89-39-107-204.ngrok-free.app/api/donations/{donation_id}/pickup-confirm"
        qr_img = qrcode.make(pickup_url)
        buf = io.BytesIO()
        qr_img.save(buf, format="PNG")
        qr_base64 = base64.b64encode(buf.getvalue()).decode("utf-8")

        _execute_with_retry(
            lambda: supabase.table("food_donations").update({"qr_code": qr_base64}).eq("id", donation_id)
        )

        try:
            _execute_with_retry(
                lambda: supabase.table("notifications").insert({
                    "user_id": effective_donor_id,
                    "title": "Donation Created",
                    "message": f"Your donation '{title}' has been created with a pickup QR code for NGO verification.",
                    "type": "status_update",
                    "read": False,
                    "created_at": datetime.utcnow().isoformat(),
                })
            )
        except Exception as notification_err:
            print("Notification insert failed:", notification_err)

        try:
            donor = _execute_with_retry(
                lambda: supabase.table("users")
                .select("email, full_name")
                .eq("id", effective_donor_id)
                .single()
            )
            if donor.data and donor.data.get("email"):
                msg = Message(
                    subject="Donation Created - QR Code Ready",
                    recipients=[donor.data["email"]],
                    body=(
                        f"Hi {donor.data.get('full_name', 'Donor')},\n\n"
                        f"Thank you for your donation '{title}'!\n\n"
                        f"Attached is your QR code for NGO pickup verification.\n\n"
                        "Warm regards,\nFoodShare Team"
                    ),
                )
                msg.attach("pickup_qr.png", "image/png", buf.getvalue())
                mail.send(msg)
        except Exception as email_err:
            print("Email sending failed:", email_err)

        log_audit(
            "donation_posted",
            user_id=effective_donor_id,
            user_role="donor",
            entity_type="donation",
            entity_id=donation_id,
            metadata={"title": title, "category": category},
            req=request,
        )

        return jsonify({
            "message": "Donation added successfully with QR code!",
            "qr_code": qr_base64,
            "data": result.data
        }), 201

    except Exception as e:
        print("Add donation error:", e)
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


# edit
@donation_bp.route("/donations/<donation_id>", methods=["PUT"])
def update_donation(donation_id):
    try:
        data = request.get_json() or {}

        # Fetch existing donation (GUARD)
        existing = (
            supabase.table("food_donations")
            .select("status, final_state, donor_id")
            .eq("id", donation_id)
            .single()
            .execute()
        )

        if not existing.data:
            return jsonify({"error": "Donation not found"}), 404

        payload, auth_error = _require_donor_access(existing.data.get("donor_id"))
        if auth_error:
            return auth_error

        # Block edits if donation is finalized
        if existing.data.get("final_state") is not None:
            return jsonify({
                "error": "This donation is no longer editable"
            }), 400

        # Block edits if already completed
        if existing.data.get("status") == "completed":
            return jsonify({
                "error": "Completed donations cannot be edited"
            }), 400

        # Allowed editable fields
        allowed_fields = [
            "title",
            "description",
            "category",
            "quantity",
            "unit",
            "expiry_date",
            "pickup_address",
            "pickup_lat",
            "pickup_lng",
            "pickup_instructions",
            "urgency",
        ]

        update_data = {}

        for field in allowed_fields:
            if field not in data:
                continue

            value = data[field]

            try:
                if field == "title":
                    value = _normalize_required_text(value, "Title", MAX_TITLE_LENGTH)

                if field == "description":
                    value = _normalize_optional_text(value, MAX_DESCRIPTION_LENGTH)

                if field == "category":
                    value = _normalize_required_text(value, "Category", 40).lower()
                    if value not in ALLOWED_CATEGORIES:
                        return jsonify({"error": "Invalid category"}), 400

                if field == "quantity":
                    value = _parse_quantity(value)

                if field == "unit":
                    value = _normalize_required_text(value, "Unit", 20).lower()
                    if value not in ALLOWED_UNITS:
                        return jsonify({"error": "Invalid unit"}), 400

                if field == "expiry_date":
                    value = _parse_future_expiry(value).isoformat()

                if field == "pickup_address":
                    value = _normalize_required_text(value, "Pickup address", MAX_PICKUP_ADDRESS_LENGTH)

                if field == "pickup_instructions":
                    value = _normalize_optional_text(value, MAX_PICKUP_INSTRUCTIONS_LENGTH)

                if field == "urgency":
                    value = _normalize_required_text(value, "Urgency", 20).lower()
                    if value not in ALLOWED_URGENCY:
                        return jsonify({"error": "Invalid urgency"}), 400

            except ValueError as exc:
                return jsonify({"error": str(exc)}), 400

            update_data[field] = value

        if "pickup_lat" in data or "pickup_lng" in data:
            try:
                pickup_lat, pickup_lng = _parse_pickup_coordinates(data, require_both=True)
            except ValueError as exc:
                return jsonify({"error": str(exc)}), 400
            update_data["pickup_lat"] = pickup_lat
            update_data["pickup_lng"] = pickup_lng

        if not update_data:
            return jsonify({
                "error": "No valid fields to update"
            }), 400

        update_data["updated_at"] = datetime.utcnow().isoformat()

        # Perform update
        result = (
            supabase.table("food_donations")
            .update(update_data)
            .eq("id", donation_id)
            .execute()
        )

        if not result.data:
            return jsonify({"error": "Donation not found"}), 404

        log_audit(
            "donation_edited",
            user_id=existing.data.get("donor_id") if payload.get("role") != "admin" else payload.get("user_id"),
            user_role=payload.get("role"),
            entity_type="donation",
            entity_id=donation_id,
            metadata={"updated_fields": list(update_data.keys())},
            req=request,
        )
        return jsonify({"data": result.data}), 200

    except Exception as e:
        print("⚠️ Update donation error:", e)
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


# List all donations for a specific donor
# GET /api/donations/list/<donor_id>
@donation_bp.route("/donations/list/<donor_id>", methods=["GET"])
def list_donations(donor_id):
    try:
        _, auth_error = _require_donor_access(donor_id)
        if auth_error:
            return auth_error

        # make sure qr_code column is fetched
        response = _execute_with_retry(lambda: (
            supabase.table("food_donations")
            .select("*") # fetch all columns including qr_code
            .eq("donor_id", donor_id)
            .order("created_at", desc=True)
        ))

        data = response.data or []

        # debugging info
        return jsonify({"data": data}), 200

    except Exception as e:
        print("⚠️ list_donations error:", e)
        import traceback

        traceback.print_exc()
        message = str(e)
        if (
            "WinError 10035" in message
            or "httpx.ReadError" in message
            or "httpcore.ReadError" in message
        ):
            return jsonify({"error": "Temporary database connectivity issue. Please retry."}), 503
        return jsonify({"error": str(e)}), 500


# Cancel a donation
@donation_bp.route("/donations/<donation_id>/cancel", methods=["PUT"])
def cancel_donation(donation_id):
    try:
        now = datetime.utcnow().isoformat()


        # Fetch existing donation (GUARD)
        existing = (
            supabase.table("food_donations")
            .select("id, donor_id, title, status, final_state")
            .eq("id", donation_id)
            .single()
            .execute()
        )

        if not existing.data:
            return jsonify({"error": "Donation not found"}), 404

        payload, auth_error = _require_donor_access(existing.data.get("donor_id"))
        if auth_error:
            return auth_error

        # Completed donations are immutable
        if existing.data.get("status") == "completed":
            return jsonify({
                "error": "Completed donations cannot be cancelled"
            }), 400

        # Block donor double-cancel only
        if existing.data.get("final_state") == "cancelled_by_donor":
            return jsonify({
                "error": "This donation was already cancelled by the donor"
            }), 400

        # Cancel any ACTIVE NGO claim (history preserved)
        supabase.table("ngo_claims").update({
            "status": "cancelled",
            "cancelled_at": now
        }).eq("donation_id", donation_id).eq("status", "claimed").execute()

        # Apply DONOR cancellation (FINAL)
        supabase.table("food_donations").update({
            "status": "available",                # safe default
            "final_state": "cancelled_by_donor",  # donor owns this
            "updated_at": now,
        }).eq("id", donation_id).execute()

        # In-app notification
        supabase.table("notifications").insert({
            "user_id": existing.data["donor_id"],
            "title": "Donation Cancelled ❌",
            "message": (
                f"Your donation '{existing.data['title']}' has been cancelled "
                f"and is no longer available for pickup."
            ),
            "type": "status_update",
            "read": False,
            "created_at": now,
        }).execute()

        # Email notification (best effort)
        try:
            donor = (
                supabase.table("users")
                .select("email, full_name")
                .eq("id", existing.data["donor_id"])
                .single()
                .execute()
            )

            if donor.data and donor.data.get("email"):
                msg = Message(
                    subject="❌ Donation Cancelled - FoodShare",
                    recipients=[donor.data["email"]],
                    body=(
                        f"Hi {donor.data.get('full_name', 'Donor')},\n\n"
                        f"Your donation titled '{existing.data['title']}' "
                        f"has been successfully cancelled.\n\n"
                        f"It will remain in your history for reference.\n\n"
                        f"Warm regards,\n"
                        f"The FoodShare Team 🌱"
                    ),
                )
                mail.send(msg)

        except Exception as email_err:
            print("⚠️ Email sending failed (cancel donation):", email_err)
            traceback.print_exc()

        log_audit(
            "donation_cancelled",
            user_id=payload.get("user_id"),
            user_role=payload.get("role"),
            entity_type="donation",
            entity_id=donation_id,
            metadata={"title": existing.data.get("title"), "reason": "cancelled_by_donor"},
            req=request,
        )
        return jsonify({
            "message": "Donation cancelled successfully"
        }), 200

    except Exception as e:
        print("⚠️ Cancel donation error:", e)
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

# Automatically mark expired donations
# PUT /api/donations/auto-expire
@donation_bp.route("/donations/auto-expire", methods=["PUT"])
def auto_expire_donations():
    """
    Marks donations as expired when:
    - expiry_date <= today
    - final_state IS NULL  (not cancelled by donor, not already expired)

    Important rules:
    - Donor cancellation is FINAL → never overridden
    - NGO cancellation does NOT use final_state → donation may still expire
    - Status and claim fields are NOT modified
    """
    try:
        today = date.today().isoformat()
        now = datetime.utcnow().isoformat()



        print("🕒 Running auto-expire check. Today:", today)

        # Fetch ONLY unfinalized donations past expiry
        to_expire = (
            supabase.table("food_donations")
            .select("id, donor_id, title, expiry_date, final_state")
            .lte("expiry_date", today)
            .is_("final_state", None) # excludes donor-cancelled & already expired
            .neq("status", "completed")
            .execute()
        )

        if not to_expire.data:
            print("✅ No donations to expire today.")
            return jsonify({"message": "No donations to expire today"}), 200

        expired_count = 0

        for donation in to_expire.data:
            donation_id = donation["id"]
            donor_id = donation["donor_id"]
            title = donation["title"]

            # Mark donation as expired (FINAL STATE)
            supabase.table("food_donations").update({
                "final_state": "expired",
                "updated_at": now,
            }).eq("id", donation_id).execute()

            expired_count += 1

            # In-app notification
            supabase.table("notifications").insert({
                "user_id": donor_id,
                "title": "Donation Expired ⚠️",
                "message": (
                    f"Your donation '{title}' has reached its expiry date "
                    f"and is no longer available for pickup."
                ),
                "type": "status_update",
                "read": False,
                "created_at": now,
            }).execute()

            # Email notification (best effort)
            try:
                donor = (
                    supabase.table("users")
                    .select("email, full_name")
                    .eq("id", donor_id)
                    .single()
                    .execute()
                )

                if donor.data and donor.data.get("email"):
                    msg = Message(
                        subject="⚠️ Donation Expired - FoodShare",
                        recipients=[donor.data["email"]],
                        body=(
                            f"Hi {donor.data.get('full_name', 'Donor')},\n\n"
                            f"Your donation titled '{title}' has expired and "
                            f"is no longer visible to NGOs.\n\n"
                            f"It will remain in your dashboard for reference.\n\n"
                            f"Thank you for supporting FoodShare 🌱\n\n"
                            f"Warm regards,\n"
                            f"The FoodShare Team"
                        ),
                    )
                    mail.send(msg)

            except Exception as email_err:
                print(
                    f"⚠️ Email sending failed for expired donation {donation_id}:",
                    email_err,
                )

        print(f"✅ {expired_count} donations marked as expired.")
        log_audit(
            "donations_expired_auto",
            user_role="system",
            entity_type="donation",
            metadata={"expired_count": expired_count},
            req=request,
        )
        return jsonify({
            "message": f"{expired_count} donations marked as expired"
        }), 200

    except Exception as e:
        print("⚠️ Auto-expire error:", e)
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

# Send Reminder Notifications for Expiring Donations
@donation_bp.route("/donations/send-reminders", methods=["PUT"])
def send_expiry_reminders():
    """
    Sends reminders for donations expiring within 24 hours.

    IMPORTANT:
    - This reminder is informational ONLY
    - It is sent even if the donation is:
        - cancelled
        - expired
        - still available
    - Does NOT change status or final_state
    """
    try:
        now = datetime.utcnow()
        tomorrow = now + timedelta(days=1)

        today_str = now.date().isoformat()
        tomorrow_str = tomorrow.date().isoformat()

        print(f"🕒 Checking for donations expiring between {today_str} and {tomorrow_str}")

        # Fetch ALL donations expiring within 24h
        # (regardless of status / final_state)
        expiring = (
            supabase.table("food_donations")
            .select("id, donor_id, title, expiry_date, status, final_state")
            .gte("expiry_date", today_str)
            .lte("expiry_date", tomorrow_str)
            .execute()
        )

        if not expiring.data:
            print("✅ No expiring donations found.")
            return jsonify({"message": "No donations expiring soon."}), 200

        count = 0
        created_at = now.isoformat()

        for donation in expiring.data:
            donor_id = donation["donor_id"]
            title = donation["title"]
            status = donation.get("status")
            final_state = donation.get("final_state")

            # Context-aware message
            if final_state == "expired":
                message = (
                    f"Your donation '{title}' has expired. "
                    f"It remains in your history for reference."
                )
            elif final_state == "cancelled_by_donor":
                message = (
                    f"Your donation '{title}' was cancelled by you "
                    f"and is approaching its original expiry date."
                )
            elif final_state == "cancelled_by_ngo":
                message = (
                    f"Your donation '{title}' was cancelled by an NGO "
                    f"and is approaching its original expiry date."
                )
            else:
                message = (
                    f"Your donation '{title}' will expire within 24 hours. "
                    f"Please ensure pickup or update the expiry date."
                )

            # In-app notification
            supabase.table("notifications").insert({
                "user_id": donor_id,
                "title": "⏰ Donation Expiry Reminder",
                "message": message,
                "type": "reminder",
                "read": False,
                "created_at": created_at,
            }).execute()

            # Email reminder (best effort)
            try:
                donor = (
                    supabase.table("users")
                    .select("email, full_name")
                    .eq("id", donor_id)
                    .single()
                    .execute()
                )

                if donor.data and donor.data.get("email"):
                    msg = Message(
                        subject="⏰ Donation Expiry Reminder - FoodShare",
                        recipients=[donor.data["email"]],
                        body=(
                            f"Hi {donor.data.get('full_name', 'Donor')},\n\n"
                            f"{message}\n\n"
                            f"This notification is for your reference only.\n\n"
                            f"Thank you for supporting FoodShare 🌱\n\n"
                            f"Warm regards,\n"
                            f"The FoodShare Team"
                        ),
                    )
                    mail.send(msg)
                    print(f"📩 Reminder email sent to {donor.data['email']}")

            except Exception as email_err:
                print(f"⚠️ Email sending failed for donation '{title}':", email_err)

            count += 1

        print(f"✅ {count} reminder notifications sent successfully.")
        log_audit(
            "expiry_reminders_sent",
            user_role="system",
            entity_type="donation",
            metadata={"reminder_count": count},
            req=request,
        )
        return jsonify({
            "message": f"{count} reminder notifications sent."
        }), 200

    except Exception as e:
        print("⚠️ Reminder job error:", e)
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500




# Pickup confirmation page (NGO QR scan)
@donation_bp.route("/donations/<donation_id>/pickup-confirm", methods=["GET"])
def confirm_pickup(donation_id):
    try:
        # Fetch donation (STRICT GUARDS)
        existing = (
            supabase.table("food_donations")
            .select(
                "id, title, status, final_state, donor_id"
            )
            .eq("id", donation_id)
            .single()
            .execute()
        )

        if not existing.data:
            return (
                "<h1>❌ Donation Not Found</h1>"
                "<p>This QR code may be invalid or expired.</p>",
                404,
                {"Content-Type": "text/html"},
            )

        donation = existing.data
        status = donation.get("status")
        final_state = donation.get("final_state")
        donation_title = donation.get("title", "Unknown Donation")

        # Block finalized donations
        if final_state is not None:
            return (
                "<h1>❌ Donation No Longer Active</h1>"
                "<p>This donation has been cancelled or expired.</p>",
                400,
                {"Content-Type": "text/html"},
            )

        # Prevent double pickup
        if status == "completed":
            return (
                "<h1>⚠️ Pickup Already Confirmed</h1>"
                "<p>This donation was already marked as picked up.</p>",
                200,
                {"Content-Type": "text/html"},
            )

        # Only CLAIMED donations can be picked up
        if status != "claimed":
            return (
                "<h1>❌ Pickup Not Allowed</h1>"
                "<p>This donation has not been claimed by an NGO.</p>",
                400,
                {"Content-Type": "text/html"},
            )

        now = datetime.utcnow().isoformat()


        # Mark donation as COMPLETED
        supabase.table("food_donations").update({
            "status": "completed",
            "updated_at": now,
        }).eq("id", donation_id).execute()

        # Mark NGO claim as COMPLETED
        supabase.table("ngo_claims").update({
             "status": "completed",
             "completed_at": now,
             "updated_at": now,
       }).eq("donation_id", donation_id).eq("status", "claimed").execute()

        completed_claim = (
            supabase.table("ngo_claims")
            .select("ngo_id")
            .eq("donation_id", donation_id)
            .eq("status", "completed")
            .order("completed_at", desc=True)
            .limit(1)
            .maybe_single()
            .execute()
        )

        ngo_name = "Assigned NGO"
        ngo_id = (completed_claim.data or {}).get("ngo_id")
        if ngo_id:
            ngo_user = (
                supabase.table("users")
                .select("full_name")
                .eq("id", ngo_id)
                .maybe_single()
                .execute()
            )
            ngo_name = (ngo_user.data or {}).get("full_name") or ngo_name

        pickup_time = datetime.utcnow().strftime("%d %b %Y, %I:%M %p UTC")

        # Notify donor
        supabase.table("notifications").insert({
            "user_id": donation["donor_id"],
            "title": "Donation Picked Up ✅",
            "message": (
                f"Your donation '{donation_title}' has been successfully "
                f"picked up by the NGO."
            ),
            "type": "status_update",
            "read": False,
            "created_at": now,
        }).execute()

        # Email notification (best effort)
        try:
            donor = (
                supabase.table("users")
                .select("email, full_name")
                .eq("id", donation["donor_id"])
                .single()
                .execute()
            )

            if donor.data and donor.data.get("email"):
                msg = Message(
                    subject="✅ Donation Pickup Confirmed - FoodShare",
                    recipients=[donor.data["email"]],
                    body=(
                        f"Hi {donor.data.get('full_name', 'Donor')},\n\n"
                        f"Your donation titled '{donation_title}' has been "
                        f"successfully picked up by the NGO.\n\n"
                        f"Thank you for making a difference 🌱\n\n"
                        f"Warm regards,\n"
                        f"The FoodShare Team"
                    ),
                )
                mail.send(msg)

        except Exception as email_err:
            print("⚠️ Email sending failed (pickup confirm):", email_err)

        # Success HTML response
        html = f"""
        <!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="UTF-8" />
            <meta name="viewport" content="width=device-width, initial-scale=1.0" />
            <title>Pickup Confirmed - FoodShare</title>
        </head>
        <body style="margin:0;font-family:Arial,sans-serif;background:linear-gradient(135deg,#fff7ed 0%,#ecfdf5 100%);color:#1f2937;">
            <div style="min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px;box-sizing:border-box;">
                <div style="width:100%;max-width:460px;background:#ffffff;border:1px solid # d1fae5;border-radius:24px;box-shadow:0 20px 45px rgba(16,24,40,0.12);overflow:hidden;">
                    <div style="background:linear-gradient(90deg,#16a34a 0%,#22c55e 55%,#86efac 100%);padding:24px 28px;color:#ffffff;text-align:center;">
                        <div style="width:64px;height:64px;margin:0 auto 14px;background:rgba(255,255,255,0.18);border-radius:999px;display:flex;align-items:center;justify-content:center;font-size:30px;font-weight:bold;">OK</div>
                        <div style="font-size:28px;font-weight:700;line-height:1.2;">Pickup Confirmed</div>
                        <div style="margin-top:8px;font-size:14px;opacity:0.95;">This donation has been successfully marked as picked up.</div>
                    </div>
                    <div style="padding:24px 24px 28px;">
                        <div style="background:#f0fdf4;border:1px solid # bbf7d0;border-radius:18px;padding:18px 16px;">
                            <div style="font-size:12px;letter-spacing:0.08em;text-transform:uppercase;color:#15803d;font-weight:700;margin-bottom:14px;">Pickup Details</div>
                            <div style="display:grid;gap:12px;">
                                <div>
                                    <div style="font-size:12px;color:#6b7280;margin-bottom:4px;">Donation</div>
                                    <div style="font-size:17px;font-weight:700;color:#111827;">{donation_title}</div>
                                </div>
                                <div>
                                    <div style="font-size:12px;color:#6b7280;margin-bottom:4px;">Picked Up At</div>
                                    <div style="font-size:15px;font-weight:600;color:#111827;">{pickup_time}</div>
                                </div>
                                <div>
                                    <div style="font-size:12px;color:#6b7280;margin-bottom:4px;">Collected By</div>
                                    <div style="font-size:15px;font-weight:600;color:#111827;">{ngo_name}</div>
                                </div>
                            </div>
                        </div>
                        <p style="margin:20px 4px 0;text-align:center;font-size:14px;line-height:1.6;color:#4b5563;">
                            Thank you for supporting FoodShare and helping reduce food waste in the community.
                        </p>
                    </div>
                </div>
            </div>
        </body>
        </html>
        """

        log_audit(
            "donation_completed",
            entity_type="donation",
            entity_id=donation_id,
            metadata={"status": "completed_via_qr"},
            req=request,
        )
        return html, 200, {"Content-Type": "text/html; charset=utf-8"}

    except Exception as e:
        print("⚠️ Pickup confirmation error:", e)
        traceback.print_exc()
        return (
            "<h1>❌ Error</h1>"
            "<p>Something went wrong while confirming pickup.</p>",
            500,
            {"Content-Type": "text/html"},
        )



# Auto-cancel claimed donations after 24h
# PUT /api/donations/auto-cancel-claims
@donation_bp.route("/donations/auto-cancel-claims", methods=["PUT"])
def auto_cancel_claimed_donations():
    """
    Auto-cancel NGO claims that were not picked up within 24 hours.

    RULES:
    - NEVER touch completed donations
    - ONLY cancel active NGO claims
    - Restore donation availability safely
    """
    try:
        now = datetime.utcnow()
        cutoff = now - timedelta(hours=24)

        print("🕒 Running auto-cancel for NGO claims older than 24h")

        # Fetch ACTIVE NGO claims older than 24h
        claims = (
            supabase.table("ngo_claims")
            .select("id, donation_id, claimed_at")
            .eq("status", "claimed")
            .lte("claimed_at", cutoff.isoformat())
            .execute()
        )

        if not claims.data:
            return jsonify({"message": "No expired NGO claims found"}), 200

        cancelled_count = 0

        for claim in claims.data:
            claim_id = claim["id"]
            donation_id = claim["donation_id"]

            # Fetch donation with STRICT guards
            donation_res = (
                supabase.table("food_donations")
                .select("status, donor_id, final_state")
                .eq("id", donation_id)
                .single()
                .execute()
            )

            if not donation_res.data:
                continue

            donation = donation_res.data
            status = donation.get("status")
            donor_id = donation.get("donor_id")

            # NEVER touch completed donations
            if status == "completed":
                print(f"⛔ Skipping completed donation {donation_id}")
                continue

            # Cancel NGO claim (HISTORY PRESERVED)
            supabase.table("ngo_claims").update({
                "status": "cancelled",
                "cancelled_at": now.isoformat(),
                "updated_at": now.isoformat(),
            }).eq("id", claim_id).execute()

            # Restore donation lifecycle safely
            supabase.table("food_donations").update({
                "status": "available",
                "final_state": None,        # ⬅️ NOT expired
                "updated_at": now.isoformat(),
            }).eq("id", donation_id).execute()

            # Notify donor (if exists)
            if donor_id:
                supabase.table("notifications").insert({
                    "user_id": donor_id,
                    "title": "Donation Claim Expired ⏰",
                    "message": (
                        "An NGO claimed your donation but did not pick it up "
                        "within 24 hours. The donation is now available again."
                    ),
                    "type": "status_update",
                    "read": False,
                    "created_at": now.isoformat(),
                }).execute()

            cancelled_count += 1

        print(f"✅ {cancelled_count} NGO claims auto-cancelled after 24h")

        log_audit(
            "claims_auto_cancelled",
            user_role="system",
            entity_type="claim",
            metadata={"cancelled_count": cancelled_count},
            req=request,
        )
        return jsonify({
            "message": f"{cancelled_count} NGO claims auto-cancelled after 24h"
        }), 200

    except Exception as e:
        print("⚠️ Auto-cancel error:", e)
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


# NGO manually cancels a claimed donation
# PUT /api/ngo/claims/<donation_id>/cancel
@donation_bp.route("/ngo/claims/<donation_id>/cancel", methods=["PUT"])
def ngo_cancel_claim(donation_id):
    try:
        data = request.get_json() or {}
        requested_ngo_id = data.get("ngo_id")
        payload, auth_error = _require_ngo_access(requested_ngo_id)
        if auth_error:
            return auth_error

        ngo_id = requested_ngo_id if payload.get("role") == "admin" else payload.get("user_id")

        now = datetime.utcnow().isoformat()

        # Fetch CLAIMED NGO claim (NOT active)
        claim_res = (
            supabase.table("ngo_claims")
            .select("id")
            .eq("donation_id", donation_id)
            .eq("ngo_id", ngo_id)
            .eq("status", "claimed") # matches DB constraint
            .maybe_single() # prevents PGRST116 crash
            .execute()
        )

        claim = claim_res.data

        if not claim:
            return jsonify({
                "error": "No claimed donation found to cancel"
            }), 404

        # Cancel NGO claim (history preserved)
        supabase.table("ngo_claims").update({
            "status": "cancelled",
            "cancelled_at": now,
            "updated_at": now,
        }).eq("id", claim["id"]).execute()

        # Restore donation availability
        supabase.table("food_donations").update({
            "status": "available",
            "final_state": "cancelled_by_ngo",
            "updated_at": now,
        }).eq("id", donation_id).execute()

        # Fetch donor for notification
        donation_res = (
            supabase.table("food_donations")
            .select("donor_id, title")
            .eq("id", donation_id)
            .maybe_single()
            .execute()
        )

        donation = donation_res.data

        if donation and donation.get("donor_id"):
            supabase.table("notifications").insert({
                "user_id": donation["donor_id"],
                "title": "NGO Cancelled Claim ❌",
                "message": (
                    f"An NGO cancelled their claim on your donation "
                    f"'{donation.get('title', 'your donation')}'. "
                    f"The donation is now available again."
                ),
                "type": "status_update",
                "read": False,
                "created_at": now,
            }).execute()

        log_audit(
            "donation_unclaimed",
            user_id=ngo_id,
            user_role="ngo",
            entity_type="donation",
            entity_id=donation_id,
            metadata={"reason": "ngo_cancel_claim"},
            req=request,
        )
        return jsonify({
            "message": "Claim cancelled successfully"
        }), 200

    except Exception as e:
        print("⚠️ NGO cancel claim error:", e)
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
