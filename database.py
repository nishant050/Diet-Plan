"""
Database module — MongoDB with Motor for async operations.
Collections: admin, profiles, device_profile_map, meal_plans, meal_checks, ai_models, activity_logs, ai_recipe_cache
"""
import os
import motor.motor_asyncio
from datetime import datetime, date

MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
DB_NAME = os.environ.get("DB_NAME", "dietplan")

# Global variables to store the client and db instances
client = None
db = None

async def init_db():
    global client, db
    if client is None:
        client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URI)
        db = client[DB_NAME]
        
    # Create indexes for performance and uniqueness
    await db.admin.create_index("username", unique=True)
    await db.meal_plans.create_index("plan_date")
    await db.meal_plans.create_index("profile_id")
    await db.meal_checks.create_index([("profile_id", 1), ("meal_plan_id", 1)], unique=True)
    await db.meal_checks.create_index("profile_id")
    await db.activity_logs.create_index("profile_id")
    await db.device_profile_map.create_index("device_fingerprint")
    await db.ai_recipe_cache.create_index([("dish_name", 1), ("model_id", 1)], unique=True)

    # Seed default AI models
    if await db.ai_models.count_documents({}) == 0:
        await db.ai_models.insert_many([
            {
                "provider": "groq", 
                "model_id": "openai/gpt-oss-120b", 
                "display_name": "GPT-OSS 120B (Groq)", 
                "api_key": "",
                "is_default": 1,
                "created_at": datetime.utcnow()
            },
            {
                "provider": "openrouter",   
                "model_id": "arcee-ai/trinity-large-preview:free", 
                "display_name": "Trinity Large Preview (Free)", 
                "api_key": "",
                "is_default": 0,
                "created_at": datetime.utcnow()
            },
            {
                "provider": "gemini",
                "model_id": "gemini-3.1-flash-lite-preview",
                "display_name": "Gemini 3.1 Flash Lite Preview",
                "api_key": "",
                "is_default": 0,
                "created_at": datetime.utcnow()
            }
        ])

    # Seed default admin
    if await db.admin.count_documents({}) == 0:
        from passlib.hash import bcrypt
        hashed = bcrypt.hash("admin123")
        await db.admin.insert_one({
            "username": "admin", 
            "password_hash": hashed,
            "created_at": datetime.utcnow()
        })

async def get_db():
    """Dependency to get the database instance."""
    global db
    if db is None:
        await init_db()
    return db
