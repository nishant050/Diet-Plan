"""
Diet Plan Dashboard — Main Application
FastAPI + Jinja2 + HTMX
"""
import os
import io
import csv
import json
from datetime import datetime, date, timedelta
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Form, UploadFile, File, Response, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from passlib.hash import bcrypt

from database import init_db, get_db
from ai_service import query_ai, get_all_models


# ──────────────────────── App Setup ────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield

app = FastAPI(title="Diet Plan Dashboard", lifespan=lifespan)

# Static files & templates
os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")
templates.env.globals["timedelta"] = timedelta
templates.env.globals["date"] = date


# ──────────────────────── Helpers ────────────────────────
def parse_user_agent(ua: str) -> dict:
    """Basic user agent parsing"""
    os_info = "Unknown"
    browser = "Unknown"
    device = "Unknown"

    ua_lower = ua.lower()

    # OS detection
    if "windows" in ua_lower:
        os_info = "Windows"
    elif "mac os" in ua_lower or "macintosh" in ua_lower:
        os_info = "macOS"
    elif "linux" in ua_lower:
        os_info = "Linux"
    elif "android" in ua_lower:
        os_info = "Android"
    elif "iphone" in ua_lower or "ipad" in ua_lower:
        os_info = "iOS"

    # Browser detection
    if "edg/" in ua_lower:
        browser = "Edge"
    elif "chrome" in ua_lower and "safari" in ua_lower:
        browser = "Chrome"
    elif "firefox" in ua_lower:
        browser = "Firefox"
    elif "safari" in ua_lower:
        browser = "Safari"
    elif "opera" in ua_lower or "opr/" in ua_lower:
        browser = "Opera"

    # Device type
    if "mobile" in ua_lower or "android" in ua_lower or "iphone" in ua_lower:
        device = "Mobile"
    elif "tablet" in ua_lower or "ipad" in ua_lower:
        device = "Tablet"
    else:
        device = "Desktop"

    return {"os": os_info, "browser": browser, "device": device}


