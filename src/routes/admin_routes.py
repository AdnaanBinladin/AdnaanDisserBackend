"""
Admin Routes for FoodShare Platform
Handles admin-specific operations like NGO verification, user management, and system reports.
"""

from flask import Blueprint, request, jsonify
from werkzeug.security import generate_password_hash
from src.services.supabase_service import supabase
from src.utils.jwt import decode_jwt
from flask_mail import Message
from src.utils.mail_instance import mail
import traceback
import logging
from datetime import datetime, timedelta, date
import csv
import io

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger(__name__)

admin_bp = Blueprint("admin", __name__)


# ────────────────────────────────────────────────────────────────
# 🔐 ADMIN AUTH MIDDLEWARE
# ────────────────────────────────────────────────────────────────
def require_admin(f):
    """Decorator to protect admin-only routes"""
    from functools import wraps

    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get("Authorization")
        if not auth_header:
            return jsonify({"error": "Missing authorization token"}), 401

        try:
            token = auth_header.split(" ")[1]
            payload = decode_jwt(token)

            if not payload:
                return jsonify({"error": "Invalid or expired token"}), 401

            if payload.get("role") != "admin":
                return jsonify({"error": "Admin access required"}), 403

            # Attach admin info to request context
            request.admin_id = payload.get("user_id")
            request.admin_email = payload.get("email")

        except Exception as e:
            logger.error(f"Admin auth error: {e}")
            return jsonify({"error": "Authentication failed"}), 401

        return f(*args, **kwargs)

    return decorated


# ────────────────────────────────────────────────────────────────
# 📊 GET ADMIN DASHBOARD STATS
# GET /api/admin/stats
# ────────────────────────────────────────────────────────────────
@admin_bp.route("/admin/stats", methods=["GET"])
@require_admin
def get_admin_stats():
    """
    Fetch platform-wide statistics for admin dashboard
    """
    try:
        # Total users count
        users_res = supabase.table("users").select("id, role, status").execute()
        users = users_res.data or []

        total_users = len(users)
        active_users = len([u for u in users if u.get("status") == "active"])
        donors = len([u for u in users if u.get("role") == "donor"])
        ngos = len([u for u in users if u.get("role") == "ngo"])

        # Pending NGOs (users with role=ngo but pending organization verification)
        pending_orgs_res = (
            supabase.table("organizations")
            .select("id")
            .eq("verification_status", "pending")
            .execute()
        )
        pending_ngos = len(pending_orgs_res.data or [])

        # Total donations
        donations_res = supabase.table("food_donations").select("id, status").execute()
        donations = donations_res.data or []

        total_donations = len(donations)
        available_donations = len([d for d in donations if d.get("status") == "available"])
        claimed_donations = len([d for d in donations if d.get("status") == "claimed"])
        completed_donations = len([d for d in donations if d.get("status") == "completed"])

        # Claims count
        claims_res = supabase.table("ngo_claims").select("id, status").execute()
        claims = claims_res.data or []

        total_claims = len(claims)
        completed_claims = len([c for c in claims if c.get("status") == "completed"])

        return jsonify({
            "total_users": total_users,
            "active_users": active_users,
            "donors": donors,
            "ngos": ngos,
            "pending_ngos": pending_ngos,
            "total_donations": total_donations,
            "available_donations": available_donations,
            "claimed_donations": claimed_donations,
            "completed_donations": completed_donations,
            "total_claims": total_claims,
            "completed_claims": completed_claims,
        }), 200

    except Exception as e:
        logger.exception("Admin stats error")
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


