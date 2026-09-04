import os
import re
import json
import math
import time
import hashlib
import traceback
from datetime import datetime, date, timedelta, timezone
from difflib import SequenceMatcher

import requests


# ============================================================
# CONFIGURATION
# ============================================================

API_BASE_URL = "https://v3.football.api-sports.io"

API_KEYS = [
    os.getenv("FOOTBALL_API_KEY"),
    os.getenv("FOOTBALL_API_KEY_2"),
]

API_KEYS = [key.strip() for key in API_KEYS if key and key.strip()]

OPENLIGADB_BASE = "https://api.openligadb.de"

CACHE_DIR = "data/cache"
OUTPUT_DIR = "data/output"

WEEKLY_FILE = os.path.join(OUTPUT_DIR, "weekly_predictions.json")
DAILY_FILE = os.path.join(OUTPUT_DIR, "daily_predictions.json")
DATA_FILE = os.path.join(OUTPUT_DIR, "merged_match_data.json")

REQUEST_TIMEOUT = 20

# How many API requests a single API key is allowed to make
# during one execution before we rotate.
MAX_REQUESTS_PER_KEY = 45

# Prediction configuration
MIN_DATA_QUALITY = 45
MIN_CONFIDENCE = 50

# API-Football leagues.
# Add more IDs later.
LEAGUES = {
    39: "Premier League",
    140: "La Liga",
    61: "Ligue 1",
    78: "Bundesliga",
    135: "Serie A",
    94: "Primeira Liga",
    88: "Eredivisie",
    203: "Süper Lig",
}


# ============================================================
# UTILITIES
# ============================================================

def ensure_directories():
    os.makedirs(CACHE_DIR, exist_ok=True)
    os.makedirs(OUTPUT_DIR, exist_ok=True)


def today_utc():
    return datetime.now(timezone.utc).date()


def week_dates():
    today = today_utc()
    start = today - timedelta(days=today.weekday())
    end = start + timedelta(days=6)
    return start, end


def normalize_name(name):
    """
    Normalizes club names so that different sources can be matched.
    """

    if not name:
        return ""

    value = name.lower().strip()

    replacements = {
        "&": "and",
        "fc": "",
        "afc": "",
        "cf": "",
        "sc": "",
        "1.": "",
        "  ": " ",
    }

    for old, new in replacements.items():
        value = value.replace(old, new)

    value = re.sub(r"[^a-z0-9\s]", "", value)
    value = re.sub(r"\s+", " ", value)

    return value.strip()


def similarity(a, b):
    a = normalize_name(a)
    b = normalize_name(b)

    if not a or not b:
        return 0

    return SequenceMatcher(None, a, b).ratio()


def match_key(home, away, match_date):
    """
    Creates a deterministic key for deduplication.
    """

    h = normalize_name(home)
    a = normalize_name(away)

    raw = f"{match_date}|{h}|{a}"

    return hashlib.sha1(raw.encode()).hexdigest()


def clamp(value, minimum=0.0, maximum=1.0):
    return max(minimum, min(maximum, value))


# ============================================================
# HTTP SESSION
# ============================================================

class HttpClient:

    def __init__(self):
        self.session = requests.Session()

        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 "
                "(Windows NT 10.0; Win64; x64) "
                AppleWebKit/537.36 "
                "Chrome/120 Safari/537.36"
            )
        })

    def get(self, url, headers=None, params=None):
        return self.session.get(
            url,
            headers=headers,
            params=params,
            timeout=REQUEST_TIMEOUT
        )


# ============================================================
# API-FOOTBALL CLIENT
# ============================================================