async def log_activity(profile_id, action, details, request: Request):
    db = await get_db()
    try:
        ua = request.headers.get("user-agent", "")
        ua_info = parse_user_agent(ua)
        fp = request.cookies.get("device_fp", "")
        ip = request.client.host if request.client else ""

        await db.execute(
            """INSERT INTO activity_logs 
               (profile_id, action, details, device_fingerprint, user_agent, os_info, browser_info, device_type, ip_address)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (profile_id, action, details, fp, ua, ua_info["os"], ua_info["browser"], ua_info["device"], ip)
        )
        await db.commit()
    finally:
        await db.close()


def get_admin_session(request: Request):
    return request.cookies.get("admin_session")


MEAL_TYPE_ORDER = ['breakfast', 'morning_snack', 'lunch', 'afternoon_snack', 'dinner', 'evening_snack']
MEAL_TYPE_LABELS = {
    'breakfast': '🌅 Breakfast',
    'morning_snack': '🍎 Morning Snack',
    'lunch': '☀️ Lunch',
    'afternoon_snack': '🫐 Afternoon Snack',
    'dinner': '🌙 Dinner',
    'evening_snack': '🌜 Evening Snack'
}


# ──────────────────────── User Routes ────────────────────────

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """Landing page — checks for known profile via cookie"""
    profile_id = request.cookies.get("profile_id")
    if profile_id:
        return RedirectResponse(url="/dashboard", status_code=302)
    return templates.TemplateResponse("welcome.html", {"request": request})


@app.post("/profile/new", response_class=HTMLResponse)
async def create_profile(request: Request, name: str = Form(...)):
    db = await get_db()
    try:
        cursor = await db.execute(
            "INSERT INTO profiles (name, device_fingerprint) VALUES (?, ?)",
            (name.strip(), request.cookies.get("device_fp", ""))
        )
        profile_id = cursor.lastrowid
        await db.commit()

        # Link device to profile
        fp = request.cookies.get("device_fp", "")
        if fp:
            await db.execute(
                "INSERT INTO device_profile_map (device_fingerprint, profile_id) VALUES (?, ?)",
                (fp, profile_id)
            )
            await db.commit()
    finally:
        await db.close()

    await log_activity(profile_id, "profile_created", f"New profile: {name}", request)

    response = RedirectResponse(url="/dashboard", status_code=302)
    response.set_cookie("profile_id", str(profile_id), max_age=365*24*3600, samesite="lax")
    return response


@app.get("/profiles/list", response_class=HTMLResponse)
async def list_profiles(request: Request):
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM profiles ORDER BY name")
        profiles = await cursor.fetchall()
    finally:
        await db.close()
    return templates.TemplateResponse("profiles_list.html", {"request": request, "profiles": profiles})


@app.post("/profile/select/{profile_id}")
async def select_profile(request: Request, profile_id: int):
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM profiles WHERE id = ?", (profile_id,))
        profile = await cursor.fetchone()
        if not profile:
            raise HTTPException(404, "Profile not found")

        # Link device
        fp = request.cookies.get("device_fp", "")
        if fp:
            await db.execute(
                "INSERT OR IGNORE INTO device_profile_map (device_fingerprint, profile_id) VALUES (?, ?)",
                (fp, profile_id)
            )
            await db.commit()
    finally:
        await db.close()

    await log_activity(profile_id, "profile_selected", f"Selected profile: {profile['name']}", request)

    response = RedirectResponse(url="/dashboard", status_code=302)
    response.set_cookie("profile_id", str(profile_id), max_age=365*24*3600, samesite="lax")
    return response


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, view_date: str = None):
    profile_id = request.cookies.get("profile_id")
    if not profile_id:
        return RedirectResponse(url="/", status_code=302)

    today = date.today()
    if view_date:
        try:
            selected_date = date.fromisoformat(view_date)
        except ValueError:
            selected_date = today
    else:
        selected_date = today

    db = await get_db()
    try:
        # Get profile
        cursor = await db.execute("SELECT * FROM profiles WHERE id = ?", (int(profile_id),))
        profile = await cursor.fetchone()
        if not profile:
            response = RedirectResponse(url="/", status_code=302)
            response.delete_cookie("profile_id")
            return response

        # If no specific date requested, check if today has meals; if not, find nearest date
        if not view_date:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM meal_plans WHERE plan_date = ? AND (profile_id IS NULL OR profile_id = ?)",
                (today.isoformat(), int(profile_id))
            )
            count = (await cursor.fetchone())[0]
            if count == 0:
                # Find nearest date with meals for this user
                cursor = await db.execute(
                    """SELECT plan_date FROM meal_plans 
                       WHERE profile_id IS NULL OR profile_id = ?
                       ORDER BY ABS(julianday(plan_date) - julianday(?)) ASC 
                       LIMIT 1""",
                    (int(profile_id), today.isoformat())
                )
                nearest = await cursor.fetchone()
                if nearest:
                    selected_date = date.fromisoformat(nearest[0])

        # Get meals for the day (user's meals + global meals)
        cursor = await db.execute(
            "SELECT * FROM meal_plans WHERE plan_date = ? AND (profile_id IS NULL OR profile_id = ?) ORDER BY meal_type",
            (selected_date.isoformat(), int(profile_id))
        )
        meals = await cursor.fetchall()

        # Get checked meals
        cursor = await db.execute(
            "SELECT meal_plan_id FROM meal_checks WHERE profile_id = ? AND is_prepared = 1",
            (int(profile_id),)
        )
        checked_ids = {row[0] for row in await cursor.fetchall()}

        # Organize meals by type
        organized = {}
        for mt in MEAL_TYPE_ORDER:
            organized[mt] = {
                "label": MEAL_TYPE_LABELS[mt],
                "meals": []
            }

        for meal in meals:
            mt = meal["meal_type"]
            if mt in organized:
                organized[mt]["meals"].append({
                    "id": meal["id"],
                    "dish_name": meal["dish_name"],
                    "description": meal["description"],
                    "calories": meal["calories"],
                    "protein_g": meal["protein_g"],
                    "carbs_g": meal["carbs_g"],
                    "fat_g": meal["fat_g"],
                    "fiber_g": meal["fiber_g"],
                    "is_checked": meal["id"] in checked_ids
                })

        # Compute total calories for the day
        total_cal = sum(m["calories"] for m in meals)
        checked_cal = sum(m["calories"] for m in meals if m["id"] in checked_ids)

    finally:
        await db.close()

    await log_activity(int(profile_id), "page_view", f"Dashboard for {selected_date.isoformat()}", request)

    # Navigation dates
    prev_date = (selected_date - timedelta(days=1)).isoformat()
    next_date = (selected_date + timedelta(days=1)).isoformat()
    is_today = selected_date == today

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "profile": profile,
        "meals": organized,
        "selected_date": selected_date,
        "today": today,
        "prev_date": prev_date,
        "next_date": next_date,
        "is_today": is_today,
        "total_calories": total_cal,
        "checked_calories": checked_cal,
        "meal_type_order": MEAL_TYPE_ORDER,
    })


@app.post("/meal/toggle/{meal_plan_id}", response_class=HTMLResponse)
async def toggle_meal(request: Request, meal_plan_id: int):
    profile_id = request.cookies.get("profile_id")
    if not profile_id:
        return HTMLResponse("<span>Error</span>", status_code=401)

    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT * FROM meal_checks WHERE profile_id = ? AND meal_plan_id = ?",
            (int(profile_id), meal_plan_id)
        )
        existing = await cursor.fetchone()

        if existing:
            new_val = 0 if existing["is_prepared"] else 1
            await db.execute(
                "UPDATE meal_checks SET is_prepared = ?, checked_at = CURRENT_TIMESTAMP WHERE id = ?",
                (new_val, existing["id"])
            )
        else:
            new_val = 1
            await db.execute(
                "INSERT INTO meal_checks (profile_id, meal_plan_id, is_prepared) VALUES (?, ?, 1)",
                (int(profile_id), meal_plan_id)
            )
        await db.commit()

        # Get dish name for logging
        cursor = await db.execute("SELECT dish_name FROM meal_plans WHERE id = ?", (meal_plan_id,))
        mp = await cursor.fetchone()
        dish = mp["dish_name"] if mp else "Unknown"
    finally:
        await db.close()

    action = "meal_prepared" if new_val else "meal_unprepared"
    await log_activity(int(profile_id), action, dish, request)

    checked = "checked" if new_val else ""
    icon = "✅" if new_val else "⬜"
    return HTMLResponse(f"""<span class="check-icon">{icon}</span>""")


@app.get("/dish/info/{meal_plan_id}", response_class=HTMLResponse)
async def dish_info(request: Request, meal_plan_id: int):
    profile_id = request.cookies.get("profile_id")

    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM meal_plans WHERE id = ?", (meal_plan_id,))
        meal = await cursor.fetchone()
        if not meal:
            return HTMLResponse("<p>Dish not found</p>")
    finally:
        await db.close()

    if profile_id:
        await log_activity(int(profile_id), "dish_viewed", meal["dish_name"], request)

    # Query AI
    result = await query_ai(meal["dish_name"])
    return HTMLResponse(f"""
        <div class="dish-detail-content">
            <div class="dish-detail-header">
                <h2>{meal["dish_name"]}</h2>
                <p class="dish-description">{meal["description"]}</p>
            </div>
            <div class="ai-content">
                {result}
            </div>
        </div>
    """)


@app.get("/history", response_class=HTMLResponse)
async def history(request: Request, week_offset: int = 0):
    profile_id = request.cookies.get("profile_id")
    if not profile_id:
        return RedirectResponse(url="/", status_code=302)

    today = date.today()
    # Calculate week start (Monday)
    start_of_week = today - timedelta(days=today.weekday()) + timedelta(weeks=week_offset)
    end_of_week = start_of_week + timedelta(days=6)

    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM profiles WHERE id = ?", (int(profile_id),))
        profile = await cursor.fetchone()

        # Get all meals for the week (user's meals + global meals)
        cursor = await db.execute(
            "SELECT * FROM meal_plans WHERE plan_date BETWEEN ? AND ? AND (profile_id IS NULL OR profile_id = ?) ORDER BY plan_date, meal_type",
            (start_of_week.isoformat(), end_of_week.isoformat(), int(profile_id))
        )
        meals = await cursor.fetchall()

        # Get checked meals
        cursor = await db.execute(
            "SELECT meal_plan_id FROM meal_checks WHERE profile_id = ? AND is_prepared = 1",
            (int(profile_id),)
        )
        checked_ids = {row[0] for row in await cursor.fetchall()}

        # Organize by day
        days = {}
        for i in range(7):
            d = start_of_week + timedelta(days=i)
            days[d.isoformat()] = {
                "date": d,
                "day_name": d.strftime("%A"),
                "meals": [],
                "total": 0,
                "prepared": 0,
            }

        for meal in meals:
            d_key = meal["plan_date"]
            if d_key in days:
                is_checked = meal["id"] in checked_ids
                days[d_key]["meals"].append({
                    "dish_name": meal["dish_name"],
                    "meal_type": MEAL_TYPE_LABELS.get(meal["meal_type"], meal["meal_type"]),
                    "calories": meal["calories"],
                    "is_checked": is_checked,
                })
                days[d_key]["total"] += 1
                if is_checked:
                    days[d_key]["prepared"] += 1

    finally:
        await db.close()

    await log_activity(int(profile_id), "history_view", f"Week of {start_of_week.isoformat()}", request)

    return templates.TemplateResponse("history.html", {
        "request": request,
        "profile": profile,
        "days": days,
        "start_of_week": start_of_week,
        "end_of_week": end_of_week,
        "prev_offset": week_offset - 1,
        "next_offset": week_offset + 1,
        "current_offset": week_offset,
        "today": today,
    })


@app.get("/profile/switch")
async def switch_profile(request: Request):
    response = RedirectResponse(url="/", status_code=302)
    response.delete_cookie("profile_id")
    return response


# ──────────────────────── Admin Routes ────────────────────────

@app.get("/admin/login", response_class=HTMLResponse)
async def admin_login_page(request: Request):
    if get_admin_session(request):
        return RedirectResponse(url="/admin", status_code=302)
    return templates.TemplateResponse("admin_login.html", {"request": request})


@app.post("/admin/login")
async def admin_login(request: Request, username: str = Form(...), password: str = Form(...)):
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM admin WHERE username = ?", (username,))
        admin = await cursor.fetchone()
        if not admin or not bcrypt.verify(password, admin["password_hash"]):
            return templates.TemplateResponse("admin_login.html", {
                "request": request,
                "error": "Invalid credentials"
            })
    finally:
        await db.close()

    response = RedirectResponse(url="/admin", status_code=302)
    response.set_cookie("admin_session", "authenticated", max_age=8*3600, httponly=True, samesite="lax")
    return response


@app.get("/admin/logout")
async def admin_logout():
    response = RedirectResponse(url="/admin/login", status_code=302)
    response.delete_cookie("admin_session")
    return response


@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(request: Request):
    if not get_admin_session(request):
        return RedirectResponse(url="/admin/login", status_code=302)

    db = await get_db()
    try:
        # Stats
        cursor = await db.execute("SELECT COUNT(*) FROM profiles")
        total_users = (await cursor.fetchone())[0]

        cursor = await db.execute("SELECT COUNT(*) FROM meal_plans")
        total_meals = (await cursor.fetchone())[0]

        cursor = await db.execute("SELECT COUNT(DISTINCT plan_date) FROM meal_plans")
        total_days = (await cursor.fetchone())[0]

        # Recent activity
        cursor = await db.execute("""
            SELECT al.*, p.name as profile_name 
            FROM activity_logs al 
            LEFT JOIN profiles p ON al.profile_id = p.id 
            ORDER BY al.created_at DESC LIMIT 50
        """)
        activities = await cursor.fetchall()

        # All profiles
        cursor = await db.execute("SELECT * FROM profiles ORDER BY name")
        profiles = await cursor.fetchall()

        # AI Models
        models = await get_all_models()

    finally:
        await db.close()

    return templates.TemplateResponse("admin_dashboard.html", {
        "request": request,
        "total_users": total_users,
        "total_meals": total_meals,
        "total_days": total_days,
        "activities": activities,
        "profiles": profiles,
        "models": models,
    })


@app.get("/admin/template/download")
async def download_template(request: Request):
    if not get_admin_session(request):
        return RedirectResponse(url="/admin/login", status_code=302)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "plan_date", "meal_type", "dish_name", "description",
        "calories", "protein_g", "carbs_g", "fat_g", "fiber_g"
    ])
    # Sample rows
    writer.writerow([
        "2026-02-22", "breakfast", "Oatmeal with Berries",
        "Warm oatmeal topped with fresh berries and honey",
        "350", "12", "55", "8", "6"
    ])
    writer.writerow([
        "2026-02-22", "lunch", "Grilled Chicken Salad",
        "Mixed greens with grilled chicken breast",
        "420", "35", "15", "22", "4"
    ])
    writer.writerow([
        "2026-02-22", "dinner", "Salmon with Vegetables",
        "Baked salmon fillet with roasted seasonal vegetables",
        "480", "38", "20", "28", "5"
    ])

    output.seek(0)
    return StreamingResponse(
        io.BytesIO(output.getvalue().encode()),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=meal_plan_template.csv"}
    )


@app.post("/admin/upload", response_class=HTMLResponse)
async def upload_meals(request: Request, file: UploadFile = File(...)):
    if not get_admin_session(request):
        return HTMLResponse("<p class='error'>Unauthorized</p>", status_code=401)

    content = await file.read()
    filename = file.filename.lower()

    rows = []
    errors = []

    try:
        if filename.endswith(".csv"):
            text = content.decode("utf-8-sig")
            reader = csv.DictReader(io.StringIO(text))
            for i, row in enumerate(reader, 2):
                rows.append(row)
        elif filename.endswith((".xlsx", ".xls")):
            import openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(content))
            ws = wb.active
            headers = [str(cell.value).strip().lower() for cell in ws[1]]
            for i, row in enumerate(ws.iter_rows(min_row=2, values_only=True), 2):
                if all(v is None for v in row):
                    continue
                rows.append(dict(zip(headers, row)))
        else:
            return HTMLResponse("""
                <div class="upload-result error">
                    <span class="icon">❌</span>
                    <p>Unsupported file format. Use CSV or XLSX.</p>
                </div>
            """)
    except Exception as e:
        return HTMLResponse(f"""
            <div class="upload-result error">
                <span class="icon">❌</span>
                <p>Error reading file: {str(e)}</p>
            </div>
        """)

    valid_types = {'breakfast', 'morning_snack', 'lunch', 'afternoon_snack', 'dinner', 'evening_snack'}
    inserted = 0

    db = await get_db()
    try:
        for i, row in enumerate(rows, 2):
            try:
                plan_date = str(row.get("plan_date", "")).strip()
                meal_type = str(row.get("meal_type", "")).strip().lower()
                dish_name = str(row.get("dish_name", "")).strip()

                if not plan_date or not meal_type or not dish_name:
                    errors.append(f"Row {i}: Missing required fields")
                    continue

                if meal_type not in valid_types:
                    errors.append(f"Row {i}: Invalid meal_type '{meal_type}'")
                    continue

                # Validate date
                date.fromisoformat(plan_date)

                await db.execute(
                    """INSERT INTO meal_plans (plan_date, meal_type, dish_name, description, calories, protein_g, carbs_g, fat_g, fiber_g)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        plan_date,
                        meal_type,
                        dish_name,
                        str(row.get("description", "")).strip(),
                        int(float(row.get("calories", 0) or 0)),
                        float(row.get("protein_g", 0) or 0),
                        float(row.get("carbs_g", 0) or 0),
                        float(row.get("fat_g", 0) or 0),
                        float(row.get("fiber_g", 0) or 0),
                    )
                )
                inserted += 1
            except Exception as e:
                errors.append(f"Row {i}: {str(e)}")

        await db.commit()
    finally:
        await db.close()

    error_html = ""
    if errors:
        error_html = "<ul class='error-list'>" + "".join(f"<li>{e}</li>" for e in errors[:10]) + "</ul>"
        if len(errors) > 10:
            error_html += f"<p>...and {len(errors)-10} more errors</p>"

    return HTMLResponse(f"""
        <div class="upload-result {'success' if inserted > 0 else 'warning'}">
            <span class="icon">{'✅' if inserted > 0 else '⚠️'}</span>
            <p><strong>{inserted}</strong> meals uploaded successfully.</p>
            {f'<p class="error-count">{len(errors)} errors found:</p>{error_html}' if errors else ''}
        </div>
    """)