# ────────────────────────────────────────────────────────────────
# 🏢 GET PENDING NGO APPLICATIONS
# GET /api/admin/ngos/pending
# ────────────────────────────────────────────────────────────────
@admin_bp.route("/admin/ngos/pending", methods=["GET"])
@require_admin
def get_pending_ngos():
    """
    Fetch all pending NGO verification requests
    """
    try:
        # Join users with organizations to get full details
        orgs_res = (
            supabase.table("organizations")
            .select("*")
            .eq("verification_status", "pending")
            .order("created_at", desc=True)
            .execute()
        )

        pending_orgs = orgs_res.data or []

        if not pending_orgs:
            return jsonify([]), 200

        # Fetch associated user data
        user_ids = [org["user_id"] for org in pending_orgs if org.get("user_id")]

        users_map = {}
        if user_ids:
            users_res = (
                supabase.table("users")
                .select("id, full_name, email, phone, created_at, status")
                .in_("id", user_ids)
                .execute()
            )
            users_map = {u["id"]: u for u in (users_res.data or [])}

        # Merge data
        result = []
        for org in pending_orgs:
            user = users_map.get(org.get("user_id"), {})
            result.append({
                "user_id": user.get("id"),
                "org_id": org.get("id"),
                "full_name": org.get("name") or user.get("full_name"),
                "email": user.get("email"),
                "phone": org.get("phone") or user.get("phone"),
                "address": org.get("address"),
                "description": org.get("description"),
                "status": org.get("verification_status"),
                "created_at": org.get("created_at") or user.get("created_at"),
            })

        return jsonify(result), 200

    except Exception as e:
        logger.exception("Get pending NGOs error")
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


# ────────────────────────────────────────────────────────────────
# ✅ APPROVE NGO APPLICATION
# PUT /api/admin/ngos/<user_id>/approve
# ──���─────────────────────────────────────────────────────────────
@admin_bp.route("/admin/ngos/<user_id>/approve", methods=["PUT"])
@require_admin
def approve_ngo(user_id):
    """
    Approve an NGO application and send confirmation email
    """
    try:
        now = datetime.utcnow().isoformat()

        # 1. Fetch user
        user_res = (
            supabase.table("users")
            .select("id, email, full_name, status")
            .eq("id", user_id)
            .single()
            .execute()
        )

        if not user_res.data:
            return jsonify({"error": "User not found"}), 404

        user = user_res.data

        # 2. Update organization verification status
        org_update = (
            supabase.table("organizations")
            .update({
                "verification_status": "approved",
            })
            .eq("user_id", user_id)
            .execute()
        )

        if not org_update.data:
            return jsonify({"error": "Organization not found"}), 404

        # 3. Ensure user status is active
        supabase.table("users").update({
            "status": "active",
        }).eq("id", user_id).execute()

        # 4. Create notification
        supabase.table("notifications").insert({
            "user_id": user_id,
            "title": "NGO Application Approved",
            "message": (
                "Congratulations! Your organization has been verified. "
                "You can now claim food donations on the platform."
            ),
            "type": "status_update",
            "read": False,
            "created_at": now,
        }).execute()

        # 5. Send approval email
        try:
            msg = Message(
                subject="Your NGO Application Has Been Approved - FoodShare",
                recipients=[user.get("email")],
            )
            msg.body = f"""
Hello {user.get("full_name", "NGO Partner")},

Great news! Your organization has been verified and approved on FoodShare.

You can now:
- Browse available food donations
- Claim donations for pickup
- Track your impact and analytics

Log in to your dashboard to start making a difference!

Thank you for joining FoodShare in the fight against food waste.

Warm regards,
The FoodShare Team
"""
            mail.send(msg)
            logger.info(f"Approval email sent to {user.get('email')}")

        except Exception as email_err:
            logger.error(f"Failed to send approval email: {email_err}")

        return jsonify({
            "success": True,
            "message": "NGO approved successfully",
        }), 200

    except Exception as e:
        logger.exception("Approve NGO error")
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


