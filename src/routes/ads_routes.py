from flask import Blueprint, request, jsonify
from src.services.supabase_service import supabase
from datetime import datetime
from postgrest.exceptions import APIError
import logging
import traceback
from flask_mail import Message
from src.utils.mail_instance import mail

ads_bp = Blueprint("ads", __name__)
logger = logging.getLogger(__name__)


@ads_bp.route("/ads/test", methods=["GET"])
def test_ads_route():
    """Test route to verify blueprint is registered"""
    return jsonify({"message": "Ads blueprint is working!", "route": "/api/ads/test"}), 200


@ads_bp.route("/ads/inquiries", methods=["POST"])
def create_ad_inquiry():
    try:
        data = request.get_json(silent=True) or {}

        company_name = data.get("companyName")
        email = data.get("contactEmail")
        phone = data.get("phone")
        message = data.get("message")

        if not company_name or not email or not message:
            return jsonify({"error": "Company name, email and message are required"}), 400

        payload = {
            "company_name": company_name,
            "contact_email": email,
            "contact_phone": phone,
            "message": message,
            "status": "pending",
            "created_at": datetime.utcnow().isoformat(),
        }

        result = supabase.table("ads_inquiries").insert(payload).execute()

        if not result.data:
            return jsonify({"error": "Failed to submit inquiry"}), 500

        return jsonify({
            "message": "Ad inquiry submitted successfully",
            "inquiry": result.data[0],
        }), 201

    except APIError as e:
        logger.exception("Supabase API error")
        error_msg = str(e)
        if "relation" in error_msg.lower() and "does not exist" in error_msg.lower():
            return jsonify({"error": "Database table not found. Please contact support."}), 500
        return jsonify({"error": f"Database error: {error_msg}"}), 500

    except Exception as e:
        logger.exception("Create ad inquiry failed")
        return jsonify({"error": f"Server error: {str(e)}"}), 500


# ---------------------------
# Admin Inquiry Management
# ---------------------------
def _fetch_inquiries(status_filter: str = "all"):
    query = (
        supabase
        .table("ads_inquiries")
        .select("*")
        .order("created_at", desc=True)
    )
    if status_filter and status_filter != "all":
        query = query.eq("status", status_filter)
    return query.execute()


@ads_bp.route("/ads/inquiries", methods=["GET"])
def get_ad_inquiries():
    """List ad inquiries (optionally filtered by status)."""
    try:
        status = (request.args.get("status") or "all").strip().lower()
        result = _fetch_inquiries(status)
        return jsonify({"inquiries": result.data or []}), 200
    except Exception as e:
        logger.exception("Failed to fetch ad inquiries")
        return jsonify({"error": f"Server error: {str(e)}"}), 500


@ads_bp.route("/admin/ads/inquiries", methods=["GET"])
def get_admin_ad_inquiries():
    """Admin alias: list ad inquiries."""
    return get_ad_inquiries()


