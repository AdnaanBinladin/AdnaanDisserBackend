from flask import Blueprint, request, jsonify
import google.generativeai as genai
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

ai_bp = Blueprint("ai", __name__)


def _resolve_supported_model_name():
    preferred_models = [
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
        "gemini-2.0-flash",
        "gemini-2.0-flash-lite",
        "gemini-1.5-flash",
        "gemini-1.5-flash-8b",
        "gemini-1.5-pro",
    ]

    available_names = []
    for model in genai.list_models():
        methods = getattr(model, "supported_generation_methods", []) or []
        if "generateContent" not in methods:
            continue

        name = getattr(model, "name", "") or ""
        short_name = name.split("/", 1)[-1] if name else ""
        if short_name:
            available_names.append(short_name)

    for candidate in preferred_models:
        if candidate in available_names:
            return candidate

    if available_names:
        return available_names[0]

    raise ValueError("No Gemini model supporting generateContent is available")

@ai_bp.route("/ai/suggest-description", methods=["POST"])
def suggest_description():
    try:
        data = request.get_json() or {}
        title = data.get("title", "")
        category = data.get("category", "")
        quantity = data.get("quantity", "")

        api_key = os.getenv("YOUR_GEMINI_KEY")
        if not api_key:
            raise ValueError("Missing YOUR_GEMINI_KEY in .env file")

        genai.configure(api_key=api_key)
        model_name = _resolve_supported_model_name()
        model = genai.GenerativeModel(model_name)

        prompt = (
            f"Write a short, kind description for a food donation titled '{title}'. "
            f"It includes {quantity} {category}. Describe it naturally for display to NGOs, not to the donor. "
            f"Avoid phrases like 'your donation'. Keep it under 25 words."
        )

        response = model.generate_content(prompt)

        suggestion = (getattr(response, "text", "") or "").strip()
        if not suggestion:
            raise ValueError("Gemini returned an empty response")

        return jsonify({"suggestion": suggestion, "model": model_name})

    except Exception as e:
        print("❌ AI Suggestion Error:", e)
        return jsonify({"error": str(e)}), 500