# ────────────────────────────────────────────────────────────────
# ❌ REJECT NGO APPLICATION
# PUT /api/admin/ngos/<user_id>/reject
# ────────────────────────────────────────────────────────────────
@admin_bp.route("/admin/ngos/<user_id>/reject", methods=["PUT"])
@require_admin
def reject_ngo(user_id):
    """
    Reject an NGO application with optional reason
    """
    try:
        data = request.get_json() or {}
        reason = data.get("reason", "Your application did not meet our verification criteria.")

        now = datetime.utcnow().isoformat()

        # 1. Fetch user
        user_res = (
            supabase.table("users")
            .select("id, email, full_name")
            .eq("id", user_id)
            .single()
            .execute()
        )

        if not user_res.data:
            return jsonify({"error": "User not found"}), 404

        user = user_res.data

        # 2. Update organization verification status
        org_update = (
            supabase.table("organizations")
            .update({
                "verification_status": "rejected",
            })
            .eq("user_id", user_id)
            .execute()
        )

        if not org_update.data:
            return jsonify({"error": "Organization not found"}), 404

        # 3. Update user status
        supabase.table("users").update({
            "status": "rejected",
        }).eq("id", user_id).execute()

        # 4. Create notification
        supabase.table("notifications").insert({
            "user_id": user_id,
            "title": "NGO Application Not Approved",
            "message": f"Unfortunately, your application was not approved. Reason: {reason}",
            "type": "status_update",
            "read": False,
            "created_at": now,
        }).execute()

        # 5. Send rejection email
        try:
            msg = Message(
                subject="Update on Your NGO Application - FoodShare",
                recipients=[user.get("email")],
            )
            msg.body = f"""
Hello {user.get("full_name", "Applicant")},

Thank you for your interest in joining FoodShare.

After reviewing your application, we regret to inform you that your organization 
has not been approved at this time.

Reason: {reason}

If you believe this decision was made in error or would like to provide additional 
documentation, please contact our support team.

Thank you for your understanding.

Warm regards,
The FoodShare Team
"""
            mail.send(msg)
            logger.info(f"Rejection email sent to {user.get('email')}")

        except Exception as email_err:
            logger.error(f"Failed to send rejection email: {email_err}")

        return jsonify({
            "success": True,
            "message": "NGO application rejected",
        }), 200

    except Exception as e:
        logger.exception("Reject NGO error")
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


# ────────────────────────────────────────────────────────────────
# 👥 GET ALL USERS
# GET /api/admin/users
# ────────────────────────────────────────────────────────────────
@admin_bp.route("/admin/users", methods=["GET"])
@require_admin
def get_all_users():
    """
    Fetch all users with filters and pagination
    """
    try:
        # Query params
        role = request.args.get("role")
        status = request.args.get("status")
        search = request.args.get("search")
        page = int(request.args.get("page", 1))
        limit = int(request.args.get("limit", 50))
        offset = (page - 1) * limit

        # Build query
        query = supabase.table("users").select(
            "id, full_name, email, phone, role, status, created_at"
        )

        if role and role != "all":
            query = query.eq("role", role)

        if status and status != "all":
            query = query.eq("status", status)

        query = query.order("created_at", desc=True)

        # Execute query
        users_res = query.execute()
        users = users_res.data or []

        # Filter by search term (client-side for now)
        if search:
            search_lower = search.lower()
            users = [
                u for u in users
                if search_lower in (u.get("full_name") or "").lower()
                or search_lower in (u.get("email") or "").lower()
            ]

        # Get donation/claim counts for each user
        user_ids = [u["id"] for u in users]

        # Donation counts
        donations_map = {}
        if user_ids:
            donations_res = (
                supabase.table("food_donations")
                .select("donor_id")
                .in_("donor_id", user_ids)
                .execute()
            )
            for d in (donations_res.data or []):
                donor_id = d.get("donor_id")
                if donor_id:
                    donations_map[donor_id] = donations_map.get(donor_id, 0) + 1

        # Claim counts (for NGOs)
        claims_map = {}
        ngo_ids = [u["id"] for u in users if u.get("role") == "ngo"]
        if ngo_ids:
            claims_res = (
                supabase.table("ngo_claims")
                .select("ngo_id")
                .in_("ngo_id", ngo_ids)
                .execute()
            )
            for c in (claims_res.data or []):
                ngo_id = c.get("ngo_id")
                if ngo_id:
                    claims_map[ngo_id] = claims_map.get(ngo_id, 0) + 1

        # Enrich user data
        enriched_users = []
        for u in users:
            enriched_users.append({
                "id": u.get("id"),
                "full_name": u.get("full_name"),
                "email": u.get("email"),
                "phone": u.get("phone"),
                "role": u.get("role"),
                "status": u.get("status"),
                "created_at": u.get("created_at"),
                "last_active": u.get("last_active"),
                "donations_count": donations_map.get(u["id"], 0),
                "claims_count": claims_map.get(u["id"], 0),
            })

        # Apply pagination
        total = len(enriched_users)
        paginated = enriched_users[offset:offset + limit]

        return jsonify({
            "users": paginated,
            "total": total,
            "page": page,
            "limit": limit,
            "pages": (total + limit - 1) // limit,
        }), 200

    except Exception as e:
        logger.exception("Get all users error")
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


