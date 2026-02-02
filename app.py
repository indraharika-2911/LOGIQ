from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import httpx
import math
import re

app = FastAPI(title="LogiQ Quantum Routing API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

OSRM = "https://router.project-osrm.org"
NOMINATIM = "https://nominatim.openstreetmap.org"
OVERPASS = "https://overpass-api.de/api/interpreter"

HEADERS = {"User-Agent": "LogiQ-Quantum-Routing"}

# ================= FUEL DATA =================
CITY_FUEL_PRICES = {
    "Delhi": 96.7,
    "Mumbai": 106.3,
    "Bengaluru": 101.9,
    "Chennai": 102.6,
    "Hyderabad": 109.7,
    "Kolkata": 106.0,
}

DEFAULT_FUEL_PRICE = 100.0

# ================= MILEAGE LOGIC (VAN / TRUCK / LORRY) =================
def estimate_mileage(vehicle: str, cc: int):
    v = vehicle.lower()

    # 🚐 VAN
    if v == "van":
        if cc <= 2000:
            return 12
        elif cc <= 3000:
            return 9
        else:
            return 7

    # 🚚 TRUCK (medium)
    if v == "truck":
        if cc <= 4000:
            return 6
        elif cc <= 6000:
            return 4.5
        else:
            return 3.5

    # 🚛 LORRY (heavy)
    if v == "lorry":
        if cc <= 6000:
            return 4
        elif cc <= 9000:
            return 3
        else:
            return 2.5

    # Fallback
    return 5

def extract_city(address: str):
    if not address:
        return None
    for city in CITY_FUEL_PRICES.keys():
        if city.lower() in address.lower():
            return city
    return None

# ================= MODELS =================
class LatLng(BaseModel):
    lat: float
    lng: float

class RouteRequest(BaseModel):
    start: LatLng
    end: LatLng
    vehicle: str
    cc: str  # comes like "1500 CC", "7000 CC", etc.

# ================= GEO HELPERS =================
async def reverse_geocode(lat, lng):
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            f"{NOMINATIM}/reverse",
            params={"lat": lat, "lon": lng, "format": "json"},
            headers=HEADERS
        )
        return r.json().get("display_name", "Unknown location")

def haversine(lat1, lon1, lat2, lon2):
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a))

def point_line_distance(toll_lat, toll_lng, lat1, lng1, lat2, lng2):
    R = 6371000
    x = math.radians(toll_lng - lng1) * math.cos(math.radians((toll_lat + lat1) / 2))
    y = math.radians(toll_lat - lat1)
    x2 = math.radians(lng2 - lng1) * math.cos(math.radians((lat2 + lat1) / 2))
    y2 = math.radians(lat2 - lat1)
    dot = x * x2 + y * y2
    len_sq = x2 * x2 + y2 * y2
    param = dot / len_sq if len_sq != 0 else -1
    if param < 0:
        xx, yy = 0, 0
    elif param > 1:
        xx, yy = x2, y2
    else:
        xx, yy = param * x2, param * y2
    dx = x - xx
    dy = y - yy
    return R * math.sqrt(dx * dx + dy * dy)

def is_toll_on_route(toll_lat, toll_lng, polyline, threshold=150):
    for i in range(len(polyline) - 1):
        lat1, lng1 = polyline[i]
        lat2, lng2 = polyline[i + 1]
        dist = point_line_distance(toll_lat, toll_lng, lat1, lng1, lat2, lng2)
        if dist <= threshold:
            return True
    return False

def dedupe_nearby_tolls(points, threshold_m=300):
    result = []
    for p in points:
        keep = True
        for q in result:
            d = haversine(p["lat"], p["lng"], q["lat"], q["lng"])
            if d < threshold_m:
                keep = False
                break
        if keep:
            result.append(p)
    return result

async def fetch_tolls_along_route(polyline):
    lats = [p[0] for p in polyline]
    lngs = [p[1] for p in polyline]
    bbox = (min(lats), min(lngs), max(lats), max(lngs))

    query = f"""
    [out:json];
    (
      node["barrier"="toll_booth"]({bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]});
      node["highway"="toll_gantry"]({bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]});
      node["amenity"="toll_plaza"]({bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]});
    );
    out body;
    """

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(OVERPASS, data=query)
        return r.json().get("elements", [])

# ================= GEOCODE =================
@app.get("/geocode")
async def geocode(query: str):
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            f"{NOMINATIM}/search",
            params={"q": query, "format": "json", "limit": 1},
            headers=HEADERS
        )
        data = r.json()
        if not data:
            return {}
        return {
            "lat": float(data[0]["lat"]),
            "lng": float(data[0]["lon"]),
            "address": data[0]["display_name"]
        }

# ================= ROUTES =================
@app.post("/routes")
async def routes(payload: RouteRequest):
    coords = f"{payload.start.lng},{payload.start.lat};{payload.end.lng},{payload.end.lat}"

    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(
            f"{OSRM}/route/v1/driving/{coords}",
            params={"overview": "full", "geometries": "geojson"},
            headers=HEADERS
        )
        data = r.json()
        if "routes" not in data or not data["routes"]:
            raise HTTPException(500, "No route found")

        route = data["routes"][0]

    polyline = [[lat, lon] for lon, lat in route["geometry"]["coordinates"]]
    distance_km = round(route["distance"] / 1000, 1)

    mins = int(route["duration"] / 60)
    eta = f"{mins // 60}h {mins % 60}m"

    # ===== Fuel Calculation =====
    # Extract number from "1500 CC", "7000 CC", etc.
    try:
        cc_match = re.search(r"\d+", payload.cc)
        cc_int = int(cc_match.group()) if cc_match else 4000
    except:
        cc_int = 4000

    start_addr = await reverse_geocode(payload.start.lat, payload.start.lng)
    end_addr = await reverse_geocode(payload.end.lat, payload.end.lng)

    city = extract_city(start_addr)
    fuel_price = CITY_FUEL_PRICES.get(city, DEFAULT_FUEL_PRICE)

    mileage = estimate_mileage(payload.vehicle, cc_int)

    fuel_used_liters = round(distance_km / mileage, 2)
    fuel_cost = round(fuel_used_liters * fuel_price, 2)

    # ===== TOLLS =====
    raw_tolls = await fetch_tolls_along_route(polyline)
    toll_points = []
    seen = set()

    for t in raw_tolls:
        lat = t["lat"]
        lng = t["lon"]

        if not is_toll_on_route(lat, lng, polyline):
            continue

        key = (round(lat, 5), round(lng, 5))
        if key in seen:
            continue
        seen.add(key)

        address = await reverse_geocode(lat, lng)

        toll_points.append({
            "lat": lat,
            "lng": lng,
            "address": address
        })

    toll_points = dedupe_nearby_tolls(toll_points, threshold_m=300)

    return {
        "quantum": {
            "polyline": polyline,
            "distance_km": distance_km,
            "eta": eta,
            "fuel_used_liters": fuel_used_liters,
            "fuel_cost": fuel_cost,
            "fuel_price_per_liter": fuel_price,
            "mileage_used": mileage,
            "vehicle": payload.vehicle,
            "cc": cc_int,
            "city": city or "Unknown"
        },
        "start": {
            "lat": payload.start.lat,
            "lng": payload.start.lng,
            "address": start_addr
        },
        "end": {
            "lat": payload.end.lat,
            "lng": payload.end.lng,
            "address": end_addr
        },
        "toll_count": len(toll_points),
        "toll_points": toll_points
    }