class APIFootballClient:

    def __init__(self, keys):

        self.keys = keys
        self.current_key_index = 0

        self.request_counts = {
            index: 0
            for index in range(len(keys))
        }

        self.client = HttpClient()

    def available_key(self):

        if not self.keys:
            return None

        for _ in range(len(self.keys)):

            index = self.current_key_index

            if self.request_counts[index] < MAX_REQUESTS_PER_KEY:
                return self.keys[index]

            self.current_key_index = (
                self.current_key_index + 1
            ) % len(self.keys)

        return None

    def rotate_key(self):

        if not self.keys:
            return

        self.current_key_index = (
            self.current_key_index + 1
        ) % len(self.keys)

    def request(self, endpoint, params):

        if not self.keys:
            print("⚠️ No API-Football keys configured.")
            return None

        attempts = len(self.keys)

        for _ in range(attempts):

            key = self.available_key()

            if not key:
                print("⚠️ API key request allowance exhausted.")
                return None

            key_index = self.current_key_index

            headers = {
                "x-apisports-key": key
            }

            url = f"{API_BASE_URL}/{endpoint}"

            try:

                response = self.client.get(
                    url,
                    headers=headers,
                    params=params
                )

                self.request_counts[key_index] += 1

                if response.status_code == 200:

                    data = response.json()

                    errors = data.get("errors")

                    if errors:
                        error_text = str(errors).lower()

                        if (
                            "rate" in error_text
                            or "limit" in error_text
                            or "token" in error_text
                            or "key" in error_text
                        ):
                            print(
                                f"⚠️ API key {key_index + 1} "
                                "reported an authentication/rate problem."
                            )

                            self.rotate_key()
                            continue

                    return data

                if response.status_code in (401, 403, 429):

                    print(
                        f"⚠️ API key {key_index + 1} "
                        f"returned HTTP {response.status_code}."
                    )

                    self.rotate_key()
                    continue

                print(
                    f"⚠️ API request failed: "
                    f"HTTP {response.status_code}"
                )

            except requests.RequestException as exc:

                print(
                    f"⚠️ API connection error: {exc}"
                )

                self.rotate_key()

        return None


# ============================================================
# API FIXTURE SOURCE
# ============================================================

class APIFixtureSource:

    def __init__(self, api_client):
        self.api = api_client

    def fetch_fixtures(self):

        start, end = week_dates()

        print()
        print("==============================================")
        print("API-FOOTBALL FIXTURE COLLECTION")
        print("==============================================")
        print(f"Period: {start} → {end}")

        fixtures = []

        for league_id, league_name in LEAGUES.items():

            print(
                f"📡 API → {league_name}"
            )

            data = self.api.request(
                "fixtures",
                {
                    "league": league_id,
                    "season": self.current_season(
                        league_id
                    ),
                    "from": start.isoformat(),
                    "to": end.isoformat(),
                }
            )

            if not data:
                continue

            responses = data.get("response", [])

            print(
                f"   ✓ {len(responses)} fixtures"
            )

            for item in responses:

                fixture = item.get("fixture", {})
                teams = item.get("teams", {})
                league = item.get("league", {})

                home = (
                    teams.get("home", {}) or {}
                ).get("name")

                away = (
                    teams.get("away", {}) or {}
                ).get("name")

                fixture_date = fixture.get("date")

                if not home or not away:
                    continue

                if not fixture_date:
                    continue

                match_date = fixture_date.split("T")[0]

                fixtures.append({
                    "source": "api_football",
                    "source_id": fixture.get("id"),
                    "date": match_date,
                    "datetime": fixture_date,
                    "league_id": league.get(
                        "id",
                        league_id
                    ),
                    "league": league.get(
                        "name",
                        league_name
                    ),
                    "home": home,
                    "away": away,
                })

        return fixtures

    @staticmethod
    def current_season(league_id):

        """
        Football seasons are not always equal to calendar years.
        For September, the active European season is generally
        the season beginning in the current calendar year.
        """

        return today_utc().year


# ============================================================
# OPENLIGADB / WEB SOURCE
# ============================================================

