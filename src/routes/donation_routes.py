from flask import Blueprint, request, jsonify
from src.services.supabase_service import supabase
from flask_mail import Message
from src.utils.mail_instance import mail
import traceback
import qrcode
import io
import base64
from datetime import date, datetime, timedelta





donation_bp = Blueprint("donation", __name__)




# ─────────────────────────────────────────────
# ➕ Add a new donation (with QR code)
# POST /api/donations/add
# ─────────────────────────────────────────────
@donation_bp.route("/donations/add", methods=["POST"])
def add_donation():
    try:
        data = request.get_json() or {}

        # ✅ Required fields
        required_fields = [
            "donor_id",
            "title",
            "category",
            "quantity",
            "unit",
            "expiry_date",
            "pickup_address",
        ]
        missing = [f for f in required_fields if f not in data or not data[f]]
        if missing:
            return jsonify({"error": f"Missing required fields: {', '.join(missing)}"}), 400

        # ✅ Validate quantity (must be positive integer)
        try:
            quantity = int(data["quantity"])
            if quantity <= 0:
                return jsonify({"error": "Quantity must be greater than zero"}), 400
        except (ValueError, TypeError):
            return jsonify({"error": "Invalid quantity value"}), 400

        # ✅ Validate expiry date (must be in the future)
        try:
            expiry_date = date.fromisoformat(data["expiry_date"])

            today = date.today()
            if expiry_date <= today:
                return jsonify({"error": "Expiry date must be a future date"}), 400
        except (ValueError, TypeError):
            return jsonify({"error": "Invalid expiry date format (YYYY-MM-DD required)"}), 400

        # ✅ Optional coordinates
        pickup_lat = data.get("pickup_lat")
        pickup_lng = data.get("pickup_lng")

        if pickup_lat is not None and pickup_lng is not None:
            try:
                pickup_lat = float(pickup_lat)
                pickup_lng = float(pickup_lng)

                # ✅ Coordinate range validation
                if not (-90 <= pickup_lat <= 90 and -180 <= pickup_lng <= 180):
                    return jsonify({"error": "Latitude or longitude out of range"}), 400
            except (ValueError, TypeError):
                return jsonify({"error": "Invalid latitude or longitude format"}), 400

        # ✅ Step 1: Insert new donation
        donation = {
            "donor_id": data["donor_id"],
            "title": data["title"],
            "description": data.get("description"),
            "category": data["category"],
            "quantity": quantity,
            "unit": data["unit"],
            "expiry_date": expiry_date.isoformat(),
            "pickup_address": data["pickup_address"],
            "pickup_lat": pickup_lat,
            "pickup_lng": pickup_lng,
            "pickup_instructions": data.get("pickup_instructions"),
            "status": "available",
            "urgency": data.get("urgency") or "medium",
            "created_at": datetime.utcnow().isoformat(),
            "updated_at": datetime.utcnow().isoformat(),
        }

        result = supabase.table("food_donations").insert(donation).execute()
        if not result.data:
            return jsonify({"error": "Failed to insert donation"}), 500

        donation_id = result.data[0]["id"]

        # ✅ Step 2: Generate QR code
        pickup_url = f"http://192.168.56.1:5050/api/donations/{donation_id}/pickup-confirm"
        qr_img = qrcode.make(pickup_url)
        buf = io.BytesIO()
        qr_img.save(buf, format="PNG")
        qr_base64 = base64.b64encode(buf.getvalue()).decode("utf-8")

        # ✅ Step 3: Save QR to database
        supabase.table("food_donations").update(
            {"qr_code": qr_base64}
        ).eq("id", donation_id).execute()

        # ✅ Step 4: In-app notification
        supabase.table("notifications").insert({
            "user_id": data["donor_id"],
            "title": "Donation Created ✅",
            "message": f"Your donation '{data['title']}' has been created with a pickup QR code for NGO verification.",
            "type": "status_update",
            "read": False,
            "created_at": datetime.utcnow().isoformat()
        }).execute()

        # ✅ Step 5: Email with QR code attachment
        try:
            donor = (
                supabase.table("users")
                .select("email, full_name")
                .eq("id", data["donor_id"])
                .single()
                .execute()
            )
            if donor.data and donor.data.get("email"):
                msg = Message(
                    subject="🎁 Donation Created - QR Code Ready",
                    recipients=[donor.data["email"]],
                    body=(
                        f"Hi {donor.data.get('full_name', 'Donor')},\n\n"
                        f"Thank you for your donation '{data['title']}'!\n\n"
                        f"Attached is your QR code for NGO pickup verification.\n\n"
                        f"Warm regards,\nFoodShare Team 🌱"
                    ),
                )
                msg.attach("pickup_qr.png", "image/png", buf.getvalue())
                mail.send(msg)
        except Exception as email_err:
            print("⚠️ Email sending failed:", email_err)

        return jsonify({
            "message": "Donation added successfully with QR code!",
            "qr_code": qr_base64,
            "data": result.data
        }), 201

    except Exception as e:
        print("⚠️ Add donation error:", e)
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


