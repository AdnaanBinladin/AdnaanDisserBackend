from flask import Blueprint, request, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
from src.services.supabase_service import supabase
from postgrest.exceptions import APIError
import jwt
import os
from datetime import datetime, timedelta
import hashlib
import logging
import random
import secrets
from src.utils.password_utils import validate_password_strength
from src.utils.jwt import decode_jwt
from flask_mail import Message
from src.utils.mail_instance import mail
from src.utils.validators import is_valid_email
from src.utils.audit_log import log_audit


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger(__name__)


auth_bp = Blueprint("auth", __name__)
profile_bp = Blueprint("profile", __name__)

JWT_SECRET = os.getenv("JWT_SECRET")


def _require_auth_payload():
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None, (jsonify({"error": "Missing token"}), 401)

    token = auth_header.split(" ", 1)[1]
    payload = decode_jwt(token)
    if not payload:
        return None, (jsonify({"error": "Invalid or expired token"}), 401)

    return payload, None

# ────────────────────────────────────────────────────────────────
# 🧩 REGISTER ENDPOINT (Handles both Donor and NGO)
# ────────────────────────────────────────────────────────────────
@auth_bp.route("/register", methods=["POST"])
def register_user():
    try:
        data = request.get_json()

        email = data.get("email")
        password = data.get("password")
        full_name = data.get("full_name")
        phone = data.get("phone")
        role = data.get("role")
        address = data.get("address")
        description = data.get("description")

        # 1️⃣ Required fields
        if not all([email, password, full_name, role]):
            log_audit(
                "register_failed",
                user_role=role,
                entity_type="user",
                metadata={"email": email, "reason": "missing_required_fields"},
                req=request,
            )
            return jsonify({"error": "Missing required fields"}), 400

        # 2️⃣ Email validation
        if not is_valid_email(email):
            log_audit(
                "register_failed",
                user_role=role,
                entity_type="user",
                metadata={"email": email, "reason": "invalid_email"},
                req=request,
            )
            return jsonify({"error": "Invalid email format"}), 400

        # 3️⃣ Password validation
        strength_error = validate_password_strength(password)
        if strength_error:
            log_audit(
                "register_failed",
                user_role=role,
                entity_type="user",
                metadata={"email": email, "reason": "weak_password"},
                req=request,
            )
            return jsonify({"error": strength_error}), 400

        # 4️⃣ NGO-specific validation (MUST be before insert)
        if role == "ngo":
            if not all([address, description, phone]):
                log_audit(
                    "register_failed",
                    user_role=role,
                    entity_type="organization",
                    metadata={"email": email, "reason": "missing_ngo_fields"},
                    req=request,
                )
                return jsonify({"error": "Missing NGO organization fields"}), 400

        # 5️⃣ Hash password
        hashed_pw = generate_password_hash(password)

        # 6️⃣ Correct status
        status = "pending" if role == "ngo" else "active"

        # 7️⃣ Insert user
        user_response = supabase.table("users").insert({
            "email": email,
            "password_hash": hashed_pw,
            "full_name": full_name,
            "phone": phone,
            "role": role,
            "status": status
        }).execute()

        if not user_response.data:
            return jsonify({"error": "Failed to register user"}), 500

        user_id = user_response.data[0]["id"]

        # 8️⃣ Insert organization ONLY for NGO
        if role == "ngo":
            supabase.table("organizations").insert({
                "user_id": user_id,
                "name": full_name,
                "address": address,
                "description": description,
                "phone": phone,
                "verification_status": "pending"
            }).execute()

        log_audit(
            "register_successful",
            user_id=user_id,
            user_role=role,
            entity_type="user",
            entity_id=user_id,
            metadata={"email": email, "status": status},
            req=request,
        )
        return jsonify({
            "message": "Registration successful",
            "user_id": user_id,
            "role": role,
            "status": status
        }), 201

    except APIError as e:
        log_audit(
            "register_failed",
            user_role=(request.get_json(silent=True) or {}).get("role"),
            entity_type="user",
            metadata={"reason": "api_error", "error": str(e)},
            req=request,
        )
        if "duplicate key" in str(e).lower():
            return jsonify({"error": "Email already exists"}), 409
        return jsonify({"error": "Database error occurred"}), 500


