print("🔥 ads_routes.py LOADED")

from flask import Blueprint, request, jsonify
from src.services.supabase_service import supabase
from datetime import datetime
from postgrest.exceptions import APIError
import logging
from werkzeug.exceptions import HTTPException

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
        print(f"🔍 Received data: {data}")

        company_name = data.get("companyName")
        email = data.get("contactEmail")
        phone = data.get("phone")
        message = data.get("message")

        if not company_name or not email or not message:
            return jsonify({
                "error": "Company name, email and message are required"
            }), 400

        payload = {
            "company_name": company_name,
            "contact_email": email,
            "contact_phone": phone,
            "message": message,
            "status": "pending",
            "created_at": datetime.utcnow().isoformat(),
        }

        print(f"📦 Inserting payload: {payload}")

        result = (
            supabase
            .table("ads_inquiries")
            .insert(payload)
            .execute()
        )

        print(f"✅ Insert result: {result}")

        if not result.data:
            print("❌ Insert returned no data")
            return jsonify({"error": "Failed to submit inquiry"}), 500

        print(f"✅ Ad inquiry created: {result.data[0].get('id')}")
        return jsonify({
            "message": "Ad inquiry submitted successfully"
        }), 201

    except APIError as e:
        print(f"🔥 Supabase APIError: {type(e).__name__}: {e}")
        print(f"🔥 Error details: {e.message if hasattr(e, 'message') else str(e)}")
        logger.exception(f"🔥 Supabase API error: {e}")
        error_msg = str(e)
        # Check if table doesn't exist
        if "relation" in error_msg.lower() and "does not exist" in error_msg.lower():
            return jsonify({
                "error": "Database table not found. Please contact support."
            }), 500
        return jsonify({"error": f"Database error: {error_msg}"}), 500

    except Exception as e:
        print(f"🔥 General Exception: {type(e).__name__}: {e}")
        import traceback
        print(f"🔥 Traceback:\n{traceback.format_exc()}")
        logger.exception("🔥 Create ad inquiry failed")
        return jsonify({"error": f"Server error: {str(e)}"}), 500


# 🔴 THIS MUST BE TOP-LEVEL (NOT INDENTED)
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

    return jsonify({
        "ads": result.data or []
    }), 200