class OpenLigaSource:

    def __init__(self):
        self.client = HttpClient()

    def fetch_bundesliga(self):

        """
        OpenLigaDB source.

        This source is supplementary. API-Football remains the
        primary structured source.

        We try the current Bundesliga season endpoint.
        """

        print()
        print("==============================================")
        print("WEB / OPENLIGADB COLLECTION")
        print("==============================================")

        results = []

        season = today_utc().year

        endpoints = [
            f"{OPENLIGADB_BASE}/getmatchdata/bl1/{season}",
        ]

        for url in endpoints:

            print(f"🌐 Web source → {url}")

            try:

                response = self.client.get(url)

                if response.status_code != 200:
                    print(
                        f"⚠️ Web source HTTP "
                        f"{response.status_code}"
                    )
                    continue

                data = response.json()

                if not isinstance(data, list):
                    continue

                for item in data:

                    teams = item.get(
                        "Team1",
                        {}
                    )

                    team2 = item.get(
                        "Team2",
                        {}
                    )

                    home = (
                        teams.get("TeamName")
                        if isinstance(teams, dict)
                        else None
                    )

                    away = (
                        team2.get("TeamName")
                        if isinstance(team2, dict)
                        else None
                    )

                    match_date = item.get(
                        "MatchDateTime"
                    )

                    if not home or not away:
                        continue

                    if not match_date:
                        continue

                    match_date = match_date.split("T")[0]

                    finished = item.get(
                        "MatchIsFinished",
                        False
                    )

                    results.append({
                        "source": "openligadb",
                        "source_id": item.get(
                            "MatchID"
                        ),
                        "date": match_date,
                        "datetime": item.get(
                            "MatchDateTime"
                        ),
                        "league_id": 78,
                        "league": "Bundesliga",
                        "home": home,
                        "away": away,
                        "finished": finished,
                    })

                print(
                    f"   ✓ {len(results)} web fixtures"
                )

                break

            except Exception as exc:

                print(
                    f"⚠️ Web extraction error: {exc}"
                )

        return results


# ============================================================
# FIXTURE NORMALIZER
# ============================================================

class FixtureNormalizer:

    def normalize(self, fixture):

        return {
            "source": fixture.get("source"),
            "source_id": fixture.get("source_id"),
            "date": fixture.get("date"),
            "datetime": fixture.get("datetime"),
            "league_id": fixture.get("league_id"),
            "league": fixture.get("league"),
            "home": fixture.get("home"),
            "away": fixture.get("away"),
            "home_normalized": normalize_name(
                fixture.get("home")
            ),
            "away_normalized": normalize_name(
                fixture.get("away")
            ),
        }


# ============================================================
# FIXTURE MERGER / DEDUPLICATOR
# ============================================================

class FixtureMerger:

    def same_match(self, a, b):

        if a.get("date") != b.get("date"):
            return False

        home_score = similarity(
            a.get("home"),
            b.get("home")
        )

        away_score = similarity(
            a.get("away"),
            b.get("away")
        )

        return (
            home_score >= 0.78
            and away_score >= 0.78
        )

    def merge(self, fixtures):

        merged = []

        for fixture in fixtures:

            found = None

            for existing in merged:

                if self.same_match(
                    fixture,
                    existing
                ):
                    found = existing
                    break

            if found:

                found["sources"].append(
                    fixture.get("source")
                )

                found["source_ids"].append(
                    fixture.get("source_id")
                )

                # Prefer API league information
                if (
                    fixture.get("source")
                    == "api_football"
                ):
                    found["league"] = (
                        fixture.get("league")
                    )

                    found["league_id"] = (
                        fixture.get("league_id")
                    )

                    if fixture.get("datetime"):
                        found["datetime"] = (
                            fixture.get("datetime")
                        )

            else:

                record = dict(fixture)

                record["sources"] = [
                    fixture.get("source")
                ]

                record["source_ids"] = [
                    fixture.get("source_id")
                ]

                merged.append(record)

        return merged


# ============================================================
# API STATISTICS COLLECTOR
# ============================================================

class StatisticsCollector:

    def __init__(self, api_client):
        self.api = api_client

    def collect(self, match):

        """
        Collects additional statistics for each API fixture.

        We deliberately keep this separate from fixture discovery.
        """

        fixture_id = match.get("api_fixture_id")

        if not fixture_id:
            return {}

        data = self.api.request(
            "fixtures",
            {
                "id": fixture_id
            }
        )

        if not data:
            return {}

        responses = data.get("response", [])

        if not responses:
            return {}

        item = responses[0]

        teams = item.get("teams", {})

        home_stats = (
            teams.get("home", {}) or {}
        )

        away_stats = (
            teams.get("away", {}) or {}
        )

        goals = item.get("goals", {}) or {}

        return {
            "home_winner": home_stats.get(
                "winner"
            ),
            "away_winner": away_stats.get(
                "winner"
            ),
            "home_goals": goals.get(
                "home"
            ),
            "away_goals": goals.get(
                "away"
            ),
        }