# ────────────────────────────────────────────────────────────────
# 🧠 LOGIN ENDPOINT
# ────────────────────────────────────────────────────────────────
@auth_bp.route("/login", methods=["POST"])
def login_user():
    try:
        data = request.get_json()
        email = data.get("email")
        password = data.get("password")

        if not all([email, password]):
            log_audit(
                "login_unsuccessful",
                entity_type="user",
                metadata={"email": email, "reason": "missing_credentials"},
                req=request,
            )
            return jsonify({"error": "Email and password are required"}), 400

        # ✅ Query user by email
        user = (
            supabase
            .table("users")
            .select("*")
            .eq("email", email)
            .single()
            .execute()
        )

        if not user.data:
            log_audit(
                "login_unsuccessful",
                entity_type="user",
                metadata={"email": email, "reason": "user_not_found"},
                req=request,
            )
            return jsonify({"error": "Invalid email or password"}), 401

        # ✅ Verify password
        if not check_password_hash(user.data["password_hash"], password):
            log_audit(
                "login_unsuccessful",
                user_id=user.data.get("id"),
                user_role=user.data.get("role"),
                entity_type="user",
                entity_id=user.data.get("id"),
                metadata={"email": email, "reason": "wrong_password"},
                req=request,
            )
            return jsonify({"error": "Invalid email or password"}), 401

        # 🚫 Block inactive / suspended accounts
        if user.data.get("status") != "active":
            log_audit(
                "login_unsuccessful",
                user_id=user.data.get("id"),
                user_role=user.data.get("role"),
                entity_type="user",
                entity_id=user.data.get("id"),
                metadata={"email": email, "reason": "inactive_account"},
                req=request,
            )
            return jsonify({
                "error": "Your account is not active. Please contact support."
            }), 403

        # ✅ Generate JWT token
        token = jwt.encode(
            {
                "user_id": user.data["id"],
                "email": user.data["email"],
                "role": user.data["role"],
                "exp": datetime.utcnow() + timedelta(hours=12)
            },
            JWT_SECRET,
            algorithm="HS256"
        )

        log_audit(
            "login_successful",
            user_id=user.data.get("id"),
            user_role=user.data.get("role"),
            entity_type="user",
            entity_id=user.data.get("id"),
            metadata={"email": user.data.get("email")},
            req=request,
        )
        return jsonify({
            "message": "Login successful",
            "token": token,
            "donor": {
                "id": user.data["id"],
                "full_name": user.data["full_name"],
                "email": user.data["email"],
                "role": user.data["role"]
            }
        }), 200

    except Exception as e:
        log_audit(
            "login_unsuccessful",
            entity_type="user",
            metadata={"reason": "server_error", "error": str(e)},
            req=request,
        )
        logger.exception("🔥 Login Error")
        return jsonify({"error": "Login failed"}), 500


@auth_bp.route("/auth/verify", methods=["GET"])
def verify_auth_token():
    payload, auth_error = _require_auth_payload()
    if auth_error:
        return auth_error

    return jsonify(
        {
            "valid": True,
            "user_id": payload.get("user_id"),
            "email": payload.get("email"),
            "role": payload.get("role"),
        }
    ), 200


# ────────────────────────────────────────────────────────────────
# 🧾 GET DONOR PROFILE BY ID
# ────────────────────────────────────────────────────────────────
@auth_bp.route("/donors/<donor_id>", methods=["GET"])
def get_donor_profile(donor_id):
    try:
        payload, auth_error = _require_auth_payload()
        if auth_error:
            return auth_error
        if payload.get("role") != "admin" and payload.get("user_id") != donor_id:
            return jsonify({"error": "Forbidden"}), 403

        response = supabase.table("users").select(
            "id, full_name, email, phone, role, status"
        ).eq("id", donor_id).single().execute()

        if not response.data:
            return jsonify({"error": "Donor not found"}), 404

        return jsonify(response.data), 200

    except Exception as e:
        print("⚠️ Error fetching donor profile:", e)
        return jsonify({"error": str(e)}), 500