# ────────────────────────────────────────────────────────────────
# 🔄 UPDATE USER STATUS (Suspend/Activate)
# PUT /api/admin/users/<user_id>/status
# ────────────────────────────────────────────────────────────────
@admin_bp.route("/admin/users/<user_id>/status", methods=["PUT"])
def update_user_status(user_id):
    """
    Suspend or reactivate a user account
    """
    try:
        data = request.get_json() or {}
        new_status = data.get("status")

        if new_status not in ["active", "suspended"]:
            return jsonify({"error": "Invalid status. Use 'active' or 'suspended'"}), 400

        now = datetime.utcnow().isoformat()

        # 1. Fetch user
        user_res = (
            supabase.table("users")
            .select("id, email, full_name, role, status")
            .eq("id", user_id)
            .single()
            .execute()
        )

        if not user_res.data:
            return jsonify({"error": "User not found"}), 404

        user = user_res.data

        # 2. Prevent self-suspension (admin can't suspend themselves)
        auth_header = request.headers.get("Authorization")
        if auth_header:
            try:
                token = auth_header.split(" ")[1]
                payload = decode_jwt(token)
                if payload and payload.get("user_id") == user_id:
                    return jsonify({"error": "You cannot suspend your own account"}), 400
            except Exception:
                pass

        # 3. Update user status
        supabase.table("users").update({
            "status": new_status,
        }).eq("id", user_id).execute()

        # 4. Create notification
        notification_title = "Account Suspended" if new_status == "suspended" else "Account Reactivated"
        notification_message = (
            "Your account has been suspended. Please contact support for more information."
            if new_status == "suspended"
            else "Your account has been reactivated. You can now access all features."
        )

        supabase.table("notifications").insert({
            "user_id": user_id,
            "title": notification_title,
            "message": notification_message,
            "type": "account_status",
            "read": False,
            "created_at": now,
        }).execute()

        # 5. Send email notification
        try:
            subject = (
                "Account Suspended - FoodShare"
                if new_status == "suspended"
                else "Account Reactivated - FoodShare"
            )

            body = (
                f"""
Hello {user.get("full_name", "User")},

Your FoodShare account has been suspended due to a policy violation or admin action.

If you believe this was done in error, please contact our support team.

Regards,
The FoodShare Team
"""
                if new_status == "suspended"
                else f"""
Hello {user.get("full_name", "User")},

Good news! Your FoodShare account has been reactivated.

You can now log in and access all platform features.

Thank you for being part of our community!

Warm regards,
The FoodShare Team
"""
            )

            msg = Message(subject=subject, recipients=[user.get("email")])
            msg.body = body
            mail.send(msg)

        except Exception as email_err:
            logger.error(f"Failed to send status update email: {email_err}")

        return jsonify({
            "success": True,
            "message": f"User {new_status} successfully",
        }), 200

    except Exception as e:
        logger.exception("Update user status error")
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


# ────────────────────────────────────────────────────────────────
# 🗑️ DELETE USER ACCOUNT (Admin)
# DELETE /api/admin/users/<user_id>
# ────────────────────────────────────────────────────────────────
@admin_bp.route("/admin/users/<user_id>", methods=["DELETE"])
def delete_user_admin(user_id):
    """
    Permanently delete a user account (admin action)
    """
    try:
        # 1. Fetch user
        user_res = (
            supabase.table("users")
            .select("id, email, full_name, role")
            .eq("id", user_id)
            .single()
            .execute()
        )

        if not user_res.data:
            return jsonify({"error": "User not found"}), 404

        user = user_res.data

        # 2. Prevent deleting admin accounts
        if user.get("role") == "admin":
            return jsonify({"error": "Admin accounts cannot be deleted"}), 403

        # 3. Anonymize donations (preserve history)
        supabase.table("food_donations").update({
            "donor_id": None,
        }).eq("donor_id", user_id).execute()

        # 4. Delete related data
        supabase.table("notifications").delete().eq("user_id", user_id).execute()
        supabase.table("password_change_codes").delete().eq("user_id", user_id).execute()
        supabase.table("organizations").delete().eq("user_id", user_id).execute()
        supabase.table("ngo_claims").delete().eq("ngo_id", user_id).execute()

        # 5. Delete user
        supabase.table("users").delete().eq("id", user_id).execute()

        logger.info(f"Admin deleted user: {user_id}")

        return jsonify({
            "success": True,
            "message": "User deleted successfully",
        }), 200

    except Exception as e:
        logger.exception("Delete user error")
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


