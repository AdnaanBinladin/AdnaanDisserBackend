from flask import Flask, request, jsonify, make_response
from flask_cors import CORS
from flask_mail import Message

from src.routes.ads_routes import ads_bp
from src.routes.auth_routes import auth_bp
from src.routes.donation_routes import donation_bp
from src.routes.notifications_routes import notifications_bp
from src.utils.mail_instance import mail
from apscheduler.schedulers.background import BackgroundScheduler
from src.routes.donation_routes import send_expiry_reminders
from src.routes.ai_routes import ai_bp
from src.routes.ngodashboard_routes import ngo_dashboard_bp
from src.routes.auth_routes import profile_bp
from dotenv import load_dotenv
from werkzeug.exceptions import HTTPException
import os
from src.routes.admin_routes import admin_bp

# Load environment variables
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(dotenv_path=os.path.join(BASE_DIR, ".env"))

app = Flask(__name__)

# Mail settings
app.config.update(
    MAIL_SERVER=os.getenv("MAIL_SERVER", "smtp.gmail.com"),
    MAIL_PORT=int(os.getenv("MAIL_PORT", 587)),
    MAIL_USE_TLS=os.getenv("MAIL_USE_TLS", "True").lower() == "true",
    MAIL_USERNAME=os.getenv("MAIL_USERNAME"),
    MAIL_PASSWORD=os.getenv("MAIL_PASSWORD"),
    MAIL_DEFAULT_SENDER=os.getenv("MAIL_DEFAULT_SENDER", os.getenv("MAIL_USERNAME")),
)

mail.init_app(app)

# Allowed frontend origins
ALLOWED_ORIGINS = [
    "http://127.0.0.1:3000",
    "http://localhost:3000",
    "http://localhost:3001",
    "http://192.168.56.1:3001",
]

CORS(
    app,
    resources={r"/api/*": {"origins": ALLOWED_ORIGINS}},
    supports_credentials=True,
    allow_headers=["Content-Type", "Authorization", "X-Requested-With"],
    methods=["GET", "POST", "OPTIONS", "PUT", "DELETE", "PATCH"],
)

# Handle preflight requests
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

# Add CORS headers to responses
@app.after_request
def add_cors_headers(response):
    origin = request.headers.get("Origin")
    if origin in ALLOWED_ORIGINS:
        response.headers["Access-Control-Allow-Origin"] = origin
    response.headers["Access-Control-Allow-Credentials"] = "true"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type,Authorization,X-Requested-With"
    response.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS,PUT,DELETE,PATCH"
    return response

# Basic error handler
@app.errorhandler(Exception)
def handle_exception(e):
    if isinstance(e, HTTPException):
        return e

    origin = request.headers.get("Origin")
    response = jsonify({"error": str(e)})

    if origin in ALLOWED_ORIGINS:
        response.headers["Access-Control-Allow-Origin"] = origin

    response.headers["Access-Control-Allow-Credentials"] = "true"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type,Authorization,X-Requested-With"
    response.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS,PUT,DELETE,PATCH"
    response.status_code = 500

    return response

# Routes
app.register_blueprint(auth_bp, url_prefix="/api")
app.register_blueprint(donation_bp, url_prefix="/api")
app.register_blueprint(notifications_bp, url_prefix="/api")
app.register_blueprint(ai_bp, url_prefix="/api")
app.register_blueprint(ngo_dashboard_bp, url_prefix="/api")
app.register_blueprint(profile_bp, url_prefix="/api")
app.register_blueprint(admin_bp, url_prefix="/api")
app.register_blueprint(ads_bp, url_prefix="/api")


@app.route("/")
def home():
    return jsonify({"message": "FoodShare backend is running."})

# Reminder job
def start_scheduler():
    scheduler = BackgroundScheduler()
    scheduler.add_job(send_expiry_reminders, "interval", hours=24)
    scheduler.start()
    print("Reminder scheduler started.")


if __name__ == "__main__":
    print("Registered routes:")
    for rule in app.url_map.iter_rules():
        print(rule)

    app.run(
        host="0.0.0.0",
        port=5050,
        debug=True,
        use_reloader=False
    )