@auth_bp.route("/profile", methods=["GET"])
def get_profile():
    try:
        payload, auth_error = _require_auth_payload()
        if auth_error:
            return auth_error

        user_id = request.args.get("userId")

        print("🔍 PROFILE FETCH userId =", user_id)

        if not user_id:
            return jsonify({"error": "Missing userId"}), 400
        if payload.get("role") != "admin" and payload.get("user_id") != user_id:
            return jsonify({"error": "Forbidden"}), 403

        user_res = supabase.table("users") \
            .select("id, full_name, email, phone, role, status, created_at") \
            .eq("id", user_id) \
            .execute()

        print("🧠 SUPABASE RESPONSE =", user_res.data)

        if not user_res.data:
            return jsonify({"error": "User not found"}), 404

        user = user_res.data[0]   # ✅ SAFE

        if user["role"] == "ngo":
            org_res = supabase.table("organizations") \
                .select("*") \
                .eq("user_id", user_id) \
                .execute()

            return jsonify({
                "type": "ngo",
                "user": user,
                "organization": org_res.data[0] if org_res.data else None
            }), 200

        return jsonify({
            "type": "donor",
            "user": user
        }), 200

    except Exception as e:
        print("🔥 PROFILE ERROR:", e)
        return jsonify({"error": str(e)}), 500


@auth_bp.route("/profile", methods=["PATCH"])
def update_profile():
    try:
        payload, auth_error = _require_auth_payload()
        if auth_error:
            return auth_error

        data = request.get_json()
        logger.info(f"PATCH /profile payload: {data}")

        user_id = data.get("userId")

        if not user_id:
            logger.warning("PATCH /profile missing userId")
            return jsonify({"error": "Missing userId"}), 400
        if payload.get("role") != "admin" and payload.get("user_id") != user_id:
            return jsonify({"error": "Forbidden"}), 403

        update_fields = {
            k: v for k, v in data.items()
            if k in ["full_name", "email", "phone"]
        }

        if not update_fields:
            logger.warning(f"No fields to update for userId={user_id}")
            return jsonify({"error": "No fields to update"}), 400

        logger.info(f"Updating user {user_id} with {update_fields}")

        supabase.table("users") \
            .update(update_fields) \
            .eq("id", user_id) \
            .execute()

        log_audit(
            "profile_edited",
            user_id=user_id,
            entity_type="user",
            entity_id=user_id,
            metadata={"updated_fields": list(update_fields.keys())},
            req=request,
        )

        logger.info(f"Profile updated successfully for userId={user_id}")
        return jsonify({"message": "Profile updated successfully"}), 200

    except Exception as e:
        logger.exception("🔥 PATCH /profile crashed")
        return jsonify({"error": "Failed to update profile"}), 500


@profile_bp.route("/profile/password/request", methods=["PATCH"])
def request_password_change():
    # 1️⃣ Auth
    auth_header = request.headers.get("Authorization")
    if not auth_header:
        return jsonify({"error": "Missing token"}), 401

    token = auth_header.split(" ")[1]
    payload = decode_jwt(token)

    if not payload:
        return jsonify({"error": "Invalid or expired token"}), 401

    user_id = payload["user_id"]

    # 2️⃣ Body
    data = request.get_json()
    current_password = data.get("current_password")
    new_password = data.get("new_password")

    if not current_password or not new_password:
        return jsonify({"error": "All fields are required"}), 400

    # 3️⃣ Fetch user
    user = (
        supabase
        .table("users")
        .select("email, password_hash")
        .eq("id", user_id)
        .single()
        .execute()
    )

    if not user.data:
        return jsonify({"error": "User not found"}), 404

    # 4️⃣ Verify current password
    if not check_password_hash(user.data["password_hash"], current_password):
        return jsonify({"error": "Current password is incorrect"}), 400
    
    # 🔐 4️⃣.5️⃣ BLOCK same password (UX + security consistency)
    if check_password_hash(user.data["password_hash"], new_password):
        return jsonify({
        "error": "New password must be different from your current password"
    }), 400

    # 5️⃣ Validate new password strength
    strength_error = validate_password_strength(new_password)
    if strength_error:
        return jsonify({"error": strength_error}), 400

    # 🔥 NEW STEP: Invalidate any previous OTPs for this user
    supabase.table("password_change_codes") \
        .delete() \
        .eq("user_id", user_id) \
        .execute()

    # 6️⃣ Generate OTP
    otp = str(random.randint(100000, 999999))
    otp_hash = generate_password_hash(otp)
    expires_at = datetime.utcnow() + timedelta(minutes=10)

    # 7️⃣ Store OTP (invalidate previous codes implicitly by only using latest)
    supabase.table("password_change_codes").insert({
        "user_id": user_id,
        "code_hash": otp_hash,
        "expires_at": expires_at.isoformat(),
        "created_at": datetime.utcnow().isoformat()
    }).execute()

    # 8️⃣ Send email
    msg = Message(
        subject="FoodShare Password Change Verification Code",
        recipients=[user.data["email"]],
    )
    msg.body = f"""
Hello,

You requested to change your password.

Your verification code is: {otp}

This code will expire in 10 minutes.

If you did not request this change, please ignore this email.

– FoodShare Security Team
"""
    mail.send(msg)

    log_audit(
        "password_change_requested",
        user_id=user_id,
        entity_type="user",
        entity_id=user_id,
        metadata={"channel": "email_otp"},
        req=request,
    )

    return jsonify({
        "message": "Verification code sent to your email"
    }), 200

