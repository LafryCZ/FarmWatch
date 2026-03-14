"""
FarmWatch — Python backend
Napojuje se na Apify API a vrací ohodnocené profily.
"""

import os
import time
import re
import requests
from flask import Flask, jsonify, request
from flask_cors import CORS

app = Flask(__name__)
CORS(app)  # Povolí volání z frontendu (Netlify)

APIFY_BASE = "https://api.apify.com/v2"

# ──────────────────────────────────────────────
# Pomocné funkce
# ──────────────────────────────────────────────

def get_token():
    """Token z env proměnné nebo z requestu."""
    return request.json.get("apifyToken") or os.environ.get("APIFY_TOKEN", "")


def compute_score(profile: dict) -> int:
    """
    Vypočítá farm skóre (0–100) podle signálů profilu.
    Čím vyšší skóre, tím větší riziko že jde o farmu.
    """
    score = 0
    friends      = profile.get("friends", 0)
    age_months   = profile.get("ageMonths", 999)
    has_bio      = profile.get("hasBio", True)
    has_photo    = profile.get("hasRealPhoto", True)
    photo_reused = profile.get("photoReused", False)
    public_posts = profile.get("publicPosts", 99)
    name         = profile.get("name", "")

    # Věk účtu
    if age_months < 3:    score += 35
    elif age_months < 9:  score += 20
    elif age_months < 18: score += 8

    # Počet přátel
    if friends < 5:     score += 30
    elif friends < 30:  score += 18
    elif friends < 80:  score += 6
    elif friends > 2000: score += 10  # kupovaní sledující

    # Profil
    if not has_bio:       score += 10
    if not has_photo:     score += 20
    if photo_reused:      score += 30

    # Příspěvky
    if public_posts < 3:  score += 12
    elif public_posts < 10: score += 5

    # Vzory jmen
    if re.search(r"\d{2,}", name):              score += 10
    if re.search(r"\b(user|account|acc|page|info|official)\b", name, re.I): score += 12

    return min(100, score)


def score_to_risk(score: int) -> str:
    if score >= 65: return "high"
    if score >= 35: return "medium"
    return "low"


def enrich_profiles(raw_profiles: list) -> list:
    """Přidá farm skóre ke každému profilu."""
    result = []
    for i, p in enumerate(raw_profiles):
        score = compute_score(p)
        result.append({
            "id": i,
            "name":        p.get("name", "Neznámý"),
            "link":        p.get("link", ""),
            "friends":     p.get("friends", 0),
            "ageMonths":   p.get("ageMonths", 0),
            "hasBio":      p.get("hasBio", False),
            "hasRealPhoto":p.get("hasRealPhoto", False),
            "photoReused": p.get("photoReused", False),
            "publicPosts": p.get("publicPosts", 0),
            "score":       score,
            "risk":        score_to_risk(score),
        })
    return result


# ──────────────────────────────────────────────
# Apify integrace
# ──────────────────────────────────────────────

