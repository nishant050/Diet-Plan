"""
Database module — SQLite with aiosqlite for async operations.
Tables: users, profiles, meal_plans, meals, meal_checks, ai_models, activity_logs
"""
import aiosqlite
import os
from datetime import datetime, date

DB_PATH = os.environ.get("DB_PATH", "dietplan.db")


async def get_db():
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    return db


async def init_db():
    db = await get_db()
    try:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS admin (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                device_fingerprint TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS device_profile_map (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_fingerprint TEXT NOT NULL,
                profile_id INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (profile_id) REFERENCES profiles(id)
            );

            CREATE TABLE IF NOT EXISTS meal_plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_date DATE NOT NULL,
                meal_type TEXT NOT NULL CHECK(meal_type IN ('breakfast', 'morning_snack', 'lunch', 'afternoon_snack', 'dinner', 'evening_snack')),
                dish_name TEXT NOT NULL,
                description TEXT DEFAULT '',
                calories INTEGER DEFAULT 0,
                protein_g REAL DEFAULT 0,
                carbs_g REAL DEFAULT 0,
                fat_g REAL DEFAULT 0,
                fiber_g REAL DEFAULT 0,
                profile_id INTEGER DEFAULT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (profile_id) REFERENCES profiles(id)
            );

            CREATE TABLE IF NOT EXISTS meal_checks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_id INTEGER NOT NULL,
                meal_plan_id INTEGER NOT NULL,
                is_prepared INTEGER DEFAULT 0,
                checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (profile_id) REFERENCES profiles(id),
                FOREIGN KEY (meal_plan_id) REFERENCES meal_plans(id),
                UNIQUE(profile_id, meal_plan_id)
            );

            CREATE TABLE IF NOT EXISTS ai_models (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider TEXT NOT NULL CHECK(provider IN ('groq', 'openrouter')),
                model_id TEXT NOT NULL,
                display_name TEXT NOT NULL,
                api_key TEXT DEFAULT '',
                is_default INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS activity_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_id INTEGER,
                action TEXT NOT NULL,
                details TEXT DEFAULT '',
                device_fingerprint TEXT DEFAULT '',
                user_agent TEXT DEFAULT '',
                os_info TEXT DEFAULT '',
                browser_info TEXT DEFAULT '',
                device_type TEXT DEFAULT '',
                ip_address TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (profile_id) REFERENCES profiles(id)
            );

            CREATE TABLE IF NOT EXISTS ai_recipe_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dish_name TEXT NOT NULL,
                model_id TEXT NOT NULL,
                recipe_html TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(dish_name, model_id)
            );

            CREATE INDEX IF NOT EXISTS idx_meal_plans_date ON meal_plans(plan_date);
            CREATE INDEX IF NOT EXISTS idx_meal_plans_profile ON meal_plans(profile_id);
            CREATE INDEX IF NOT EXISTS idx_meal_checks_profile ON meal_checks(profile_id);
            CREATE INDEX IF NOT EXISTS idx_activity_logs_profile ON activity_logs(profile_id);
            CREATE INDEX IF NOT EXISTS idx_device_profile ON device_profile_map(device_fingerprint);
        """)
        await db.commit()

        # Migration: add profile_id column if missing (for existing databases)
        try:
            await db.execute("SELECT profile_id FROM meal_plans LIMIT 1")
        except Exception:
            await db.execute("ALTER TABLE meal_plans ADD COLUMN profile_id INTEGER DEFAULT NULL REFERENCES profiles(id)")
            await db.commit()

        # Seed default AI models
        cursor = await db.execute("SELECT COUNT(*) as cnt FROM ai_models")
        row = await cursor.fetchone()
        if row[0] == 0:
            await db.executemany(
                "INSERT INTO ai_models (provider, model_id, display_name, is_default) VALUES (?, ?, ?, ?)",
                [
                    ("groq", "openai/gpt-oss-120b", "GPT-OSS 120B (Groq)", 1),
                    ("openrouter", "arcee-ai/trinity-large-preview:free", "Trinity Large Preview (Free)", 0),
                ]
            )
            await db.commit()

        # Seed default admin
        cursor = await db.execute("SELECT COUNT(*) as cnt FROM admin")
        row = await cursor.fetchone()
        if row[0] == 0:
            from passlib.hash import bcrypt
            hashed = bcrypt.hash("admin123")
            await db.execute(
                "INSERT INTO admin (username, password_hash) VALUES (?, ?)",
                ("admin", hashed)
            )
            await db.commit()
    finally:
        await db.close()