@profile_bp.route("/profile/password/verify", methods=["PATCH"])
def verify_password_change():
    # 1️⃣ Auth
    auth_header = request.headers.get("Authorization")
    if not auth_header:
        return jsonify({"error": "Missing token"}), 401

    token = auth_header.split(" ")[1]
    payload = decode_jwt(token)

    if not payload:
        return jsonify({"error": "Invalid or expired token"}), 401

    user_id = payload["user_id"]

    # 2️⃣ Body
    data = request.get_json()
    code = data.get("code")
    new_password = data.get("new_password")

    if not code or not new_password:
        return jsonify({"error": "Code and new password are required"}), 400

    # 3️⃣ Fetch latest OTP
    otp_record = (
        supabase
        .table("password_change_codes")
        .select("*")
        .eq("user_id", user_id)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )

    if not otp_record.data:
        return jsonify({"error": "No verification code found"}), 400

    otp_data = otp_record.data[0]

    # 4️⃣ Expiry check
    expires_at = datetime.fromisoformat(otp_data["expires_at"])
    if datetime.utcnow() > expires_at:
        return jsonify({"error": "Verification code has expired"}), 400

    # 5️⃣ Verify OTP
    if not check_password_hash(otp_data["code_hash"], code):
        return jsonify({"error": "Invalid verification code"}), 400

    # 6️⃣ Fetch current password hash
    user = (
        supabase
        .table("users")
        .select("password_hash")
        .eq("id", user_id)
        .single()
        .execute()
    )

    if not user.data:
        return jsonify({"error": "User not found"}), 404

    # 🔐 7️⃣ BLOCK reusing old password
    if check_password_hash(user.data["password_hash"], new_password):
        return jsonify({
            "error": "New password must be different from your current password"
        }), 400

    # 8️⃣ Validate password strength (final authority)
    strength_error = validate_password_strength(new_password)
    if strength_error:
        return jsonify({"error": strength_error}), 400

    # 9️⃣ Update password
    new_hash = generate_password_hash(new_password)
    supabase.table("users") \
        .update({"password_hash": new_hash}) \
        .eq("id", user_id) \
        .execute()

    # 🔟 Invalidate OTP (one-time use)
    supabase.table("password_change_codes") \
        .delete() \
        .eq("id", otp_data["id"]) \
        .execute()

    log_audit(
        "password_changed",
        user_id=user_id,
        entity_type="user",
        entity_id=user_id,
        metadata={"method": "otp_verify"},
        req=request,
    )

    return jsonify({
        "message": "Password updated successfully"
    }), 200