# ────────────────────────────────────────────────────────────────
# 📈 GET ALL DONATIONS (Admin View)
# GET /api/admin/donations
# ────────────────────────────────────────────────────────────────
@admin_bp.route("/admin/donations", methods=["GET"])
def get_all_donations():
    """
    Fetch all donations with filters for admin moderation
    """
    try:
        status = request.args.get("status")
        category = request.args.get("category")
        page = int(request.args.get("page", 1))
        limit = int(request.args.get("limit", 50))
        offset = (page - 1) * limit

        # Build query
        query = supabase.table("food_donations").select("*")

        if status and status != "all":
            query = query.eq("status", status)

        if category and category != "all":
            query = query.eq("category", category)

        query = query.order("created_at", desc=True)

        # Execute
        donations_res = query.execute()
        donations = donations_res.data or []

        # Get donor info
        donor_ids = list({d["donor_id"] for d in donations if d.get("donor_id")})
        donors_map = {}

        if donor_ids:
            donors_res = (
                supabase.table("users")
                .select("id, full_name, email")
                .in_("id", donor_ids)
                .execute()
            )
            donors_map = {d["id"]: d for d in (donors_res.data or [])}

        # Enrich donations
        enriched = []
        for d in donations:
            donor = donors_map.get(d.get("donor_id"), {})
            enriched.append({
                **d,
                "donor_name": donor.get("full_name"),
                "donor_email": donor.get("email"),
            })

        total = len(enriched)
        paginated = enriched[offset:offset + limit]

        return jsonify({
            "donations": paginated,
            "total": total,
            "page": page,
            "limit": limit,
            "pages": (total + limit - 1) // limit,
        }), 200

    except Exception as e:
        logger.exception("Get all donations error")
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


