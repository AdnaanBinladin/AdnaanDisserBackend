import os
from dotenv import load_dotenv
from supabase import create_client, Client

# ✅ Load the .env file (this must come before reading environment variables)
load_dotenv(dotenv_path=os.path.join(os.getcwd(), ".env"))

# ✅ Get values from environment
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# ✅ Sanity check for debugging
if not SUPABASE_URL:
    raise ValueError("❌ SUPABASE_URL is missing — check your .env path")
if not SUPABASE_KEY:
    raise ValueError("❌ SUPABASE_KEY is missing — check your .env path")

print("✅ Loaded Supabase credentials")
print(f"SUPABASE_URL: {SUPABASE_URL}")
print(f"SUPABASE_KEY present: {bool(SUPABASE_KEY)}")

# ✅ Create Supabase client
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
