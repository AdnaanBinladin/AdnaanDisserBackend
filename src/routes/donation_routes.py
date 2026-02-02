from flask import Blueprint, request, jsonify
from src.services.supabase_service import supabase
from flask_mail import Message
from src.utils.mail_instance import mail
import datetime
import traceback
import qrcode
import io
import base64

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
            expiry_date = datetime.date.fromisoformat(data["expiry_date"])
            today = datetime.date.today()
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
            "created_at": datetime.datetime.utcnow().isoformat(),
            "updated_at": datetime.datetime.utcnow().isoformat(),
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
            "created_at": datetime.datetime.utcnow().isoformat()
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

        allowed_fields = [
            "title", "description", "category", "quantity", "unit",
            "expiry_date", "pickup_address", "pickup_lat", "pickup_lng",
            "pickup_instructions", "urgency"
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
                        return jsonify({"error": "Quantity must be greater than zero"}), 400
                except (ValueError, TypeError):
                    return jsonify({"error": "Invalid quantity"}), 400

            # 📅 Expiry date validation
            if field == "expiry_date":
                try:
                    expiry_date = datetime.date.fromisoformat(value)
                    if expiry_date <= datetime.date.today():
                        return jsonify({"error": "Expiry date must be in the future"}), 400
                    value = expiry_date.isoformat()
                except Exception:
                    return jsonify({"error": "Invalid expiry date format"}), 400

            # 🌍 Coordinates validation
            if field in ["pickup_lat", "pickup_lng"] and value is not None:
                try:
                    value = float(value)
                except (ValueError, TypeError):
                    return jsonify({"error": f"Invalid {field}"}), 400

            update_data[field] = value

        if not update_data:
            return jsonify({"error": "No valid fields to update"}), 400

        update_data["updated_at"] = datetime.datetime.utcnow().isoformat()

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
        # ✅ Fetch existing donation
        existing = (
            supabase.table("food_donations")
            .select("*")
            .eq("id", donation_id)
            .single()
            .execute()
        )

        if not existing.data:
            return jsonify({"error": "Donation not found"}), 404

        # 🚫 Prevent cancelling completed donations
        if existing.data["status"] == "completed":
            return jsonify({
                "error": "Completed donations cannot be cancelled"
            }), 400

        # ✅ Update donation status to "cancelled"
        supabase.table("food_donations").update({
            "status": "cancelled",
            "updated_at": datetime.datetime.utcnow().isoformat(),
        }).eq("id", donation_id).execute()

        # ✅ Create in-app notification
        supabase.table("notifications").insert({
            "user_id": existing.data["donor_id"],
            "title": "Donation Cancelled ❌",
            "message": f"Your donation '{existing.data['title']}' has been cancelled.",
            "type": "status_update",
            "read": False,
            "created_at": datetime.datetime.utcnow().isoformat(),
        }).execute()

        # ✅ Send cancellation email
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
                        f"Your donation titled '{existing.data['title']}' has been successfully cancelled.\n\n"
                        f"If this was a mistake, you can post it again anytime from your dashboard.\n\n"
                        f"Warm regards,\nThe FoodShare Team 🌱"
                    )
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
    Marks all donations whose expiry_date <= today and status == 'available'
    as 'expired'. Also sends notifications and emails.
    """
    try:
        today = datetime.datetime.utcnow().date().isoformat()
        print("🕒 Running auto-expire check. Today:", today)

        # ✅ Fetch donations that should expire
        to_expire = (
            supabase.table("food_donations")
            .select("id, donor_id, title, expiry_date, status")
            .lte("expiry_date", today)
            .eq("status", "available")
            .execute()
        )

        if not to_expire or not to_expire.data:
            print("✅ No donations to expire today.")
            return jsonify({"message": "No donations to expire today"}), 200

        expired_count = 0

        for donation in to_expire.data:
            donation_id = donation["id"]
            donor_id = donation["donor_id"]
            title = donation["title"]

            # ✅ Update donation status
            supabase.table("food_donations")\
                .update({"status": "expired", "updated_at": datetime.datetime.utcnow().isoformat()})\
                .eq("id", donation_id)\
                .execute()
            expired_count += 1

            # ✅ Create in-app notification
            supabase.table("notifications").insert({
                "user_id": donor_id,
                "title": "Donation Expired ⚠️",
                "message": f"Your donation '{title}' has reached its expiry date and is now marked as expired.",
                "type": "status_update",
                "read": False,
                "created_at": datetime.datetime.utcnow().isoformat()
            }).execute()

            # ✅ Send expiry email (optional)
            try:
                donor = supabase.table("users").select("email, full_name").eq("id", donor_id).single().execute()
                if donor.data and donor.data.get("email"):
                    donor_email = donor.data["email"]
                    donor_name = donor.data.get("full_name", "Donor")

                    msg = Message(
                        subject="⚠️ Donation Expired - FoodShare",
                        recipients=[donor_email],
                        body=f"Hi {donor_name},\n\n"
                             f"Your donation titled '{title}' has now expired and is no longer visible to NGOs.\n\n"
                             f"Thank you again for supporting FoodShare.\n\n"
                             f"Warm regards,\nThe FoodShare Team 🌱"
                    )
                    mail.send(msg)
                    print(f"📩 Expiry email sent to {donor_email}")
            except Exception as email_err:
                print(f"⚠️ Email sending failed for donation {donation_id}:", email_err)

        print(f"✅ {expired_count} donations marked as expired.")
        return jsonify({"message": f"{expired_count} donations marked as expired"}), 200

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
    - Creates in-app notifications
    - Sends reminder emails
    """
    try:
        now = datetime.datetime.utcnow()
        tomorrow = now + datetime.timedelta(days=1)
        today_str = now.date().isoformat()
        tomorrow_str = tomorrow.date().isoformat()

        print(f"🕒 Checking for donations expiring between {today_str} and {tomorrow_str}")

        # ✅ Fetch donations expiring within 24 hours and still available
        expiring = (
            supabase.table("food_donations")
            .select("id, donor_id, title, expiry_date, status")
            .eq("status", "available")
            .gte("expiry_date", today_str)
            .lte("expiry_date", tomorrow_str)
            .execute()
        )

        if not expiring.data:
            print("✅ No expiring donations found.")
            return jsonify({"message": "No donations expiring soon."}), 200

        count = 0
        for donation in expiring.data:
            donor_id = donation["donor_id"]
            title = donation["title"]

            # ✅ Add in-app notification
            supabase.table("notifications").insert({
                "user_id": donor_id,
                "title": "⏰ Donation Expiring Soon",
                "message": f"Your donation '{title}' will expire soon. Please ensure pickup or extend its date.",
                "type": "reminder",
                "read": False,
                "created_at": datetime.datetime.utcnow().isoformat()
            }).execute()

            # ✅ Send email reminder
            try:
                donor = supabase.table("users").select("email, full_name").eq("id", donor_id).single().execute()
                if donor.data and donor.data.get("email"):
                    donor_email = donor.data["email"]
                    donor_name = donor.data.get("full_name", "Donor")

                    msg = Message(
                        subject="⏰ Donation Expiring Soon - FoodShare",
                        recipients=[donor_email],
                        body=(
                            f"Hi {donor_name},\n\n"
                            f"This is a friendly reminder that your donation titled '{title}' will expire within 24 hours.\n\n"
                            f"If it hasn’t been picked up yet, please coordinate.\n\n"
                            f"Thank you for helping reduce food waste!\n\n"
                            f"Warm regards,\nThe FoodShare Team 🌱"
                        )
                    )
                    mail.send(msg)
                    print(f"📩 Reminder email sent to {donor_email}")
            except Exception as email_err:
                print(f"⚠️ Email sending failed for donation '{title}':", email_err)

            count += 1

        print(f"✅ {count} reminder notifications sent successfully.")
        return jsonify({"message": f"{count} reminder notifications sent."}), 200

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
        # 🔍 Fetch donation first
        existing = (
            supabase.table("food_donations")
            .select("status, title")
            .eq("id", donation_id)
            .single()
            .execute()
        )

        if not existing.data:
            return (
                "<h1>❌ Donation not found</h1><p>This QR code may be invalid.</p>",
                404,
                {"Content-Type": "text/html"},
            )

        # 🚫 Prevent double scan
        if existing.data["status"] == "completed":
            return (
                "<h1>⚠️ Already Confirmed</h1>"
                "<p>This donation was already marked as picked up.</p>",
                200,
                {"Content-Type": "text/html"},
            )

        # ✅ Mark as completed
        supabase.table("food_donations").update({
            "status": "completed",
            "updated_at": datetime.datetime.utcnow().isoformat()
        }).eq("id", donation_id).execute()

        donation_title = existing.data.get("title", "Unknown Donation")

        html = f"""
        <html>
        <body style='font-family: Arial; text-align:center; margin-top: 100px;'>
            <h1>✅ Pickup Confirmed!</h1>
            <p>The donation <b>{donation_title}</b> has been successfully marked as picked up.</p>
            <p>Thank you for supporting FoodShare 🌱</p>
        </body>
        </html>
        """

        return html, 200, {"Content-Type": "text/html"}

    except Exception as e:
        print("⚠️ Pickup confirmation error:", e)
        return f"<h1>Error</h1><p>{e}</p>", 500, {"Content-Type": "text/html"}
 


