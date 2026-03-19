"""
FarmWatch — Python backend v1.1
Napojuje se na Apify API a vraci ohodnocene profily.
"""

import os
import time
import re
import json
import requests
from flask import Flask, jsonify, request
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

APIFY_BASE = "https://api.apify.com/v2"

print("=== FarmWatch backend start ===", flush=True)
print(f"APIFY_TOKEN present: {bool(os.environ.get('APIFY_TOKEN'))}", flush=True)


# ──────────────────────────────────────────────
# Scoring
# ──────────────────────────────────────────────

def compute_score(profile: dict) -> int:
    score = 0
    friends      = profile.get("friends", 0) or 0
    age_months   = profile.get("ageMonths", 0) or 0
    has_bio      = profile.get("hasBio", False)
    has_photo    = profile.get("hasRealPhoto", False)
    photo_reused = profile.get("photoReused", False)
    public_posts = profile.get("publicPosts", 0) or 0
    name         = profile.get("name", "") or ""

    if age_months == 0:   score += 15
    elif age_months < 3:  score += 35
    elif age_months < 9:  score += 20
    elif age_months < 18: score += 8

    if friends == 0:      score += 25
    elif friends < 5:     score += 30
    elif friends < 30:    score += 18
    elif friends < 80:    score += 6
    elif friends > 2000:  score += 10

    if not has_bio:       score += 10
    if not has_photo:     score += 20
    if photo_reused:      score += 30

    if public_posts == 0:  score += 10
    elif public_posts < 3: score += 12
    elif public_posts < 10: score += 5

    if re.search(r"\d{2,}", name):
        score += 10
    if re.search(r"\b(user|account|acc|page|info|official)\b", name, re.I):
        score += 12

    return min(100, score)


def score_to_risk(score: int) -> str:
    if score >= 65: return "high"
    if score >= 35: return "medium"
    return "low"


def enrich_profiles(raw_profiles: list) -> list:
    result = []
    for i, p in enumerate(raw_profiles):
        score = compute_score(p)
        result.append({
            "id":          i,
            "name":        p.get("name") or "Nezname",
            "link":        p.get("link") or "",
            "friends":     p.get("friends") or 0,
            "ageMonths":   p.get("ageMonths") or 0,
            "hasBio":      bool(p.get("hasBio")),
            "hasRealPhoto":bool(p.get("hasRealPhoto")),
            "photoReused": bool(p.get("photoReused")),
            "publicPosts": p.get("publicPosts") or 0,
            "score":       score,
            "risk":        score_to_risk(score),
        })
    return result


# ──────────────────────────────────────────────
# Apify integrace
# ──────────────────────────────────────────────

def run_apify_actor(token: str, actor_id: str, input_data: dict, timeout: int = 180) -> list:
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }

    print(f"[Apify] Spoustim actor: {actor_id}", flush=True)
    print(f"[Apify] Input: {json.dumps(input_data)[:300]}", flush=True)

    run_url = f"{APIFY_BASE}/acts/{actor_id}/runs"
    resp = requests.post(run_url, json=input_data, headers=headers, timeout=30)
    print(f"[Apify] Run response: {resp.status_code}", flush=True)
    resp.raise_for_status()

    run_data   = resp.json()["data"]
    run_id     = run_data["id"]
    dataset_id = run_data["defaultDatasetId"]
    print(f"[Apify] Run ID: {run_id}, Dataset: {dataset_id}", flush=True)

    status_url = f"{APIFY_BASE}/actor-runs/{run_id}"
    deadline   = time.time() + timeout
    while time.time() < deadline:
        time.sleep(5)
        status_resp = requests.get(status_url, headers=headers, timeout=10)
        status = status_resp.json()["data"]["status"]
        print(f"[Apify] Status: {status}", flush=True)
        if status == "SUCCEEDED":
            break
        if status in ("FAILED", "ABORTED", "TIMED-OUT"):
            raise RuntimeError(f"Apify actor skoncil se stavem: {status}")

    items_url  = f"{APIFY_BASE}/datasets/{dataset_id}/items?limit=500"
    items_resp = requests.get(items_url, headers=headers, timeout=30)
    items_resp.raise_for_status()
    items = items_resp.json()
    print(f"[Apify] Stazeno polozek: {len(items)}", flush=True)
    if items:
        print(f"[Apify] SAMPLE: {json.dumps(items[0], ensure_ascii=False)[:800]}", flush=True)
    return items


