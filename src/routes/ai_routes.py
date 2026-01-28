from flask import Blueprint, request, jsonify
from google import genai
import os
from dotenv import load_dotenv

# ✅ Load environment variables
load_dotenv()

ai_bp = Blueprint("ai", __name__)

@ai_bp.route("/ai/suggest-description", methods=["POST"])
def suggest_description():
    try:
        data = request.get_json()
        title = data.get("title", "")
        category = data.get("category", "")
        quantity = data.get("quantity", "")

        # ✅ Use environment variable
        api_key = os.getenv("YOUR_GEMINI_KEY")
        if not api_key:
            raise ValueError("Missing GOOGLE_API_KEY in .env file")

        client = genai.Client(api_key=api_key)

        # ✅ Clear, structured prompt
        prompt = (
            f"Write a short, kind description for a food donation titled '{title}'. "
            f"It includes {quantity} {category}. Describe it naturally for display to NGOs, not to the donor. "
            f"Avoid phrases like 'your donation'. Keep it under 25 words."
        )

        # ✅ Use new Gemini call syntax
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt
        )

        suggestion = response.text.strip()
        return jsonify({"suggestion": suggestion})

    except Exception as e:
        print("❌ AI Suggestion Error:", e)
        return jsonify({"error": str(e)}), 500