def run_apify_actor(token: str, actor_id: str, input_data: dict, timeout: int = 120) -> list:
    """
    Spustí Apify actor a počká na výsledky.
    Vrátí seznam položek z datasetu.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }

    # 1. Spustit actor
    run_url = f"{APIFY_BASE}/acts/{actor_id}/runs"
    resp = requests.post(run_url, json=input_data, headers=headers, timeout=30)
    resp.raise_for_status()
    run_data = resp.json()["data"]
    run_id   = run_data["id"]
    dataset_id = run_data["defaultDatasetId"]

    # 2. Čekat na dokončení (polling)
    status_url = f"{APIFY_BASE}/actor-runs/{run_id}"
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(5)
        status_resp = requests.get(status_url, headers=headers, timeout=10)
        status = status_resp.json()["data"]["status"]
        if status == "SUCCEEDED":
            break
        if status in ("FAILED", "ABORTED", "TIMED-OUT"):
            raise RuntimeError(f"Apify actor skončil se stavem: {status}")

    # 3. Stáhnout výsledky
    items_url = f"{APIFY_BASE}/datasets/{dataset_id}/items?limit=200"
    items_resp = requests.get(items_url, headers=headers, timeout=30)
    items_resp.raise_for_status()
    return items_resp.json()


def parse_fb_profiles(raw_items: list) -> list:
    """
    Převede raw data z Apify Facebook scraperu
    do formátu pro naši analýzu.
    """
    profiles = []
    for item in raw_items:
        # Apify Facebook Post Scraper vrací různé struktury
        # podle toho jaký actor použiješ — upravíme dle potřeby
        author = item.get("author") or item.get("user") or {}
        name   = author.get("name") or item.get("authorName") or "Neznámý"
        link   = author.get("link") or item.get("authorUrl") or ""

        # Normalizace odkazu — jen část za facebook.com
        if "facebook.com" in link:
            link = "/" + link.split("facebook.com/")[-1].rstrip("/")

        profiles.append({
            "name":         name,
            "link":         link,
            "friends":      author.get("friends") or item.get("friendsCount") or 0,
            "ageMonths":    author.get("ageMonths") or 0,  # Apify toto nevrací přímo
            "hasBio":       bool(author.get("about") or author.get("bio")),
            "hasRealPhoto": bool(author.get("profilePicUrl") or author.get("photo")),
            "photoReused":  False,  # vyžaduje TinEye API — zatím False
            "publicPosts":  author.get("postsCount") or item.get("postsCount") or 0,
        })
    return profiles


# ──────────────────────────────────────────────
# API endpointy
# ──────────────────────────────────────────────

@app.route("/api/health", methods=["GET"])
def health():
    """Ověření že backend běží."""
    return jsonify({"status": "ok", "version": "1.0"})


@app.route("/api/analyze", methods=["POST"])
def analyze():
    """
    Hlavní endpoint — přijme URL příspěvku a Apify token,
    vrátí seznam profilů s farm skóre.

    Body: {
        "url": "https://facebook.com/...",
        "apifyToken": "apify_api_xxx",
        "maxProfiles": 50
    }
    """
    data  = request.get_json()
    url   = data.get("url", "").strip()
    token = data.get("apifyToken", "").strip() or os.environ.get("APIFY_TOKEN", "")
    max_p = int(data.get("maxProfiles", 50))

    if not url:
        return jsonify({"error": "Chybí URL příspěvku"}), 400
    if not token:
        return jsonify({"error": "Chybí Apify API token"}), 400

    try:
        # Spustit Facebook scraper
        # Actor: apify/facebook-posts-scraper
        raw_items = run_apify_actor(
            token=token,
            actor_id="apify/facebook-comments-scraper",
            input_data={
                "postUrls": [url],
                "maxComments": max_p,
                "includeNestedComments": True,
            },
            timeout=180
        )

        # Parsovat a ohodnotit profily
        raw_profiles  = parse_fb_profiles(raw_items)
        profiles      = enrich_profiles(raw_profiles)

        farms   = sum(1 for p in profiles if p["risk"] == "high")
        suspect = sum(1 for p in profiles if p["risk"] == "medium")
        real    = sum(1 for p in profiles if p["risk"] == "low")
        avg_score = round(sum(p["score"] for p in profiles) / len(profiles)) if profiles else 0

        return jsonify({
            "profiles": profiles,
            "stats": {
                "total":    len(profiles),
                "farms":    farms,
                "suspect":  suspect,
                "real":     real,
                "avgScore": avg_score,
            }
        })

    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response else 0
        if status == 401:
            return jsonify({"error": "Neplatný Apify token (401)"}), 401
        return jsonify({"error": f"Apify API chyba: {str(e)}"}), 502
    except Exception as e:
        return jsonify({"error": f"Chyba: {str(e)}"}), 500


@app.route("/api/score", methods=["POST"])
def score_manual():
    """
    Ruční ohodnocení — přijme CSV data a vrátí profily se skóre.
    Užitečné když data máš z jiného zdroje.

    Body: { "profiles": [ {...}, ... ] }
    """
    data     = request.get_json()
    profiles = data.get("profiles", [])
    if not profiles:
        return jsonify({"error": "Chybí profily"}), 400

    enriched = enrich_profiles(profiles)
    return jsonify({"profiles": enriched})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