# ============================================================
# FEATURE ENGINE
# ============================================================

class FeatureEngine:

    def build(self, match):

        """
        Converts available information into model features.

        Missing information is NOT invented.

        This is critical for preventing false confidence.
        """

        features = {
            "home_strength": None,
            "away_strength": None,
            "home_form": None,
            "away_form": None,
            "home_goals": None,
            "away_goals": None,
        }

        stats = match.get("statistics", {})

        home_goals = stats.get("home_goals")
        away_goals = stats.get("away_goals")

        if home_goals is not None:
            features["home_goals"] = home_goals

        if away_goals is not None:
            features["away_goals"] = away_goals

        return features


# ============================================================
# DATA QUALITY
# ============================================================

class DataQualityEngine:

    def score(self, match):

        score = 0

        sources = set(
            match.get("sources", [])
        )

        if "api_football" in sources:
            score += 30

        if "openligadb" in sources:
            score += 15

        if match.get("league"):
            score += 10

        if match.get("date"):
            score += 10

        if match.get("datetime"):
            score += 10

        statistics = match.get(
            "statistics",
            {}
        )

        if statistics:
            score += 15

        if (
            statistics.get("home_goals")
            is not None
        ):
            score += 5

        if (
            statistics.get("away_goals")
            is not None
        ):
            score += 5

        return min(score, 100)


# ============================================================
# POISSON ENGINE
# ============================================================

class PoissonEngine:

    @staticmethod
    def poisson_probability(k, lam):

        if lam <= 0:
            return 1.0 if k == 0 else 0.0

        return (
            math.exp(-lam)
            * (lam ** k)
            / math.factorial(k)
        )

    def matrix(self, home_xg, away_xg):

        matrix = {}

        for home_goals in range(0, 9):

            for away_goals in range(0, 9):

                probability = (
                    self.poisson_probability(
                        home_goals,
                        home_xg
                    )
                    *
                    self.poisson_probability(
                        away_goals,
                        away_xg
                    )
                )

                matrix[
                    (home_goals, away_goals)
                ] = probability

        return matrix

    def calculate(self, home_xg, away_xg):

        matrix = self.matrix(
            home_xg,
            away_xg
        )

        home_win = sum(
            p
            for (h, a), p in matrix.items()
            if h > a
        )

        draw = sum(
            p
            for (h, a), p in matrix.items()
            if h == a
        )

        away_win = sum(
            p
            for (h, a), p in matrix.items()
            if h < a
        )

        over_15 = sum(
            p
            for (h, a), p in matrix.items()
            if h + a >= 2
        )

        over_25 = sum(
            p
            for (h, a), p in matrix.items()
            if h + a >= 3
        )

        over_35 = sum(
            p
            for (h, a), p in matrix.items()
            if h + a >= 4
        )

        btts = sum(
            p
            for (h, a), p in matrix.items()
            if h >= 1 and a >= 1
        )

        probabilities = {
            "Home Win": home_win,
            "Draw": draw,
            "Away Win": away_win,
            "Over 1.5": over_15,
            "Over 2.5": over_25,
            "Over 3.5": over_35,
            "BTTS": btts,
        }

        return probabilities


# ============================================================
# PREDICTION ENGINE
# ============================================================