@profile_bp.route("/profile/password/resend", methods=["PATCH"])
def resend_password_otp():
    # 1️⃣ Auth
    auth_header = request.headers.get("Authorization")
    if not auth_header:
        return jsonify({"error": "Missing token"}), 401

    token = auth_header.split(" ")[1]
    payload = decode_jwt(token)

    if not payload:
        return jsonify({"error": "Invalid or expired token"}), 401

    user_id = payload["user_id"]

    # 2️⃣ Fetch user email
    user = supabase.table("users") \
        .select("email") \
        .eq("id", user_id) \
        .single() \
        .execute()

    if not user.data:
        return jsonify({"error": "User not found"}), 404

    # 3️⃣ Fetch latest OTP
    otp_record = supabase.table("password_change_codes") \
        .select("*") \
        .eq("user_id", user_id) \
        .order("created_at", desc=True) \
        .limit(1) \
        .execute()

    if otp_record.data:
        last_created = datetime.fromisoformat(
            otp_record.data[0]["created_at"]
        )
        # ⏱️ 30s cooldown
        if datetime.utcnow() - last_created < timedelta(seconds=30):
            return jsonify({
                "error": "Please wait before resending the code"
            }), 429

        # ❌ Invalidate previous OTP
        supabase.table("password_change_codes") \
            .delete() \
            .eq("user_id", user_id) \
            .execute()

    # 4️⃣ Generate new OTP
    otp = str(random.randint(100000, 999999))
    otp_hash = generate_password_hash(otp)
    expires_at = datetime.utcnow() + timedelta(minutes=10)

    supabase.table("password_change_codes").insert({
        "user_id": user_id,
        "code_hash": otp_hash,
        "expires_at": expires_at.isoformat(),
        "created_at": datetime.utcnow().isoformat()
    }).execute()

    # 5️⃣ Send email
    msg = Message(
        subject="FoodShare Password Change Verification Code",
        recipients=[user.data["email"]],
    )
    msg.body = f"""
Hello,

Your new verification code is: {otp}

This code will expire in 10 minutes.

If you did not request this, please ignore this email.

– FoodShare Security Team
"""
    mail.send(msg)

    log_audit(
        "password_change_otp_resent",
        user_id=user_id,
        entity_type="user",
        entity_id=user_id,
        metadata={"channel": "email_otp"},
        req=request,
    )

    return jsonify({
        "message": "Verification code resent"
    }), 200


@auth_bp.route("/account/delete", methods=["DELETE"])
def delete_account():
    try:
        payload, auth_error = _require_auth_payload()
        if auth_error:
            return auth_error

        data = request.get_json()
        user_id = data.get("userId")

        if not user_id:
            return jsonify({"error": "Missing userId"}), 400
        if payload.get("role") != "admin" and payload.get("user_id") != user_id:
            return jsonify({"error": "Forbidden"}), 403

        # 1️⃣ Check user exists
        user = supabase.table("users") \
            .select("id") \
            .eq("id", user_id) \
            .execute()

        if not user.data:
            return jsonify({"error": "User not found"}), 404

        # 2️⃣ Anonymise donations (keep history)
        supabase.table("food_donations") \
            .update({
                "donor_id": None
            }) \
            .eq("donor_id", user_id) \
            .execute()

        # 3️⃣ Delete notifications
        supabase.table("notifications") \
            .delete() \
            .eq("user_id", user_id) \
            .execute()

        # 4️⃣ Delete password reset codes
        supabase.table("password_change_codes") \
            .delete() \
            .eq("user_id", user_id) \
            .execute()

        # 5️⃣ Delete user account
        supabase.table("users") \
            .delete() \
            .eq("id", user_id) \
            .execute()

        log_audit(
            "account_deleted",
            user_id=user_id,
            entity_type="user",
            entity_id=user_id,
            metadata={"deleted_by": "self"},
            req=request,
        )

        logger.info(f"✅ Account deleted userId={user_id}")

        return jsonify({"message": "Account deleted successfully"}), 200

    except Exception as e:
        logger.exception("🔥 Delete account failed")
        return jsonify({"error": "Failed to delete account"}), 500


@auth_bp.route("/organizations/<org_id>", methods=["PATCH"])
def update_organization(org_id):
    import traceback
    try:
        payload, auth_error = _require_auth_payload()
        if auth_error:
            return auth_error

        org_res = (
            supabase
            .table("organizations")
            .select("id, user_id")
            .eq("id", org_id)
            .limit(1)
            .execute()
        )
        if not org_res.data:
            return jsonify({"error": "Organization not found"}), 404

        org = org_res.data[0]
        if payload.get("role") != "admin" and payload.get("user_id") != org.get("user_id"):
            return jsonify({"error": "Forbidden"}), 403

        data = request.get_json() or {}

        update_payload = {
            "name": data.get("name"),
            "description": data.get("description"),
            "address": data.get("address"),
            "phone": data.get("phone"),
        }

        update_payload = {k: v for k, v in update_payload.items() if v is not None}

        print("🛠️ UPDATE PAYLOAD =", update_payload)

        result = (
            supabase
            .table("organizations")
            .update(update_payload)
            .eq("id", org_id)
            .execute()
        )

        if not result.data:
            return jsonify({"error": "Organization not found"}), 404

        log_audit(
            "organization_edited",
            entity_type="organization",
            entity_id=org_id,
            metadata={"updated_fields": list(update_payload.keys())},
            req=request,
        )

        return jsonify({"success": True}), 200

    except Exception:
        print("❌ UPDATE ORG ERROR")
        print(traceback.format_exc())
        return jsonify({"error": "Internal server error"}), 500