# ────────────────────────────────────────────────────────────────
# 📊 GENERATE REPORTS
# GET /api/admin/reports/<report_type>
# ────────────────────────────────────────────────────────────────
@admin_bp.route("/admin/reports/<report_type>", methods=["GET"])
def generate_report(report_type):
    """
    Generate various admin reports
    Supported types: users, donations, ngos, impact
    """
    try:
        today = date.today()
        
        if report_type == "users":
            # User report
            users_res = (
                supabase.table("users")
                .select("id, full_name, email, phone, role, status, created_at")
                .order("created_at", desc=True)
                .execute()
            )
            
            return jsonify({
                "report_type": "users",
                "generated_at": datetime.utcnow().isoformat(),
                "total_records": len(users_res.data or []),
                "data": users_res.data or [],
            }), 200

        elif report_type == "donations":
            # Donations report
            donations_res = (
                supabase.table("food_donations")
                .select("*")
                .order("created_at", desc=True)
                .execute()
            )
            
            donations = donations_res.data or []
            
            # Summary stats
            summary = {
                "total": len(donations),
                "available": len([d for d in donations if d.get("status") == "available"]),
                "claimed": len([d for d in donations if d.get("status") == "claimed"]),
                "completed": len([d for d in donations if d.get("status") == "completed"]),
                "expired": len([d for d in donations if d.get("final_state") == "expired"]),
                "cancelled": len([d for d in donations if d.get("final_state") == "cancelled_by_donor"]),
            }
            
            return jsonify({
                "report_type": "donations",
                "generated_at": datetime.utcnow().isoformat(),
                "summary": summary,
                "data": donations,
            }), 200

        elif report_type == "ngos":
            # NGO report
            orgs_res = (
                supabase.table("organizations")
                .select("*")
                .order("created_at", desc=True)
                .execute()
            )
            
            orgs = orgs_res.data or []
            
            # Get user info
            user_ids = [o["user_id"] for o in orgs if o.get("user_id")]
            users_map = {}
            
            if user_ids:
                users_res = (
                    supabase.table("users")
                    .select("id, email, full_name")
                    .in_("id", user_ids)
                    .execute()
                )
                users_map = {u["id"]: u for u in (users_res.data or [])}
            
            # Get claim stats
            claims_res = (
                supabase.table("ngo_claims")
                .select("ngo_id, status")
                .execute()
            )
            
            claims_map = {}
            for c in (claims_res.data or []):
                ngo_id = c.get("ngo_id")
                if ngo_id:
                    if ngo_id not in claims_map:
                        claims_map[ngo_id] = {"total": 0, "completed": 0}
                    claims_map[ngo_id]["total"] += 1
                    if c.get("status") == "completed":
                        claims_map[ngo_id]["completed"] += 1
            
            # Enrich data
            enriched = []
            for org in orgs:
                user = users_map.get(org.get("user_id"), {})
                claims = claims_map.get(org.get("user_id"), {"total": 0, "completed": 0})
                
                enriched.append({
                    **org,
                    "email": user.get("email"),
                    "total_claims": claims["total"],
                    "completed_claims": claims["completed"],
                })
            
            summary = {
                "total": len(orgs),
                "approved": len([o for o in orgs if o.get("verification_status") == "approved"]),
                "pending": len([o for o in orgs if o.get("verification_status") == "pending"]),
                "rejected": len([o for o in orgs if o.get("verification_status") == "rejected"]),
            }
            
            return jsonify({
                "report_type": "ngos",
                "generated_at": datetime.utcnow().isoformat(),
                "summary": summary,
                "data": enriched,
            }), 200

        elif report_type == "impact":
            # Platform impact report
            
            # Completed claims with donation data
            claims_res = (
                supabase.table("ngo_claims")
                .select("donation_id, status, completed_at")
                .eq("status", "completed")
                .execute()
            )
            
            claims = claims_res.data or []
            donation_ids = list({c["donation_id"] for c in claims if c.get("donation_id")})
            
            donations_map = {}
            if donation_ids:
                donations_res = (
                    supabase.table("food_donations")
                    .select("id, quantity, unit, category")
                    .in_("id", donation_ids)
                    .execute()
                )
                donations_map = {d["id"]: d for d in (donations_res.data or [])}
            
            # Calculate impact metrics
            PIECE_TO_KG = {"Fruits": 0.18, "Vegetables": 0.25, "Meat": 0.30, "Dairy": 0.50, "Grains": 0.40, "Prepared Food": 0.40}
            
            def convert_to_kg(qty, unit, category):
                try:
                    qty = float(qty)
                except:
                    return 0.0
                
                unit = (unit or "").lower()
                if unit == "kg":
                    return qty
                if unit == "pieces":
                    return qty * PIECE_TO_KG.get(category, 0.25)
                if unit == "liters":
                    return qty * 1.0
                if unit == "boxes":
                    return qty * 5.0
                return 0.0
            
            total_food_kg = 0.0
            category_breakdown = {}
            
            for c in claims:
                donation = donations_map.get(c.get("donation_id"))
                if donation:
                    kg = convert_to_kg(
                        donation.get("quantity", 0),
                        donation.get("unit"),
                        donation.get("category")
                    )
                    total_food_kg += kg
                    
                    cat = donation.get("category", "Other")
                    category_breakdown[cat] = category_breakdown.get(cat, 0) + kg
            
            # Environmental impact estimates
            co2_avoided = total_food_kg * 2.5  # ~2.5kg CO2 per kg food waste
            water_saved = total_food_kg * 1000  # ~1000L water per kg food
            
            return jsonify({
                "report_type": "impact",
                "generated_at": datetime.utcnow().isoformat(),
                "metrics": {
                    "total_food_saved_kg": round(total_food_kg, 2),
                    "co2_avoided_kg": round(co2_avoided, 2),
                    "water_saved_liters": round(water_saved, 2),
                    "completed_donations": len(claims),
                },
                "category_breakdown": {
                    k: round(v, 2) for k, v in category_breakdown.items()
                },
            }), 200

        else:
            return jsonify({"error": f"Unknown report type: {report_type}"}), 400

    except Exception as e:
        logger.exception("Generate report error")
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


