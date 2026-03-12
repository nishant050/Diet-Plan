"""
AI Service — handles requests to Groq and OpenRouter APIs for recipe info.
"""
import httpx
import os
from database import get_db


GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
GEMINI_API_URL_TEMPLATE = "https://generativelanguage.googleapis.com/v1beta/models/{model_id}:generateContent?key={api_key}"


async def get_default_model():
    db = await get_db()
    model = await db.ai_models.find_one({"is_default": 1})
    if not model:
        model = await db.ai_models.find_one({})
    return model


async def get_all_models():
    db = await get_db()
    cursor = db.ai_models.find({}).sort([("is_default", -1), ("created_at", -1)])
    return await cursor.to_list(length=None)


async def get_cached_recipe(dish_name: str, model_id: str):
    db = await get_db()
    doc = await db.ai_recipe_cache.find_one({
        "dish_name": dish_name.lower().strip(),
        "model_id": model_id
    })
    return doc["recipe_html"] if doc else None


async def cache_recipe(dish_name: str, model_id: str, recipe_html: str):
    db = await get_db()
    await db.ai_recipe_cache.update_one(
        {"dish_name": dish_name.lower().strip(), "model_id": model_id},
        {"$set": {"recipe_html": recipe_html}},
        upsert=True
    )


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

<h4>🔗 References & Tutorials</h4>
<ul>
<li><a href="#" target="_blank">Source Recipe</a> (if available)</li>
<li><a href="#" target="_blank">YouTube Video Tutorial</a></li>
</ul>

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
    api_key = model.get("api_key", "")

    # Determine API URL and key
    if provider == "groq":
        api_url = GROQ_API_URL
        api_key = api_key or os.environ.get("GROQ_API_KEY", "")
    elif provider == "openrouter":
        api_url = OPENROUTER_API_URL
        api_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
    elif provider == "gemini":
        api_key = api_key or os.environ.get("GEMINI_API_KEY", "")
        # Gemini injects key in URL
        api_url = GEMINI_API_URL_TEMPLATE.format(model_id=model_id, api_key=api_key)
    else:
        return "<p class='error'>Unknown AI provider configured.</p>"

    if not api_key:
        return f"<p class='error'>API key not configured for {provider}. Set the API key in admin settings or environment variable.</p>"

    headers = {
        "Content-Type": "application/json",
    }

    if provider in ("groq", "openrouter"):
        headers["Authorization"] = f"Bearer {api_key}"
    if provider == "openrouter":
        headers["HTTP-Referer"] = os.environ.get("APP_URL", "http://localhost:8000")
        headers["X-Title"] = "Diet Plan Dashboard"

    if provider == "gemini":
        payload = {
            "contents": [{"parts": [{"text": build_prompt(dish_name)}]}],
            "systemInstruction": {"parts": [{"text": "You are a nutrition expert and professional chef. Respond only in clean HTML format. Use Google Search to find an authentic recipe and a valid YouTube video tutorial link."}]},
            "generationConfig": {"temperature": 0.5, "maxOutputTokens": 2048},
            "tools": [{"googleSearch": {}}]
        }
    else:
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
        async with httpx.AsyncClient(timeout=45.0) as client:
            response = await client.post(api_url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
            
            if provider == "gemini":
                content = data["candidates"][0]["content"]["parts"][0]["text"]
            else:
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