class PredictionEngine:

    def __init__(self):

        self.poisson = PoissonEngine()

    def estimate_xg(self, match):

        """
        Conservative baseline model.

        Unlike the old engine, it does NOT automatically assign
        a strong home advantage.

        As more historical statistics are collected, this method
        can be upgraded without changing the rest of the system.
        """

        statistics = match.get(
            "statistics",
            {}
        )

        home_goals = statistics.get(
            "home_goals"
        )

        away_goals = statistics.get(
            "away_goals"
        )

        # If actual historical information exists,
        # use it cautiously.
        if (
            home_goals is not None
            and away_goals is not None
        ):

            total = home_goals + away_goals

            if total > 0:

                home_share = (
                    home_goals / total
                )

                away_share = (
                    away_goals / total
                )

                home_xg = (
                    1.45
                    + (home_share - 0.5)
                    * 0.60
                )

                away_xg = (
                    1.20
                    + (away_share - 0.5)
                    * 0.60
                )

                return (
                    clamp(home_xg, 0.25, 3.5),
                    clamp(away_xg, 0.25, 3.5)
                )

        # Neutral fallback.
        #
        # Notice that the difference is small.
        # This prevents missing data from automatically
        # becoming "Home Win".
        return 1.35, 1.20

    def predict(self, match):

        quality = match.get(
            "data_quality",
            0
        )

        home_xg, away_xg = (
            self.estimate_xg(match)
        )

        probabilities = self.poisson.calculate(
            home_xg,
            away_xg
        )

        # ----------------------------------------------------
        # ANTI-HOME-BIAS NORMALIZATION
        # ----------------------------------------------------

        result_probs = {
            "Home Win": probabilities["Home Win"],
            "Draw": probabilities["Draw"],
            "Away Win": probabilities["Away Win"],
        }

        best_result = max(
            result_probs,
            key=result_probs.get
        )

        raw_probability = (
            result_probs[best_result]
        )

        # Confidence combines probability and data quality.
        confidence = (
            raw_probability * 0.70
            + (quality / 100.0) * 0.30
        )

        # Poor data cannot generate a high-confidence prediction.
        if quality < MIN_DATA_QUALITY:
            confidence *= 0.75

        confidence = clamp(
            confidence
        )

        if confidence * 100 < MIN_CONFIDENCE:
            recommendation = "NO BET"
        else:
            recommendation = best_result

        return {
            "xg": {
                "home": round(home_xg, 2),
                "away": round(away_xg, 2),
            },

            "probabilities": {
                key: round(value * 100, 1)
                for key, value
                in probabilities.items()
            },

            "prediction": recommendation,

            "confidence": round(
                confidence * 100,
                1
            ),

            "data_quality": quality,
        }


# ============================================================
# PIPELINE
# ============================================================