@app.get("/admin/meals", response_class=HTMLResponse)
async def admin_meals(request: Request, week_offset: int = 0, view_all: int = 0):
    if not get_admin_session(request):
        return RedirectResponse(url="/admin/login", status_code=302)

    today = date.today()

    db = await get_db()
    try:
        if view_all:
            cursor = await db.execute("SELECT COUNT(*) FROM meal_plans")
            total = (await cursor.fetchone())[0]
            cursor = await db.execute(
                "SELECT mp.*, p.name as assigned_to FROM meal_plans mp LEFT JOIN profiles p ON mp.profile_id = p.id ORDER BY plan_date ASC, meal_type"
            )
            meals = await cursor.fetchall()
            week_start = None
            week_end = None
        else:
            week_start = today - timedelta(days=today.weekday()) + timedelta(weeks=week_offset)
            week_end = week_start + timedelta(days=6)
            cursor = await db.execute(
                "SELECT COUNT(*) FROM meal_plans WHERE plan_date BETWEEN ? AND ?",
                (week_start.isoformat(), week_end.isoformat())
            )
            total = (await cursor.fetchone())[0]
            cursor = await db.execute(
                "SELECT mp.*, p.name as assigned_to FROM meal_plans mp LEFT JOIN profiles p ON mp.profile_id = p.id WHERE plan_date BETWEEN ? AND ? ORDER BY plan_date ASC, meal_type",
                (week_start.isoformat(), week_end.isoformat())
            )
            meals = await cursor.fetchall()

        # Get all profiles for assignment dropdown
        cursor = await db.execute("SELECT id, name FROM profiles ORDER BY name")
        profiles = await cursor.fetchall()
    finally:
        await db.close()

    return templates.TemplateResponse("admin_meals.html", {
        "request": request,
        "meals": meals,
        "total": total,
        "week_offset": week_offset,
        "week_start": week_start,
        "week_end": week_end,
        "view_all": view_all,
        "today": today,
        "meal_type_labels": MEAL_TYPE_LABELS,
        "meal_type_order": MEAL_TYPE_ORDER,
        "profiles": profiles,
    })