@auth_bp.route("/auth/forgot-password", methods=["POST"])
def forgot_password():
    try:
        data = request.get_json(silent=True) or {}
        email = data.get("email")

        # ✅ Always return generic success (security best practice)
        if not email:
            return jsonify({
                "message": "If the account exists, a reset link will be sent"
            }), 200

        # 🔍 Find user
        user_res = (
            supabase.table("users")
            .select("id")
            .eq("email", email)
            .maybe_single()
            .execute()
        )

        if not user_res.data:
            # ❌ Do NOT reveal user existence
            return jsonify({
                "message": "If the account exists, a reset link will be sent"
            }), 200

        user_id = user_res.data["id"]

        # 🔐 Generate secure reset token
        raw_token = secrets.token_urlsafe(48)
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()

        # 🧹 Invalidate previous reset tokens
        supabase.table("password_resets") \
            .delete() \
            .eq("user_id", user_id) \
            .execute()

        # 💾 Store new reset token
        supabase.table("password_resets").insert({
            "user_id": user_id,
            "token_hash": token_hash,
            "expires_at": (
                datetime.utcnow() + timedelta(minutes=30)
            ).isoformat(),
            "used": False
        }).execute()

        # 🔗 Build reset link
        reset_link = (
            f"{os.getenv('FRONTEND_URL')}"
            f"/forgot-password/reset-password?token={raw_token}"
        )

        # 📧 Send email
        msg = Message(
            subject="Reset your FoodShare password",
            recipients=[email],
        )
        msg.body = f"""
Hello,

You requested to reset your FoodShare password.

Click the link below to reset it:
{reset_link}

This link will expire in 30 minutes.

If you did not request this, please ignore this email.

– FoodShare Security Team
"""
        mail.send(msg)
        log_audit(
            "forgot_password_requested",
            user_id=user_id,
            entity_type="user",
            entity_id=user_id,
            metadata={"email": email, "account_exists": True},
            req=request,
        )
        return jsonify({
            "message": "If the account exists, a reset link will be sent"
        }), 200

    except Exception:
        logger.exception("🔥 Forgot password failed")
        # Still return generic success (never expose errors here)
        return jsonify({
            "message": "If the account exists, a reset link will be sent"
        }), 200



@auth_bp.route("/auth/reset-password", methods=["POST"])
def reset_password():
    data = request.get_json()
    token = data.get("token")
    new_password = data.get("password")

    if not token or not new_password:
        return jsonify({"error": "Invalid request"}), 400

    token_hash = hashlib.sha256(token.encode()).hexdigest()

    reset_res = (
        supabase.table("password_resets")
        .select("id, user_id, expires_at, used")
        .eq("token_hash", token_hash)
        .maybe_single()
        .execute()
    )

    if not reset_res.data:
        return jsonify({"error": "Invalid or expired token"}), 400

    reset = reset_res.data

    if reset["used"]:
        return jsonify({"error": "Token already used"}), 400

    expires_at = datetime.fromisoformat(reset["expires_at"])

    if expires_at < datetime.utcnow().replace(tzinfo=expires_at.tzinfo):


        return jsonify({"error": "Token expired"}), 400

    # Update password
    hashed_pw = generate_password_hash(new_password)

    supabase.table("users").update({
        "password_hash": hashed_pw
    }).eq("id", reset["user_id"]).execute()

    # Mark token as used
    supabase.table("password_resets").update({
        "used": True
    }).eq("id", reset["id"]).execute()

    log_audit(
        "password_reset_completed",
        user_id=reset["user_id"],
        entity_type="user",
        entity_id=reset["user_id"],
        metadata={"method": "forgot_password_token"},
        req=request,
    )

    return jsonify({"message": "Password reset successful"}), 200
