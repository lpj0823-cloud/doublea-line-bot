import os
import requests

DEFAULT_LAT = 23.3352
DEFAULT_LON = 121.3178
DEFAULT_LOCATION_NAME = "玉里"


def search_nearby_restaurants(keyword: str = "", location_name: str = DEFAULT_LOCATION_NAME,
                               lat: float = DEFAULT_LAT, lon: float = DEFAULT_LON,
                               radius: int = 1000) -> list[dict]:
    api_key = os.environ["GOOGLE_MAPS_API_KEY"]
    search_query = keyword if keyword else "餐廳"
    url = "https://maps.googleapis.com/maps/api/place/textsearch/json"
    params = {
        "query": f"{location_name} {search_query}",
        "location": f"{lat},{lon}",
        "radius": radius,
        "language": "zh-TW",
        "key": api_key,
    }
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    data = r.json()
    results = []
    for place in data.get("results", [])[:5]:
        place_id = place.get("place_id", "")
        name = place.get("name", "")
        rating = place.get("rating", 0)
        address = place.get("formatted_address", "")
        open_now = place.get("opening_hours", {}).get("open_now", None)
        maps_url = f"https://www.google.com/maps/place/?q=place_id:{place_id}"
        results.append({
            "name": name,
            "rating": rating,
            "address": address,
            "open_now": open_now,
            "maps_url": maps_url,
        })
    return results


def format_restaurant_results(results: list[dict], keyword: str = "") -> str:
    if not results:
        kw = f"「{keyword}」" if keyword else "餐廳"
        return f"😅 附近找不到{kw}，換個關鍵字試試？"
    kw = f"{keyword}" if keyword else "餐廳"
    lines = [f"🍽️ 附近{kw}推薦\n"]
    for i, r in enumerate(results, 1):
        rating_str = f"⭐ {r['rating']}" if r['rating'] else ""
        if r['open_now'] is True:
            status = "🟢 營業中"
        elif r['open_now'] is False:
            status = "🔴 已打烊"
        else:
            status = ""
        status_rating = " | ".join(filter(None, [status, rating_str]))
        lines.append(f"{i}. 【{r['name']}】")
        if status_rating:
            lines.append(f"   {status_rating}")
        lines.append(f"   🔗 {r['maps_url']}")
        lines.append("")
    return "\n".join(lines).strip()