# ────────────────────────────────────────────────────────────────
# 📥 EXPORT REPORT AS CSV
# GET /api/admin/reports/<report_type>/export
# ────────────────────────────────────────────────────────────────
@admin_bp.route("/admin/reports/<report_type>/export", methods=["GET"])
def export_report_csv(report_type):
    """
    Export report data as CSV
    """
    try:
        from flask import Response
        
        if report_type == "users":
            users_res = (
                supabase.table("users")
                .select("id, full_name, email, phone, role, status, created_at")
                .order("created_at", desc=True)
                .execute()
            )
            
            data = users_res.data or []
            headers = ["ID", "Full Name", "Email", "Phone", "Role", "Status", "Created At"]
            
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(headers)
            
            for row in data:
                writer.writerow([
                    row.get("id"),
                    row.get("full_name"),
                    row.get("email"),
                    row.get("phone"),
                    row.get("role"),
                    row.get("status"),
                    row.get("created_at"),
                ])
            
            output.seek(0)
            
            return Response(
                output.getvalue(),
                mimetype="text/csv",
                headers={"Content-Disposition": f"attachment; filename=users_report_{date.today()}.csv"}
            )

        elif report_type == "donations":
            donations_res = (
                supabase.table("food_donations")
                .select("id, title, category, quantity, unit, status, final_state, created_at, expiry_date")
                .order("created_at", desc=True)
                .execute()
            )
            
            data = donations_res.data or []
            headers = ["ID", "Title", "Category", "Quantity", "Unit", "Status", "Final State", "Created At", "Expiry Date"]
            
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(headers)
            
            for row in data:
                writer.writerow([
                    row.get("id"),
                    row.get("title"),
                    row.get("category"),
                    row.get("quantity"),
                    row.get("unit"),
                    row.get("status"),
                    row.get("final_state"),
                    row.get("created_at"),
                    row.get("expiry_date"),
                ])
            
            output.seek(0)
            
            return Response(
                output.getvalue(),
                mimetype="text/csv",
                headers={"Content-Disposition": f"attachment; filename=donations_report_{date.today()}.csv"}
            )

        else:
            return jsonify({"error": f"CSV export not available for: {report_type}"}), 400

    except Exception as e:
        logger.exception("Export CSV error")
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


# ────────────────────────────────────────────────────────────────
# 🔔 SEND BROADCAST NOTIFICATION
# POST /api/admin/notifications/broadcast
# ────────────────────────────────────────────────────────────────
@admin_bp.route("/admin/notifications/broadcast", methods=["POST"])
def send_broadcast_notification():
    """
    Send a notification to all users or specific role
    """
    try:
        data = request.get_json() or {}
        title = data.get("title")
        message = data.get("message")
        target_role = data.get("role")  # Optional: "donor", "ngo", or None for all

        if not title or not message:
            return jsonify({"error": "Title and message are required"}), 400

        now = datetime.utcnow().isoformat()

        # Fetch target users
        query = supabase.table("users").select("id")
        
        if target_role:
            query = query.eq("role", target_role)
        
        query = query.eq("status", "active")
        
        users_res = query.execute()
        users = users_res.data or []

        if not users:
            return jsonify({"error": "No users found for broadcast"}), 404

        # Create notifications in batch
        notifications = [
            {
                "user_id": user["id"],
                "title": title,
                "message": message,
                "type": "broadcast",
                "read": False,
                "created_at": now,
            }
            for user in users
        ]

        # Insert in batches of 100
        batch_size = 100
        for i in range(0, len(notifications), batch_size):
            batch = notifications[i:i + batch_size]
            supabase.table("notifications").insert(batch).execute()

        return jsonify({
            "success": True,
            "message": f"Notification sent to {len(users)} users",
            "recipients": len(users),
        }), 200

    except Exception as e:
        logger.exception("Broadcast notification error")
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


# ────────────────────────────────────────────────────────────────
# 🔧 SYSTEM HEALTH CHECK
# GET /api/admin/health
# ────────────────────────────────────────────────────────────────
@admin_bp.route("/admin/health", methods=["GET"])
def system_health():
    """
    Check system health and database connectivity
    """
    try:
        # Test database connection
        test_res = supabase.table("users").select("id").limit(1).execute()
        db_status = "healthy" if test_res.data is not None else "unhealthy"

        return jsonify({
            "status": "ok",
            "database": db_status,
            "timestamp": datetime.utcnow().isoformat(),
        }), 200

    except Exception as e:
        return jsonify({
            "status": "error",
            "database": "unhealthy",
            "error": str(e),
            "timestamp": datetime.utcnow().isoformat(),
        }), 500