def _set_inquiry_status(inquiry_id: str, new_status: str):
    now_iso = datetime.utcnow().isoformat()

    inquiry_res = (
        supabase
        .table("ads_inquiries")
        .select("*")
        .eq("id", inquiry_id)
        .limit(1)
        .execute()
    )

    if not inquiry_res.data:
        return jsonify({"error": "Inquiry not found"}), 404

    inquiry = inquiry_res.data[0]
    if not inquiry.get("contact_email"):
        return jsonify({"error": "Inquiry has no contact_email; cannot send automated message"}), 400

    supabase.table("ads_inquiries").update({
        "status": new_status,
        "updated_at": now_iso,
    }).eq("id", inquiry_id).execute()

    payload = request.get_json(silent=True) or {}
    rejection_reason = payload.get("reason", "Your inquiry does not meet our current ad review criteria.")
    created_ad = None
    image_url = payload.get("image_url") or inquiry.get("image_url")
    redirect_url = payload.get("redirect_url") or inquiry.get("redirect_url")

    email_sent = False
    email_error = None

    # Send automated email based on decision
    try:
        if new_status == "rejected":
            msg = Message(
                subject="Update on Your FoodShare Advertising Inquiry",
                recipients=[inquiry.get("contact_email")],
            )
            msg.body = f"""
Hello {inquiry.get("company_name", "Partner")},

Thank you for your interest in advertising with FoodShare.

After review, your advertising inquiry was not approved at this time.
Reason: {rejection_reason}

You can submit a revised inquiry with additional details.

Best regards,
FoodShare Team
"""
            mail.send(msg)
            logger.info(f"Ad inquiry rejection email sent to {inquiry.get('contact_email')}")
            email_sent = True
    except Exception as email_err:
        logger.error(f"Failed to send rejection email for inquiry {inquiry_id}: {email_err}")
        logger.error(traceback.format_exc())
        email_error = str(email_err)

    if new_status == "approved":
        # Only publish ad if required assets are provided
        if image_url and redirect_url:
            ad_payload = {
                "company_name": inquiry.get("company_name"),
                "image_url": image_url,
                "redirect_url": redirect_url,
                "status": "approved",
                "is_active": True,
                "placement": payload.get("placement") or "login_page",
                "created_at": now_iso,
                "updated_at": now_iso,
            }

            ad_res = supabase.table("ads").insert(ad_payload).execute()
            if ad_res.data:
                created_ad = ad_res.data[0]

        try:
            msg = Message(
                subject="Your FoodShare Advertising Inquiry Was Approved",
                recipients=[inquiry.get("contact_email")],
            )

            if created_ad:
                msg.body = f"""
Hello {inquiry.get("company_name", "Partner")},

Great news! Your advertising inquiry has been approved and your ad is now active on FoodShare.

Ad redirect URL: {redirect_url}
Ad image URL: {image_url}

Thank you for advertising with FoodShare.

Best regards,
FoodShare Team
"""
            else:
                msg.body = f"""
Hello {inquiry.get("company_name", "Partner")},

Your advertising inquiry has been approved.

To activate your ad, please send us the following:
1) Ad image URL
2) Redirect URL (where users should go when they click your ad)

You can reply with both links and we will publish your ad immediately.

Best regards,
FoodShare Team
"""

            mail.send(msg)
            logger.info(f"Ad inquiry approval email sent to {inquiry.get('contact_email')}")
            email_sent = True
        except Exception as email_err:
            logger.error(f"Failed to send approval email for inquiry {inquiry_id}: {email_err}")
            logger.error(traceback.format_exc())
            email_error = str(email_err)

    if not email_sent:
        return jsonify({
            "error": "Inquiry status was updated but email could not be sent",
            "inquiry_id": inquiry_id,
            "status": new_status,
            "email_sent": email_sent,
            "email_error": email_error or "Unknown SMTP error",
            "requires_assets": new_status == "approved" and created_ad is None,
            "ad": created_ad,
        }), 502

    return jsonify({
        "message": f"Inquiry {new_status}",
        "inquiry_id": inquiry_id,
        "status": new_status,
        "requires_assets": new_status == "approved" and created_ad is None,
        "email_sent": email_sent,
        "email_error": email_error,
        "ad": created_ad,
    }), 200


@ads_bp.route("/admin/ads/inquiries/<inquiry_id>/approve", methods=["PATCH", "PUT"])
def approve_ad_inquiry(inquiry_id):
    try:
        return _set_inquiry_status(inquiry_id, "approved")
    except Exception as e:
        logger.exception("Failed to approve ad inquiry")
        return jsonify({"error": f"Server error: {str(e)}"}), 500


@ads_bp.route("/admin/ads/inquiries/<inquiry_id>/reject", methods=["PATCH", "PUT"])
def reject_ad_inquiry(inquiry_id):
    try:
        return _set_inquiry_status(inquiry_id, "rejected")
    except Exception as e:
        logger.exception("Failed to reject ad inquiry")
        return jsonify({"error": f"Server error: {str(e)}"}), 500


@ads_bp.route("/ads/inquiries/<inquiry_id>/approve", methods=["PATCH", "PUT"])
def approve_ad_inquiry_alias(inquiry_id):
    return approve_ad_inquiry(inquiry_id)


@ads_bp.route("/ads/inquiries/<inquiry_id>/reject", methods=["PATCH", "PUT"])
def reject_ad_inquiry_alias(inquiry_id):
    return reject_ad_inquiry(inquiry_id)


@ads_bp.route("/ads/active", methods=["GET"])
def get_active_ads():
    placement = request.args.get("placement")

    query = (
        supabase
        .table("ads")
        .select("*")
        .eq("is_active", True)
    )

    if placement:
        query = query.eq("placement", placement)

    result = query.execute()

    return jsonify({"ads": result.data or []}), 200