#edit
@donation_bp.route("/donations/<donation_id>", methods=["PUT"])
def update_donation(donation_id):
    try:
        data = request.get_json() or {}

        # ─────────────────────────────────────────
        # 🔒 Fetch existing donation (GUARD)
        # ─────────────────────────────────────────
        existing = (
            supabase.table("food_donations")
            .select("status, final_state")
            .eq("id", donation_id)
            .single()
            .execute()
        )

        if not existing.data:
            return jsonify({"error": "Donation not found"}), 404

        # ❌ Block edits if donation is finalized
        if existing.data.get("final_state") is not None:
            return jsonify({
                "error": "This donation is no longer editable"
            }), 400

        # ❌ Block edits if already completed
        if existing.data.get("status") == "completed":
            return jsonify({
                "error": "Completed donations cannot be edited"
            }), 400

        # ─────────────────────────────────────────
        # ✅ Allowed editable fields
        # ─────────────────────────────────────────
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

            # 🔢 Quantity validation
            if field == "quantity":
                try:
                    value = int(value)
                    if value <= 0:
                        return jsonify({
                            "error": "Quantity must be greater than zero"
                        }), 400
                except (ValueError, TypeError):
                    return jsonify({"error": "Invalid quantity"}), 400

            # 📅 Expiry date validation
            if field == "expiry_date":
                try:
                    expiry_date = date.fromisoformat(value)
                    if expiry_date <= date.today():
                        return jsonify({
                            "error": "Expiry date must be in the future"
                        }), 400
                    value = expiry_date.isoformat()
                except Exception:
                    return jsonify({
                        "error": "Invalid expiry date format (YYYY-MM-DD required)"
                    }), 400

            # 🌍 Coordinates validation
            if field in ["pickup_lat", "pickup_lng"] and value is not None:
                try:
                    value = float(value)
                except (ValueError, TypeError):
                    return jsonify({
                        "error": f"Invalid {field}"
                    }), 400

            update_data[field] = value

        if not update_data:
            return jsonify({
                "error": "No valid fields to update"
            }), 400

        update_data["updated_at"] = datetime.datetime.utcnow().isoformat()

        # ─────────────────────────────────────────
        # ✅ Perform update
        # ─────────────────────────────────────────
        result = (
            supabase.table("food_donations")
            .update(update_data)
            .eq("id", donation_id)
            .execute()
        )

        if not result.data:
            return jsonify({"error": "Donation not found"}), 404

        return jsonify({"data": result.data}), 200

    except Exception as e:
        print("⚠️ Update donation error:", e)
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


