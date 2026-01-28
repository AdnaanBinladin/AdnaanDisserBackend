from flask import Blueprint, jsonify, request
from src.services.supabase_service import supabase
import traceback

notifications_bp = Blueprint("notifications", __name__)

# ─────────────────────────────────────────────
# 📬 Get all notifications for a specific user
# GET /api/notifications/<user_id>
# ─────────────────────────────────────────────
@notifications_bp.route("/notifications/<user_id>", methods=["GET"])
def get_notifications(user_id):
    """
    Fetch all notifications for a given user (sorted by newest first)
    """
    try:
        result = (
            supabase.table("notifications")
            .select("*")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .execute()
        )

        if hasattr(result, "error") and result.error:
            return jsonify({"error": result.error.message}), 500

        return jsonify({"data": result.data}), 200

    except Exception as e:
        print("⚠️ Notification fetch error:", e)
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


# ─────────────────────────────────────────────
# 🟢 Mark all notifications as read
# PUT /api/notifications/read/<user_id>
# ─────────────────────────────────────────────
@notifications_bp.route("/notifications/read/<user_id>", methods=["PUT"])
def mark_all_as_read(user_id):
    """
    Mark all notifications for a user as read
    """
    try:
        result = (
            supabase.table("notifications")
            .update({"read": True})
            .eq("user_id", user_id)
            .eq("read", False)
            .execute()
        )

        if hasattr(result, "error") and result.error:
            return jsonify({"error": result.error.message}), 500

        return jsonify({"message": "✅ All notifications marked as read"}), 200

    except Exception as e:
        print("⚠️ Error marking all as read:", e)
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


# ─────────────────────────────────────────────
# 🟡 Mark a single notification as read (optional)
# PUT /api/notifications/read_one/<notif_id>
# ─────────────────────────────────────────────
@notifications_bp.route("/notifications/read_one/<notif_id>", methods=["PUT"])
def mark_single_as_read(notif_id):
    """
    Mark a single notification as read (useful for granular UI updates)
    """
    try:
        result = (
            supabase.table("notifications")
            .update({"read": True})
            .eq("id", notif_id)
            .execute()
        )

        if hasattr(result, "error") and result.error:
            return jsonify({"error": result.error.message}), 500

        return jsonify({"message": "Notification marked as read"}), 200

    except Exception as e:
        print("⚠️ Error marking notification as read:", e)
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