@app.delete("/admin/meal/{meal_id}", response_class=HTMLResponse)
async def delete_meal(request: Request, meal_id: int):
    if not get_admin_session(request):
        return HTMLResponse("Unauthorized", status_code=401)

    db = await get_db()
    try:
        await db.execute("DELETE FROM meal_checks WHERE meal_plan_id = ?", (meal_id,))
        await db.execute("DELETE FROM meal_plans WHERE id = ?", (meal_id,))
        await db.commit()
    finally:
        await db.close()

    return HTMLResponse("")


@app.delete("/admin/meals/clear-week", response_class=HTMLResponse)
async def clear_week(request: Request, week_start: str = Query(...), week_end: str = Query(...)):
    if not get_admin_session(request):
        return HTMLResponse("Unauthorized", status_code=401)

    db = await get_db()
    try:
        # Delete associated meal checks first
        await db.execute(
            "DELETE FROM meal_checks WHERE meal_plan_id IN (SELECT id FROM meal_plans WHERE plan_date BETWEEN ? AND ?)",
            (week_start, week_end)
        )
        cursor = await db.execute(
            "DELETE FROM meal_plans WHERE plan_date BETWEEN ? AND ?",
            (week_start, week_end)
        )
        deleted = cursor.rowcount
        await db.commit()
    finally:
        await db.close()

    return HTMLResponse(f'''
        <div class="upload-result success">
            <span class="icon">✅</span>
            <p>Cleared <strong>{deleted}</strong> meals from {week_start} to {week_end}</p>
            <p style="font-size:0.8rem; margin-top:0.5rem; color:var(--text-muted);">Refresh the page to see the updated table.</p>
        </div>
    ''')