# ─────────────────────────────────────────────
# 📦 List all donations for a specific donor
# GET /api/donations/list/<donor_id>
# ─────────────────────────────────────────────
@donation_bp.route("/donations/list/<donor_id>", methods=["GET"])
def list_donations(donor_id):
    try:
        # ✅ make sure qr_code column is fetched
        response = (
            supabase.table("food_donations")
            .select("*")  # fetch all columns including qr_code
            .eq("donor_id", donor_id)
            .order("created_at", desc=True)
            .execute()
        )

        data = response.data or []

        # debugging info
        print(f"✅ fetched {len(data)} donations for donor {donor_id}")
        for d in data:
            print(
                f"🧾 {d.get('title', 'Untitled')} - QR present: {'✅' if d.get('qr_code') else '❌'}"
            )

        return jsonify({"data": data}), 200

    except Exception as e:
        print("⚠️ list_donations error:", e)
        import traceback

        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


# ─────────────────────────────────────────────
# ❌ Cancel a donation
# ─────────────────────────────────────────────
@donation_bp.route("/donations/<donation_id>/cancel", methods=["PUT"])
def cancel_donation(donation_id):
    try:
        now = datetime.utcnow().isoformat()


        # ─────────────────────────────────────────
        # 🔍 Fetch existing donation (GUARD)
        # ─────────────────────────────────────────
        existing = (
            supabase.table("food_donations")
            .select("id, donor_id, title, status, final_state")
            .eq("id", donation_id)
            .single()
            .execute()
        )

        if not existing.data:
            return jsonify({"error": "Donation not found"}), 404

        # ❌ Completed donations are immutable
        if existing.data.get("status") == "completed":
            return jsonify({
                "error": "Completed donations cannot be cancelled"
            }), 400

        # ❌ Block donor double-cancel only
        if existing.data.get("final_state") == "cancelled_by_donor":
            return jsonify({
                "error": "This donation was already cancelled by the donor"
            }), 400

        # ─────────────────────────────────────────
        # 🧹 Cancel any ACTIVE NGO claim (history preserved)
        # ─────────────────────────────────────────
        supabase.table("ngo_claims").update({
            "status": "cancelled",
            "cancelled_at": now
        }).eq("donation_id", donation_id).eq("status", "active").execute()

        # ─────────────────────────────────────────
        # ✅ Apply DONOR cancellation (FINAL)
        # ─────────────────────────────────────────
        supabase.table("food_donations").update({
            "status": "available",                # safe default
            "final_state": "cancelled_by_donor",  # donor owns this
            "updated_at": now,
        }).eq("id", donation_id).execute()

        # ─────────────────────────────────────────
        # 🔔 In-app notification
        # ─────────────────────────────────────────
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

        # ─────────────────────────────────────────
        # 📧 Email notification (best effort)
        # ─────────────────────────────────────────
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

        return jsonify({
            "message": "Donation cancelled successfully"
        }), 200

    except Exception as e:
        print("⚠️ Cancel donation error:", e)
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