# ─────────────────────────────────────────────
# ⏰ Auto-cancel claimed donations after 24h
# PUT /api/donations/auto-cancel-claims
# ─────────────────────────────────────────────
@donation_bp.route("/donations/auto-cancel-claims", methods=["PUT"])
def auto_cancel_claimed_donations():
    try:
        now = datetime.datetime.utcnow()
        cutoff = now - datetime.timedelta(minutes=1)

        print("🕒 Running auto-cancel for claimed donations older than 24h")

        claimed = (
            supabase.table("food_donations")
            .select("id, title, donor_id, claimed_date, status")
            .eq("status", "claimed")
            .execute()
        )

        if not claimed.data:
            return jsonify({"message": "No claimed donations found"}), 200

        cancelled_count = 0

        for d in claimed.data:
            claimed_date = d.get("claimed_date")
            if not claimed_date:
                continue

            claimed_time = datetime.datetime.fromisoformat(claimed_date)

            if claimed_time <= cutoff:
                # 🔁 Make donation AVAILABLE again
                supabase.table("food_donations").update({
                    "status": "available",
                    "claimed_by": None,
                    "claimed_date": None,
                    "updated_at": now.isoformat(),
                }).eq("id", d["id"]).execute()

                # 🔔 Notify donor
                supabase.table("notifications").insert({
                    "user_id": d["donor_id"],
                    "title": "Donation Claim Auto-Cancelled ⏰",
                    "message": f"The donation '{d['title']}' was not picked up within 24 hours and is available again.",
                    "type": "status_update",
                    "read": False,
                    "created_at": now.isoformat(),
                }).execute()

                cancelled_count += 1

        return jsonify({
            "message": f"{cancelled_count} claimed donations auto-cancelled"
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

        donation = (
            supabase.table("food_donations")
            .select("*")
            .eq("id", donation_id)
            .single()
            .execute()
        )

        if not donation.data:
            return jsonify({"error": "Donation not found"}), 404

        if donation.data["status"] != "claimed":
            return jsonify({"error": "Only claimed donations can be cancelled"}), 400

        if donation.data.get("claimed_by") != ngo_id:
            return jsonify({"error": "Unauthorized"}), 403

        # 🔁 Make donation available again
        supabase.table("food_donations").update({
            "status": "available",
            "claimed_by": None,
            "claimed_date": None,
            "updated_at": datetime.datetime.utcnow().isoformat(),
        }).eq("id", donation_id).execute()

        # 🔔 Notify donor
        supabase.table("notifications").insert({
            "user_id": donation.data["donor_id"],
            "title": "Donation Claim Cancelled ❌",
            "message": f"The NGO cancelled their claim on '{donation.data['title']}'. The donation is available again.",
            "type": "status_update",
            "read": False,
            "created_at": datetime.datetime.utcnow().isoformat(),
        }).execute()

        return jsonify({"message": "Claim cancelled successfully"}), 200

    except Exception as e:
        print("⚠️ NGO cancel error:", e)
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