@app.post("/admin/meal/add", response_class=HTMLResponse)
async def add_meal_manual(request: Request, plan_date: str = Form(...), meal_type: str = Form(...),
                          dish_name: str = Form(...), description: str = Form(""),
                          calories: int = Form(0), protein_g: float = Form(0),
                          carbs_g: float = Form(0), fat_g: float = Form(0), fiber_g: float = Form(0),
                          profile_id: int = Form(0)):
    if not get_admin_session(request):
        return HTMLResponse("Unauthorized", status_code=401)

    pid = profile_id if profile_id > 0 else None

    db = await get_db()
    try:
        cursor = await db.execute(
            """INSERT INTO meal_plans (plan_date, meal_type, dish_name, description, calories, protein_g, carbs_g, fat_g, fiber_g, profile_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (plan_date, meal_type, dish_name.strip(), description.strip(), calories, protein_g, carbs_g, fat_g, fiber_g, pid)
        )
        new_id = cursor.lastrowid
        await db.commit()

        # Get assigned name
        assigned_name = "Everyone"
        if pid:
            c2 = await db.execute("SELECT name FROM profiles WHERE id = ?", (pid,))
            p = await c2.fetchone()
            if p:
                assigned_name = p["name"]

        # Get all profiles for dropdown
        cursor = await db.execute("SELECT id, name FROM profiles ORDER BY name")
        profiles = await cursor.fetchall()
    finally:
        await db.close()

    # Build profile options
    profile_opts = '<option value="0"' + (' selected' if not pid else '') + '>👥 Everyone</option>'
    for p in profiles:
        sel = ' selected' if p['id'] == pid else ''
        profile_opts += f'<option value="{p["id"]}"{sel}>{p["name"]}</option>'

    return HTMLResponse(f"""
        <tr id="meal-row-{new_id}" class="new-row-flash">
            <td>
                <input type="date" value="{plan_date}" class="inline-input"
                       hx-post="/admin/meal/{new_id}/edit" hx-include="closest tr" hx-target="#meal-row-{new_id}" hx-swap="outerHTML"
                       name="plan_date">
            </td>
            <td>
                <select class="inline-input" name="meal_type"
                        hx-post="/admin/meal/{new_id}/edit" hx-include="closest tr" hx-target="#meal-row-{new_id}" hx-swap="outerHTML">
                    <option value="breakfast" {"selected" if meal_type == "breakfast" else ""}>🌅 Breakfast</option>
                    <option value="morning_snack" {"selected" if meal_type == "morning_snack" else ""}>🍎 Morning Snack</option>
                    <option value="lunch" {"selected" if meal_type == "lunch" else ""}>☀️ Lunch</option>
                    <option value="afternoon_snack" {"selected" if meal_type == "afternoon_snack" else ""}>🫐 Afternoon Snack</option>
                    <option value="dinner" {"selected" if meal_type == "dinner" else ""}>🌙 Dinner</option>
                    <option value="evening_snack" {"selected" if meal_type == "evening_snack" else ""}>🌜 Evening Snack</option>
                </select>
            </td>
            <td>
                <input type="text" value="{dish_name}" class="inline-input inline-name" name="dish_name"
                       hx-post="/admin/meal/{new_id}/edit" hx-include="closest tr" hx-target="#meal-row-{new_id}" hx-swap="outerHTML"
                       hx-trigger="change">
            </td>
            <td>
                <input type="number" value="{calories}" class="inline-input inline-num" name="calories"
                       hx-post="/admin/meal/{new_id}/edit" hx-include="closest tr" hx-target="#meal-row-{new_id}" hx-swap="outerHTML"
                       hx-trigger="change">
            </td>
            <td class="macros-cell">
                <input type="number" value="{protein_g}" class="inline-input inline-micro" name="protein_g" step="0.1" hx-post="/admin/meal/{new_id}/edit" hx-include="closest tr" hx-target="#meal-row-{new_id}" hx-swap="outerHTML" hx-trigger="change">
                <input type="number" value="{carbs_g}" class="inline-input inline-micro" name="carbs_g" step="0.1" hx-post="/admin/meal/{new_id}/edit" hx-include="closest tr" hx-target="#meal-row-{new_id}" hx-swap="outerHTML" hx-trigger="change">
                <input type="number" value="{fat_g}" class="inline-input inline-micro" name="fat_g" step="0.1" hx-post="/admin/meal/{new_id}/edit" hx-include="closest tr" hx-target="#meal-row-{new_id}" hx-swap="outerHTML" hx-trigger="change">
            </td>
            <td>
                <select class="inline-input inline-assign" name="profile_id"
                        hx-post="/admin/meal/{new_id}/edit" hx-include="closest tr" hx-target="#meal-row-{new_id}" hx-swap="outerHTML">
                    {profile_opts}
                </select>
            </td>
            <td class="actions-cell">
                <button class="btn btn-sm btn-copy" onclick="openCopyModal({new_id}, '{dish_name}')">📋</button>
                <button class="btn btn-sm btn-danger"
                        hx-delete="/admin/meal/{new_id}"
                        hx-target="#meal-row-{new_id}"
                        hx-swap="outerHTML"
                        hx-confirm="Delete this meal?">🗑️</button>
            </td>
        </tr>
    """)


@app.post("/admin/meal/{meal_id}/edit", response_class=HTMLResponse)
async def edit_meal(request: Request, meal_id: int,
                    plan_date: str = Form(...), meal_type: str = Form(...),
                    dish_name: str = Form(...), calories: int = Form(0),
                    protein_g: float = Form(0), carbs_g: float = Form(0), fat_g: float = Form(0),
                    profile_id: int = Form(0)):
    if not get_admin_session(request):
        return HTMLResponse("Unauthorized", status_code=401)

    pid = profile_id if profile_id > 0 else None

    db = await get_db()
    try:
        await db.execute(
            """UPDATE meal_plans SET plan_date = ?, meal_type = ?, dish_name = ?, 
               calories = ?, protein_g = ?, carbs_g = ?, fat_g = ?, profile_id = ?
               WHERE id = ?""",
            (plan_date, meal_type, dish_name.strip(), calories, protein_g, carbs_g, fat_g, pid, meal_id)
        )
        await db.commit()
        cursor = await db.execute(
            "SELECT mp.*, p.name as assigned_to FROM meal_plans mp LEFT JOIN profiles p ON mp.profile_id = p.id WHERE mp.id = ?",
            (meal_id,)
        )
        meal = await cursor.fetchone()

        # Get profiles for dropdown
        cursor = await db.execute("SELECT id, name FROM profiles ORDER BY name")
        profiles = await cursor.fetchall()
    finally:
        await db.close()

    if not meal:
        return HTMLResponse("")

    mt = meal["meal_type"]
    mpid = meal["profile_id"]

    profile_opts = '<option value="0"' + (' selected' if not mpid else '') + '>👥 Everyone</option>'
    for p in profiles:
        sel = ' selected' if p['id'] == mpid else ''
        profile_opts += f'<option value="{p["id"]}"{sel}>{p["name"]}</option>'

    return HTMLResponse(f"""
        <tr id="meal-row-{meal['id']}">
            <td>
                <input type="date" value="{meal['plan_date']}" class="inline-input"
                       hx-post="/admin/meal/{meal['id']}/edit" hx-include="closest tr" hx-target="#meal-row-{meal['id']}" hx-swap="outerHTML"
                       name="plan_date">
            </td>
            <td>
                <select class="inline-input" name="meal_type"
                        hx-post="/admin/meal/{meal['id']}/edit" hx-include="closest tr" hx-target="#meal-row-{meal['id']}" hx-swap="outerHTML">
                    <option value="breakfast" {"selected" if mt == "breakfast" else ""}>🌅 Breakfast</option>
                    <option value="morning_snack" {"selected" if mt == "morning_snack" else ""}>🍎 Morning Snack</option>
                    <option value="lunch" {"selected" if mt == "lunch" else ""}>☀️ Lunch</option>
                    <option value="afternoon_snack" {"selected" if mt == "afternoon_snack" else ""}>🫐 Afternoon Snack</option>
                    <option value="dinner" {"selected" if mt == "dinner" else ""}>🌙 Dinner</option>
                    <option value="evening_snack" {"selected" if mt == "evening_snack" else ""}>🌜 Evening Snack</option>
                </select>
            </td>
            <td>
                <input type="text" value="{meal['dish_name']}" class="inline-input inline-name" name="dish_name"
                       hx-post="/admin/meal/{meal['id']}/edit" hx-include="closest tr" hx-target="#meal-row-{meal['id']}" hx-swap="outerHTML"
                       hx-trigger="change">
            </td>
            <td>
                <input type="number" value="{meal['calories']}" class="inline-input inline-num" name="calories"
                       hx-post="/admin/meal/{meal['id']}/edit" hx-include="closest tr" hx-target="#meal-row-{meal['id']}" hx-swap="outerHTML"
                       hx-trigger="change">
            </td>
            <td class="macros-cell">
                <input type="number" value="{meal['protein_g']}" class="inline-input inline-micro" name="protein_g" step="0.1" hx-post="/admin/meal/{meal['id']}/edit" hx-include="closest tr" hx-target="#meal-row-{meal['id']}" hx-swap="outerHTML" hx-trigger="change">
                <input type="number" value="{meal['carbs_g']}" class="inline-input inline-micro" name="carbs_g" step="0.1" hx-post="/admin/meal/{meal['id']}/edit" hx-include="closest tr" hx-target="#meal-row-{meal['id']}" hx-swap="outerHTML" hx-trigger="change">
                <input type="number" value="{meal['fat_g']}" class="inline-input inline-micro" name="fat_g" step="0.1" hx-post="/admin/meal/{meal['id']}/edit" hx-include="closest tr" hx-target="#meal-row-{meal['id']}" hx-swap="outerHTML" hx-trigger="change">
            </td>
            <td>
                <select class="inline-input inline-assign" name="profile_id"
                        hx-post="/admin/meal/{meal['id']}/edit" hx-include="closest tr" hx-target="#meal-row-{meal['id']}" hx-swap="outerHTML">
                    {profile_opts}
                </select>
            </td>
            <td class="actions-cell">
                <button class="btn btn-sm btn-copy" onclick="openCopyModal({meal['id']}, '{meal['dish_name']}')">📋</button>
                <button class="btn btn-sm btn-danger"
                        hx-delete="/admin/meal/{meal['id']}"
                        hx-target="#meal-row-{meal['id']}"
                        hx-swap="outerHTML"
                        hx-confirm="Delete this meal?">🗑️</button>
            </td>
        </tr>
    """)


@app.post("/admin/meal/{meal_id}/copy", response_class=HTMLResponse)
async def copy_meal(request: Request, meal_id: int, target_date: str = Form(...)):
    if not get_admin_session(request):
        return HTMLResponse("Unauthorized", status_code=401)

    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM meal_plans WHERE id = ?", (meal_id,))
        meal = await cursor.fetchone()
        if not meal:
            return HTMLResponse('<div class="upload-result error"><span class="icon">❌</span><p>Meal not found.</p></div>')

        await db.execute(
            """INSERT INTO meal_plans (plan_date, meal_type, dish_name, description, calories, protein_g, carbs_g, fat_g, fiber_g, profile_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (target_date, meal["meal_type"], meal["dish_name"], meal["description"],
             meal["calories"], meal["protein_g"], meal["carbs_g"], meal["fat_g"], meal["fiber_g"], meal["profile_id"])
        )
        await db.commit()
    finally:
        await db.close()

    return HTMLResponse(f'''
        <div class="upload-result success">
            <span class="icon">✅</span>
            <p>Copied "<strong>{meal["dish_name"]}</strong>" to <strong>{target_date}</strong></p>
        </div>
    ''')


@app.post("/admin/model/add", response_class=HTMLResponse)
async def add_model(request: Request, provider: str = Form(...), model_id: str = Form(...),
                    display_name: str = Form(...), api_key: str = Form("")):
    if not get_admin_session(request):
        return HTMLResponse("Unauthorized", status_code=401)

    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO ai_models (provider, model_id, display_name, api_key) VALUES (?, ?, ?, ?)",
            (provider, model_id.strip(), display_name.strip(), api_key.strip())
        )
        await db.commit()
    finally:
        await db.close()

    return RedirectResponse(url="/admin#models", status_code=302)


