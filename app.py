from flask import Flask, request, jsonify, make_response
from flask_cors import CORS
from flask_mail import Message

from src.routes.ads_routes import ads_bp
from src.routes.auth_routes import auth_bp
from src.routes.donation_routes import donation_bp
from src.routes.notifications_routes import notifications_bp
from src.utils.mail_instance import mail   # ✅ new: shared Mail instance
from apscheduler.schedulers.background import BackgroundScheduler
from src.routes.donation_routes import send_expiry_reminders
from src.routes.ai_routes import ai_bp
from src.routes.ngodashboard_routes import ngo_dashboard_bp
from src.routes.auth_routes import profile_bp
from dotenv import load_dotenv
from werkzeug.exceptions import HTTPException
import os

# ==========================================
# 🔹 Load environment variables
# ==========================================
load_dotenv(dotenv_path=os.path.join(os.getcwd(), ".env"))

app = Flask(__name__)

# ==========================================
# 🔹 Email configuration (from .env)
# ==========================================
app.config.update(
    MAIL_SERVER=os.getenv("MAIL_SERVER", "smtp.gmail.com"),
    MAIL_PORT=int(os.getenv("MAIL_PORT", 587)),
    MAIL_USE_TLS=os.getenv("MAIL_USE_TLS", "True").lower() == "true",
    MAIL_USERNAME=os.getenv("MAIL_USERNAME"),
    MAIL_PASSWORD=os.getenv("MAIL_PASSWORD"),
    MAIL_DEFAULT_SENDER=os.getenv("MAIL_DEFAULT_SENDER", os.getenv("MAIL_USERNAME")),
)

# ✅ Initialize Flask-Mail
mail.init_app(app)

# ==========================================
# 🔹 Allowed origins (for CORS)
# ==========================================
ALLOWED_ORIGINS = [
    "http://127.0.0.1:3000",
    "http://localhost:3000",
    "http://localhost:3001",
    "http://192.168.56.1:3001",
]

# ✅ Configure Flask-CORS
CORS(
    app,
    resources={r"/api/*": {"origins": ALLOWED_ORIGINS}},
    supports_credentials=True,
    allow_headers=["Content-Type", "Authorization", "X-Requested-With"],
    methods=["GET", "POST", "OPTIONS", "PUT", "DELETE", "PATCH"],
)

# ==========================================
# 🔹 Preflight handler
# ==========================================
@app.before_request
def handle_preflight():
    if request.method == "OPTIONS":
        origin = request.headers.get("Origin", "")
        if origin in ALLOWED_ORIGINS:
            response = make_response()
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS,PUT,DELETE,PATCH"
            response.headers["Access-Control-Allow-Headers"] = "Content-Type,Authorization,X-Requested-With"
            response.headers["Access-Control-Allow-Credentials"] = "true"
            response.status_code = 200
            return response
    return None

# ==========================================
# 🔹 Apply CORS headers to ALL responses
# ==========================================
@app.after_request
def add_cors_headers(response):
    origin = request.headers.get("Origin")
    if origin in ALLOWED_ORIGINS:
        response.headers["Access-Control-Allow-Origin"] = origin
    response.headers["Access-Control-Allow-Credentials"] = "true"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type,Authorization,X-Requested-With"
    response.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS,PUT,DELETE,PATCH"
    return response

# ==========================================
# 🔹 Global error handler (REAL errors only)
# ==========================================
@app.errorhandler(Exception)
def handle_exception(e):
    # ✅ Let Flask handle normal HTTP errors (404, 405, etc.)
    if isinstance(e, HTTPException):
        return e

    # ❌ Only catch REAL server crashes
    origin = request.headers.get("Origin")
    response = jsonify({"error": str(e)})

    if origin in ALLOWED_ORIGINS:
        response.headers["Access-Control-Allow-Origin"] = origin

    response.headers["Access-Control-Allow-Credentials"] = "true"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type,Authorization,X-Requested-With"
    response.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS,PUT,DELETE,PATCH"
    response.status_code = 500

    return response

# ==========================================
# 🔹 Register blueprints
# ==========================================
app.register_blueprint(auth_bp, url_prefix="/api")
app.register_blueprint(donation_bp, url_prefix="/api")
app.register_blueprint(notifications_bp, url_prefix="/api")
app.register_blueprint(ai_bp, url_prefix="/api")
app.register_blueprint(ngo_dashboard_bp, url_prefix="/api")
app.register_blueprint(profile_bp, url_prefix="/api")
app.register_blueprint(ads_bp, url_prefix="/api")


# ==========================================
# 🔹 Home route
# ==========================================
@app.route("/")
def home():
    return jsonify({"message": "🥦 FoodShare backend is running with full CORS + email support!"})

# ==========================================
# 🔹 Scheduler (runs after app + mail are initialized)
# ==========================================
def start_scheduler():
    scheduler = BackgroundScheduler()
    scheduler.add_job(send_expiry_reminders, "interval", hours=24)
    scheduler.start()
    print("🕒 Reminder scheduler started (every 24h).")

# ==========================================
# 🔹 Run server
# ==========================================
if __name__ == "__main__":
    print("✅ Registered routes:")
    for rule in app.url_map.iter_rules():
        print(rule)

    app.run(
        host="0.0.0.0",
        port=5050,
        debug=True,
        use_reloader=False
    )