def normalize_link(link: str) -> str:
    if not link:
        return ""
    if "facebook.com" in link:
        link = "/" + link.split("facebook.com/")[-1].rstrip("/")
    return link


def parse_fb_profiles(raw_items: list) -> list:
    profiles = []
    seen     = set()

    for item in raw_items:
        author = item.get("author") or item.get("user") or item.get("commenter") or {}

        name = (
            author.get("name") or
            item.get("authorName") or
            item.get("name") or
            item.get("userName") or
            item.get("ownerName") or
            ""
        )

        link = (
            author.get("url") or author.get("link") or author.get("profileUrl") or
            item.get("authorUrl") or item.get("profileUrl") or
            item.get("authorLink") or item.get("userUrl") or
            ""
        )

        photo = (
            author.get("profilePicUrl") or author.get("photo") or author.get("picture") or
            item.get("profilePicUrl") or item.get("authorPhoto") or
            ""
        )

        friends = (
            author.get("friendsCount") or author.get("friends") or
            item.get("friendsCount") or item.get("friends") or
            0
        )

        has_bio = bool(
            author.get("about") or author.get("bio") or author.get("description") or
            item.get("about") or item.get("bio")
        )

        posts = (
            author.get("postsCount") or author.get("posts") or
            item.get("postsCount") or item.get("posts") or
            0
        )

        if not name or name in seen:
            continue
        seen.add(name)

        profiles.append({
            "name":         name,
            "link":         normalize_link(link),
            "friends":      int(friends) if friends else 0,
            "ageMonths":    0,
            "hasBio":       has_bio,
            "hasRealPhoto": bool(photo),
            "photoReused":  False,
            "publicPosts":  int(posts) if posts else 0,
        })

    print(f"[Parse] Unikatnich profilu: {len(profiles)}", flush=True)
    return profiles


# ──────────────────────────────────────────────
# API endpointy
# ──────────────────────────────────────────────

@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "version": "1.1"})


@app.route("/api/analyze", methods=["POST"])
def analyze():
    data  = request.get_json()
    url   = (data.get("url") or "").strip()
    token = (data.get("apifyToken") or "").strip() or os.environ.get("APIFY_TOKEN", "")
    max_p = int(data.get("maxProfiles") or 50)

    print(f"[Analyze] URL: {url}", flush=True)
    print(f"[Analyze] Token: {'OK - ' + token[:8] + '...' if token else 'MISSING'}", flush=True)

    if not url:
        return jsonify({"error": "Chybi URL prispevku"}), 400
    if not token:
        return jsonify({"error": "Chybi Apify API token"}), 400

    try:
        raw_items = run_apify_actor(
            token=token,
            actor_id="apify~facebook-comments-scraper",
            input_data={
                "startUrls": [{"url": url}],
                "maxComments": max_p,
                "includeNestedComments": False,
            },
            timeout=180
        )

        raw_profiles = parse_fb_profiles(raw_items)
        profiles     = enrich_profiles(raw_profiles)

        farms   = sum(1 for p in profiles if p["risk"] == "high")
        suspect = sum(1 for p in profiles if p["risk"] == "medium")
        real    = sum(1 for p in profiles if p["risk"] == "low")
        avg     = round(sum(p["score"] for p in profiles) / len(profiles)) if profiles else 0

        return jsonify({
            "profiles": profiles,
            "stats": {
                "total":    len(profiles),
                "farms":    farms,
                "suspect":  suspect,
                "real":     real,
                "avgScore": avg,
            },
            "debug": {
                "raw_count":    len(raw_items),
                "parsed_count": len(raw_profiles),
            }
        })

    except requests.exceptions.HTTPError as e:
        code = e.response.status_code if e.response else 0
        body = e.response.text[:200] if e.response else ""
        print(f"[Error] HTTP {code}: {body}", flush=True)
        if code == 401:
            return jsonify({"error": "Neplatny Apify token (401)"}), 401
        return jsonify({"error": f"Apify API chyba: {str(e)}"}), 502
    except Exception as e:
        print(f"[Error] {str(e)}", flush=True)
        return jsonify({"error": f"Chyba: {str(e)}"}), 500


@app.route("/api/score", methods=["POST"])
def score_manual():
    data     = request.get_json()
    profiles = data.get("profiles", [])
    if not profiles:
        return jsonify({"error": "Chybi profily"}), 400
    return jsonify({"profiles": enrich_profiles(profiles)})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