@app.delete("/admin/model/{model_id_int}", response_class=HTMLResponse)
async def delete_model(request: Request, model_id_int: int):
    if not get_admin_session(request):
        return HTMLResponse("Unauthorized", status_code=401)

    db = await get_db()
    try:
        await db.execute("DELETE FROM ai_models WHERE id = ?", (model_id_int,))
        await db.commit()
    finally:
        await db.close()

    return HTMLResponse("")


@app.post("/admin/model/default/{model_id_int}", response_class=HTMLResponse)
async def set_default_model(request: Request, model_id_int: int):
    if not get_admin_session(request):
        return HTMLResponse("Unauthorized", status_code=401)

    db = await get_db()
    try:
        await db.execute("UPDATE ai_models SET is_default = 0")
        await db.execute("UPDATE ai_models SET is_default = 1 WHERE id = ?", (model_id_int,))
        await db.commit()
    finally:
        await db.close()

    return RedirectResponse(url="/admin#models", status_code=302)


@app.get("/admin/activity", response_class=HTMLResponse)
async def admin_activity(request: Request, profile_filter: int = 0, page: int = 1):
    if not get_admin_session(request):
        return RedirectResponse(url="/admin/login", status_code=302)

    per_page = 100
    offset = (page - 1) * per_page

    db = await get_db()
    try:
        if profile_filter:
            cursor = await db.execute(
                """SELECT al.*, p.name as profile_name 
                   FROM activity_logs al LEFT JOIN profiles p ON al.profile_id = p.id 
                   WHERE al.profile_id = ?
                   ORDER BY al.created_at DESC LIMIT ? OFFSET ?""",
                (profile_filter, per_page, offset)
            )
        else:
            cursor = await db.execute(
                """SELECT al.*, p.name as profile_name 
                   FROM activity_logs al LEFT JOIN profiles p ON al.profile_id = p.id 
                   ORDER BY al.created_at DESC LIMIT ? OFFSET ?""",
                (per_page, offset)
            )
        activities = await cursor.fetchall()

        cursor = await db.execute("SELECT id, name FROM profiles ORDER BY name")
        profiles = await cursor.fetchall()
    finally:
        await db.close()

    return templates.TemplateResponse("admin_activity.html", {
        "request": request,
        "activities": activities,
        "profiles": profiles,
        "profile_filter": profile_filter,
        "page": page,
    })


@app.post("/admin/password", response_class=HTMLResponse)
async def change_admin_password(request: Request, current_password: str = Form(...), new_password: str = Form(...)):
    if not get_admin_session(request):
        return HTMLResponse("Unauthorized", status_code=401)

    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM admin LIMIT 1")
        admin = await cursor.fetchone()
        if not bcrypt.verify(current_password, admin["password_hash"]):
            return HTMLResponse("""<div class="upload-result error"><span class="icon">❌</span><p>Current password incorrect.</p></div>""")
        
        new_hash = bcrypt.hash(new_password)
        await db.execute("UPDATE admin SET password_hash = ? WHERE id = ?", (new_hash, admin["id"]))
        await db.commit()
    finally:
        await db.close()

    return HTMLResponse("""<div class="upload-result success"><span class="icon">✅</span><p>Password updated successfully.</p></div>""")
