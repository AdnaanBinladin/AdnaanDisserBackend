from flask import Blueprint, request, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
from src.services.supabase_service import supabase
from postgrest.exceptions import APIError
import jwt
import os
import datetime
import logging
import random
from src.utils.password_utils import validate_password_strength
from src.utils.jwt import decode_jwt
from flask_mail import Message
from src.utils.mail_instance import mail


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger(__name__)


auth_bp = Blueprint("auth", __name__)
profile_bp = Blueprint("profile", __name__)

JWT_SECRET = os.getenv("JWT_SECRET")

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

        # ✅ Validate common fields
        if not all([email, password, full_name, role]):
            return jsonify({"error": "Missing required fields"}), 400

        # ✅ Hash password
        hashed_pw = generate_password_hash(password)

        # ✅ Step 1: Insert into 'users' table
        user_response = supabase.table("users").insert({
            "email": email,
            "password_hash": hashed_pw,
            "full_name": full_name,
            "phone": phone,
            "role": role,
            "status": "active"
        }).execute()

        if not user_response.data:
            return jsonify({"error": "Failed to register user"}), 500

        user_id = user_response.data[0]["id"]

        # ✅ Step 2: If NGO, create record in 'organizations'
        if role == "ngo":
            if not all([address, description, phone]):
                return jsonify({"error": "Missing NGO organization fields"}), 400

            org_response = supabase.table("organizations").insert({
                "user_id": user_id,
                "name": full_name,
                "address": address,
                "description": description,
                "phone": phone,
                "verification_status": "pending"
            }).execute()

            if not org_response.data:
                return jsonify({"error": "Failed to create organization record"}), 500

            # ✅ Optional: Create welcome notification for NGO
            try:
                supabase.table("notifications").insert({
                    "user_id": user_id,
                    "title": "NGO Registration Pending Review 🕒",
                    "message": f"Your organization '{full_name}' has been submitted and is awaiting admin approval.",
                    "type": "status_update",
                    "read": False,
                    "created_at": datetime.datetime.utcnow().isoformat()
                }).execute()
            except Exception as notif_err:
                print("⚠️ Failed to create NGO notification:", notif_err)

        # ✅ Return success
        return jsonify({
            "message": "User registered successfully",
            "user_id": user_id
        }), 201

    except APIError as e:
        error_message = str(e).lower()
        if "duplicate key" in error_message and "email" in error_message:
            return jsonify({"error": "Email already exists"}), 409
        return jsonify({"error": "Database error occurred"}), 500

    except Exception as e:
        print("⚠️ Registration Error:", e)
        return jsonify({"error": str(e)}), 500


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
            return jsonify({"error": "Email and password are required"}), 400

        # ✅ Query user by email
        user = supabase.table("users").select("*").eq("email", email).single().execute()

        if not user.data:
            return jsonify({"error": "Invalid email or password"}), 401

        # ✅ Verify password hash
        if not check_password_hash(user.data["password_hash"], password):
            return jsonify({"error": "Invalid email or password"}), 401

        # ✅ Generate JWT token
        token = jwt.encode({
            "user_id": user.data["id"],
            "email": user.data["email"],
            "role": user.data["role"],
            "exp": datetime.datetime.utcnow() + datetime.timedelta(hours=12)
        }, JWT_SECRET, algorithm="HS256")

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
        print("⚠️ Login Error:", e)
        return jsonify({"error": str(e)}), 500


# ────────────────────────────────────────────────────────────────
# 🧾 GET DONOR PROFILE BY ID
# ────────────────────────────────────────────────────────────────
@auth_bp.route("/donors/<donor_id>", methods=["GET"])
def get_donor_profile(donor_id):
    try:
        response = supabase.table("users").select(
            "id, full_name, email, phone, role"
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
        user_id = request.args.get("userId")

        print("🔍 PROFILE FETCH userId =", user_id)

        if not user_id:
            return jsonify({"error": "Missing userId"}), 400

        user_res = supabase.table("users") \
            .select("id, full_name, email, phone, role") \
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
        data = request.get_json()
        logger.info(f"PATCH /profile payload: {data}")

        user_id = data.get("userId")

        if not user_id:
            logger.warning("PATCH /profile missing userId")
            return jsonify({"error": "Missing userId"}), 400

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

    # 5️⃣ Validate new password strength
    strength_error = validate_password_strength(new_password)
    if strength_error:
        return jsonify({"error": strength_error}), 400

    # 6️⃣ Generate OTP
    otp = str(random.randint(100000, 999999))
    otp_hash = generate_password_hash(otp)
    expires_at = datetime.datetime.utcnow() + datetime.timedelta(minutes=10)

    # 7️⃣ Store OTP
    supabase.table("password_change_codes").insert({
        "user_id": user_id,
        "code_hash": otp_hash,
        "expires_at": expires_at.isoformat(),
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

    return jsonify({
        "message": "Verification code sent to your email"
    }), 200