# ─────────────────────────────────────────────
# ⏰ Automatically mark expired donations
# PUT /api/donations/auto-expire
# ─────────────────────────────────────────────
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

        # ─────────────────────────────────────────
        # 🔍 Fetch ONLY unfinalized donations past expiry
        # ─────────────────────────────────────────
        to_expire = (
            supabase.table("food_donations")
            .select("id, donor_id, title, expiry_date, final_state")
            .lte("expiry_date", today)
            .is_("final_state", None)  # ⛔ excludes donor-cancelled & already expired
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

            # ─────────────────────────────────────────
            # ✅ Mark donation as expired (FINAL STATE)
            # ─────────────────────────────────────────
            supabase.table("food_donations").update({
                "final_state": "expired",
                "updated_at": now,
            }).eq("id", donation_id).execute()

            expired_count += 1

            # ─────────────────────────────────────────
            # 🔔 In-app notification
            # ─────────────────────────────────────────
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

            # ─────────────────────────────────────────
            # 📧 Email notification (best effort)
            # ─────────────────────────────────────────
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
        return jsonify({
            "message": f"{expired_count} donations marked as expired"
        }), 200

    except Exception as e:
        print("⚠️ Auto-expire error:", e)
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

# ─────────────────────────────────────────────
# 🕒 Send Reminder Notifications for Expiring Donations
# ─────────────────────────────────────────────
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

        # ─────────────────────────────────────────
        # 🔍 Fetch ALL donations expiring within 24h
        # (regardless of status / final_state)
        # ─────────────────────────────────────────
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

            # ─────────────────────────────────────────
            # 🏷️ Context-aware message
            # ─────────────────────────────────────────
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

            # ─────────────────────────────────────────
            # 🔔 In-app notification
            # ─────────────────────────────────────────
            supabase.table("notifications").insert({
                "user_id": donor_id,
                "title": "⏰ Donation Expiry Reminder",
                "message": message,
                "type": "reminder",
                "read": False,
                "created_at": created_at,
            }).execute()

            # ─────────────────────────────────────────
            # 📧 Email reminder (best effort)
            # ─────────────────────────────────────────
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
        return jsonify({
            "message": f"{count} reminder notifications sent."
        }), 200

    except Exception as e:
        print("⚠️ Reminder job error:", e)
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500




# ─────────────────────────────────────────────
# ✅ Pickup confirmation page (NGO QR scan)
# ─────────────────────────────────────────────
@donation_bp.route("/donations/<donation_id>/pickup-confirm", methods=["GET"])
def confirm_pickup(donation_id):
    try:
        # ─────────────────────────────────────────
        # 🔍 Fetch donation (STRICT GUARDS)
        # ─────────────────────────────────────────
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

        # ─────────────────────────────────────────
        # ❌ Block finalized donations
        # ─────────────────────────────────────────
        if final_state is not None:
            return (
                "<h1>❌ Donation No Longer Active</h1>"
                "<p>This donation has been cancelled or expired.</p>",
                400,
                {"Content-Type": "text/html"},
            )

        # ─────────────────────────────────────────
        # ⚠️ Prevent double pickup
        # ─────────────────────────────────────────
        if status == "completed":
            return (
                "<h1>⚠️ Pickup Already Confirmed</h1>"
                "<p>This donation was already marked as picked up.</p>",
                200,
                {"Content-Type": "text/html"},
            )

        # ─────────────────────────────────────────
        # ❌ Only CLAIMED donations can be picked up
        # ─────────────────────────────────────────
        if status != "claimed":
            return (
                "<h1>❌ Pickup Not Allowed</h1>"
                "<p>This donation has not been claimed by an NGO.</p>",
                400,
                {"Content-Type": "text/html"},
            )

        now = datetime.utcnow().isoformat()


        # ─────────────────────────────────────────
        # ✅ Mark donation as COMPLETED
        # ─────────────────────────────────────────
        supabase.table("food_donations").update({
            "status": "completed",
            "updated_at": now,
        }).eq("id", donation_id).execute()

        # ─────────────────────────────────────────
        # 🔔 Notify donor
        # ─────────────────────────────────────────
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

        # ─────────────────────────────────────────
        # 📧 Email notification (best effort)
        # ─────────────────────────────────────────
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

        # ─────────────────────────────────────────
        # ✅ Success HTML response
        # ─────────────────────────────────────────
        html = f"""
        <html>
        <body style="font-family: Arial; text-align:center; margin-top: 100px;">
            <h1>✅ Pickup Confirmed!</h1>
            <p>The donation <b>{donation_title}</b> has been successfully marked as picked up.</p>
            <p>Thank you for supporting FoodShare 🌱</p>
        </body>
        </html>
        """

        return html, 200, {"Content-Type": "text/html"}

    except Exception as e:
        print("⚠️ Pickup confirmation error:", e)
        traceback.print_exc()
        return (
            "<h1>❌ Error</h1>"
            "<p>Something went wrong while confirming pickup.</p>",
            500,
            {"Content-Type": "text/html"},
        )



# ─────────────────────────────────────────────
# ⏰ Auto-cancel claimed donations after 24h
# PUT /api/donations/auto-cancel-claims
# ─────────────────────────────────────────────
@donation_bp.route("/donations/auto-cancel-claims", methods=["PUT"])
def auto_cancel_claimed_donations():
    """
    Auto-cancel NGO claims that were not picked up within 24 hours.
    """
    try:
        now = datetime.datetime.utcnow()
        cutoff = now - datetime.timedelta(hours=24)

        print("🕒 Running auto-cancel for NGO claims older than 24h")

        # 🔍 Fetch ACTIVE NGO claims older than 24h
        claims = (
            supabase.table("ngo_claims")
            .select("id, donation_id, claimed_at")
            .eq("status", "active")
            .lte("claimed_at", cutoff.isoformat())
            .execute()
        )

        if not claims.data:
            return jsonify({"message": "No expired NGO claims found"}), 200

        cancelled_count = 0

        for claim in claims.data:
            claim_id = claim["id"]
            donation_id = claim["donation_id"]

            # 🔍 Fetch donor from donation (SOURCE OF TRUTH)
            donation = (
                supabase.table("food_donations")
                .select("donor_id")
                .eq("id", donation_id)
                .single()
                .execute()
            )

            donor_id = donation.data["donor_id"] if donation.data else None

            # 1️⃣ Cancel NGO claim
            supabase.table("ngo_claims").update({
                "status": "cancelled",
                "cancelled_at": now.isoformat(),
            }).eq("id", claim_id).execute()

            # 2️⃣ Update donation lifecycle
            supabase.table("food_donations").update({
                "status": "available",
                "final_state": "expired",
                "updated_at": now.isoformat(),
            }).eq("id", donation_id).execute()

            # 3️⃣ Notify donor (if exists)
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

        return jsonify({
            "message": f"{cancelled_count} NGO claims auto-cancelled after 24h"
        }), 200

    except Exception as e:
        print("⚠️ Auto-cancel error:", e)
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500



# ─────────────────────────────────────────────
# ❌ NGO manually cancels a claimed donation
# PUT /api/ngo/claims/<donation_id>/cancel
# ─────────────────────────────────────────────
@donation_bp.route("/ngo/claims/<donation_id>/cancel", methods=["PUT"])
def ngo_cancel_claim(donation_id):
    try:
        data = request.get_json() or {}
        ngo_id = data.get("ngo_id")

        if not ngo_id:
            return jsonify({"error": "NGO ID required"}), 400

        now = datetime.utcnow().isoformat()

        # ─────────────────────────────────────────
        # 1️⃣ Fetch CLAIMED NGO claim (NOT active)
        # ─────────────────────────────────────────
        claim_res = (
            supabase.table("ngo_claims")
            .select("id")
            .eq("donation_id", donation_id)
            .eq("ngo_id", ngo_id)
            .eq("status", "claimed")   # ✅ matches DB constraint
            .maybe_single()            # ✅ prevents PGRST116 crash
            .execute()
        )

        claim = claim_res.data

        if not claim:
            return jsonify({
                "error": "No claimed donation found to cancel"
            }), 404

        # ─────────────────────────────────────────
        # 2️⃣ Cancel NGO claim (history preserved)
        # ─────────────────────────────────────────
        supabase.table("ngo_claims").update({
            "status": "cancelled",
            "cancelled_at": now,
            "updated_at": now,
        }).eq("id", claim["id"]).execute()

        # ─────────────────────────────────────────
        # 3️⃣ Restore donation availability
        # ─────────────────────────────────────────
        supabase.table("food_donations").update({
            "status": "available",
            "final_state": "cancelled_by_ngo",
            "updated_at": now,
        }).eq("id", donation_id).execute()

        # ─────────────────────────────────────────
        # 4️⃣ Fetch donor for notification
        # ─────────────────────────────────────────
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

        return jsonify({
            "message": "Claim cancelled successfully"
        }), 200

    except Exception as e:
        print("⚠️ NGO cancel claim error:", e)
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