class HybridFootballPipeline:

    def __init__(self):

        ensure_directories()

        self.api_client = (
            APIFootballClient(
                API_KEYS
            )
        )

        self.api_source = (
            APIFixtureSource(
                self.api_client
            )
        )

        self.web_source = (
            OpenLigaSource()
        )

        self.normalizer = (
            FixtureNormalizer()
        )

        self.merger = (
            FixtureMerger()
        )

        self.stats = (
            StatisticsCollector(
                self.api_client
            )
        )

        self.quality = (
            DataQualityEngine()
        )

        self.predictor = (
            PredictionEngine()
        )

    # --------------------------------------------------------

    def collect_fixtures(self):

        api_fixtures = (
            self.api_source
            .fetch_fixtures()
        )

        web_fixtures = (
            self.web_source
            .fetch_bundesliga()
        )

        combined = (
            api_fixtures
            + web_fixtures
        )

        normalized = [
            self.normalizer.normalize(
                fixture
            )
            for fixture in combined
        ]

        return self.merger.merge(
            normalized
        )

    # --------------------------------------------------------

    def attach_api_ids(self, matches):

        """
        Connects the merged match to its API-Football fixture ID.
        """

        for match in matches:

            for source_id, source in zip(
                match.get(
                    "source_ids",
                    []
                ),
                match.get(
                    "sources",
                    []
                )
            ):

                if (
                    source
                    == "api_football"
                ):

                    match[
                        "api_fixture_id"
                    ] = source_id

                    break

        return matches

    # --------------------------------------------------------

    def collect_statistics(self, matches):

        """
        Only API-linked fixtures receive API statistics.

        We do NOT waste API requests on matches that don't
        have an API fixture ID.
        """

        for index, match in enumerate(
            matches,
            start=1
        ):

            fixture_id = match.get(
                "api_fixture_id"
            )

            if not fixture_id:
                continue

            print(
                f"📊 Statistics "
                f"{index}/{len(matches)} → "
                f"{match.get('home')} vs "
                f"{match.get('away')}"
            )

            try:

                statistics = (
                    self.stats.collect(
                        match
                    )
                )

                match[
                    "statistics"
                ] = statistics

            except Exception as exc:

                print(
                    f"⚠️ Statistics error: "
                    f"{exc}"
                )

            # Small delay to avoid aggressive requests.
            time.sleep(0.15)

        return matches

    # --------------------------------------------------------

    def calculate_predictions(self, matches):

        output = []

        for match in matches:

            match[
                "data_quality"
            ] = self.quality.score(
                match
            )

            prediction = (
                self.predictor.predict(
                    match
                )
            )

            result = {
                "fixture": {
                    "id": match.get(
                        "api_fixture_id"
                    ),
                    "date": match.get(
                        "date"
                    ),
                    "datetime": match.get(
                        "datetime"
                    ),
                    "league": match.get(
                        "league"
                    ),
                    "home": match.get(
                        "home"
                    ),
                    "away": match.get(
                        "away"
                    ),
                },

                "sources": match.get(
                    "sources",
                    []
                ),

                "data_quality": match.get(
                    "data_quality"
                ),

                "prediction": prediction,
            }

            output.append(result)

        return output

    # --------------------------------------------------------

    def save_json(self, path, payload):

        with open(
            path,
            "w",
            encoding="utf-8"
        ) as file:

            json.dump(
                payload,
                file,
                indent=2,
                ensure_ascii=False
            )

    # --------------------------------------------------------

    def run(self):

        print()
        print("====================================================")
        print("🚀 HYBRID FOOTBALL PREDICTION ENGINE")
        print("====================================================")

        if not API_KEYS:

            print(
                "⚠️ No API keys detected."
            )

            print(
                "The web source will still be attempted."
            )

        matches = (
            self.collect_fixtures()
        )

        print()
        print(
            f"📋 Unique fixtures discovered: "
            f"{len(matches)}"
        )

        if not matches:

            print(
                "❌ No fixtures discovered."
            )

            return

        matches = (
            self.attach_api_ids(
                matches
            )
        )

        matches = (
            self.collect_statistics(
                matches
            )
        )

        predictions = (
            self.calculate_predictions(
                matches
            )
        )

        today = today_utc().isoformat()

        daily = [
            item
            for item in predictions
            if item["fixture"]["date"]
            == today
        ]

        start, end = week_dates()

        weekly_payload = {
            "generated_at": datetime.now(
                timezone.utc
            ).isoformat(),

            "week_start": start.isoformat(),

            "week_end": end.isoformat(),

            "match_count": len(
                predictions
            ),

            "matches": predictions,
        }

        daily_payload = {
            "generated_at": datetime.now(
                timezone.utc
            ).isoformat(),

            "date": today,

            "match_count": len(
                daily
            ),

            "matches": daily,
        }

        self.save_json(
            WEEKLY_FILE,
            weekly_payload
        )

        self.save_json(
            DAILY_FILE,
            daily_payload
        )

        self.save_json(
            DATA_FILE,
            {
                "generated_at":
                    datetime.now(
                        timezone.utc
                    ).isoformat(),

                "matches": matches,
            }
        )

        print()
        print("====================================================")
        print("✅ PIPELINE COMPLETE")
        print("====================================================")

        print(
            f"📅 Weekly matches: "
            f"{len(predictions)}"
        )

        print(
            f"🔥 Today's matches: "
            f"{len(daily)}"
        )

        print(
            f"🔑 API requests used: "
            f"{sum(self.api_client.request_counts.values())}"
        )

        print(
            f"📁 Output: {OUTPUT_DIR}"
        )


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    try:

        pipeline = (
            HybridFootballPipeline()
        )

        pipeline.run()

    except KeyboardInterrupt:

        print(
            "\n🛑 Pipeline stopped by user."
        )

    except Exception as exc:

        print()
        print(
            "❌ CRITICAL PIPELINE FAILURE"
        )

        print(
            f"Error: {exc}"
        )

        traceback.print_exc()

        raise
