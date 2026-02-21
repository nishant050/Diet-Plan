"""
AI Service — handles requests to Groq and OpenRouter APIs for recipe info.
"""
import httpx
import os
from database import get_db


GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"


async def get_default_model():
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM ai_models WHERE is_default = 1 LIMIT 1")
        model = await cursor.fetchone()
        if not model:
            cursor = await db.execute("SELECT * FROM ai_models LIMIT 1")
            model = await cursor.fetchone()
        return model
    finally:
        await db.close()


async def get_all_models():
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM ai_models ORDER BY is_default DESC, created_at DESC")
        return await cursor.fetchall()
    finally:
        await db.close()


async def get_cached_recipe(dish_name: str, model_id: str):
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT recipe_html FROM ai_recipe_cache WHERE dish_name = ? AND model_id = ?",
            (dish_name.lower().strip(), model_id)
        )
        row = await cursor.fetchone()
        return row[0] if row else None
    finally:
        await db.close()


async def cache_recipe(dish_name: str, model_id: str, recipe_html: str):
    db = await get_db()
    try:
        await db.execute(
            "INSERT OR REPLACE INTO ai_recipe_cache (dish_name, model_id, recipe_html) VALUES (?, ?, ?)",
            (dish_name.lower().strip(), model_id, recipe_html)
        )
        await db.commit()
    finally:
        await db.close()


def build_prompt(dish_name: str) -> str:
    return f"""You are a nutrition expert and chef. Provide detailed information about the dish: "{dish_name}"

Please provide the following in clean HTML format (no markdown, use HTML tags):

<h3>📋 Recipe: {dish_name}</h3>

<h4>🥗 Nutritional Information (per serving)</h4>
<ul>
<li>Calories: estimated kcal</li>
<li>Protein: g</li>
<li>Carbohydrates: g</li>
<li>Fat: g</li>
<li>Fiber: g</li>
<li>Sugar: g</li>
</ul>

<h4>🛒 Ingredients</h4>
<ul>List all ingredients with quantities</ul>

<h4>👨‍🍳 Preparation Steps</h4>
<ol>Step by step cooking instructions</ol>

<h4>⏱️ Cooking Time</h4>
<p>Prep time, cook time, total time</p>

<h4>💡 Health Benefits</h4>
<ul>Key health benefits of this dish</ul>

<h4>⚠️ Dietary Notes</h4>
<p>Any allergens, dietary restrictions info (vegan, gluten-free, etc.)</p>

Keep the response concise but informative. Use clean, well-formatted HTML only."""


async def query_ai(dish_name: str) -> str:
    model = await get_default_model()
    if not model:
        return "<p class='error'>No AI model configured. Ask admin to add one.</p>"

    # Check cache first
    cached = await get_cached_recipe(dish_name, model["model_id"])
    if cached:
        return cached

    provider = model["provider"]
    model_id = model["model_id"]
    api_key = model["api_key"] or ""

    # Determine API URL and key
    if provider == "groq":
        api_url = GROQ_API_URL
        api_key = api_key or os.environ.get("GROQ_API_KEY", "")
    elif provider == "openrouter":
        api_url = OPENROUTER_API_URL
        api_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
    else:
        return "<p class='error'>Unknown AI provider configured.</p>"

    if not api_key:
        return f"<p class='error'>API key not configured for {provider}. Set the API key in admin settings or environment variable.</p>"

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    if provider == "openrouter":
        headers["HTTP-Referer"] = os.environ.get("APP_URL", "http://localhost:8000")
        headers["X-Title"] = "Diet Plan Dashboard"

    payload = {
        "model": model_id,
        "messages": [
            {"role": "system", "content": "You are a nutrition expert and professional chef. Respond only in clean HTML format."},
            {"role": "user", "content": build_prompt(dish_name)},
        ],
        "max_tokens": 2000,
        "temperature": 0.7,
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(api_url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
            content = data["choices"][0]["message"]["content"]

            # Cache the result
            await cache_recipe(dish_name, model_id, content)
            return content
    except httpx.TimeoutException:
        return "<p class='error'>AI service timed out. Please try again.</p>"
    except httpx.HTTPStatusError as e:
        return f"<p class='error'>AI service error: {e.response.status_code} — {e.response.text[:200]}</p>"
    except Exception as e:
        return f"<p class='error'>Error querying AI: {str(e)}</p>"
