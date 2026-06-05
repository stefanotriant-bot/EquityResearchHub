"""Equity Research Hub: due diligence aggregator with auth, watchlist, and subscriptions.

Data sources:
  - yfinance (Yahoo Finance) for prices, financials, news, holders
  - SEC EDGAR submissions API for filings (10-K, 10-Q, 8-K, DEF 14A)

NOTE on payments: The /api/subscribe endpoint simulates an upgrade. For real
billing, swap the simulated upgrade block for a Stripe Checkout / Subscription
flow before launching publicly.
"""
import hashlib
import json
import os
import re
import secrets
import sqlite3
from datetime import datetime, timezone, timedelta, date
from functools import lru_cache, wraps

import requests
import yfinance as yf
from flask import (Flask, flash, g, jsonify, redirect, render_template,
                   request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

try:
    import stripe
    _STRIPE_LIB = True
except ImportError:
    _STRIPE_LIB = False

try:
    import anthropic
    _ANTHROPIC_LIB = True
except ImportError:
    _ANTHROPIC_LIB = False

# ============================================================================
# OWNER CONFIGURATION
# ----------------------------------------------------------------------------
# Change this to YOUR email. When you sign up with this email, you'll
# automatically have permanent free Patron-tier access.
# You can also set the OWNER_EMAIL environment variable to override.
OWNER_EMAIL = os.environ.get("OWNER_EMAIL", "owner@equityresearchhub.local").lower()
# ============================================================================

APP_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get(
    "FLASK_SECRET",
    "erh-dev-secret-change-me-" + os.urandom(8).hex(),
)
app.config["DATABASE"] = os.path.join(APP_DIR, "erh.db")
app.permanent_session_lifetime = timedelta(days=30)

# ---------- Plan & limit configuration ----------
# Free tier is FEATURE-gated, not count-gated. Unlimited lookups, unlimited watchlist.
# What's locked: AI brief, research journal, alerts, exports, comparison, full filings.
FREE_WATCHLIST_LIMIT = 10
APPRENTICE_AI_BRIEFS_PER_DAY = 10
APPRENTICE_JOURNAL_TICKERS = 5
APPRENTICE_ACTIVE_ALERTS = 5
# Patron and lifetime are unlimited.

PLANS = {
    "apprentice": {"monthly": 9.99, "yearly": 89.0, "name": "Apprentice"},
    "patron":     {"monthly": 24.99, "yearly": 239.0, "name": "Patron"},
}
LIFETIME_PRICE = 349.0

# ---------- Stripe (real payments) ----------
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY")
STRIPE_PUBLISHABLE_KEY = os.environ.get("STRIPE_PUBLISHABLE_KEY")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET")
APP_URL = os.environ.get("APP_URL", "")
STRIPE_ENABLED = bool(STRIPE_SECRET_KEY and _STRIPE_LIB)
if STRIPE_ENABLED:
    stripe.api_key = STRIPE_SECRET_KEY
STRIPE_PRICES = {
    ("apprentice", "monthly"): os.environ.get("STRIPE_PRICE_APPRENTICE_MONTHLY"),
    ("apprentice", "yearly"):  os.environ.get("STRIPE_PRICE_APPRENTICE_YEARLY"),
    ("patron", "monthly"):     os.environ.get("STRIPE_PRICE_PATRON_MONTHLY"),
    ("patron", "yearly"):      os.environ.get("STRIPE_PRICE_PATRON_YEARLY"),
    ("lifetime", "lifetime"):  os.environ.get("STRIPE_PRICE_LIFETIME"),
}

# ---------- Anthropic (AI company brief) ----------
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
ANTHROPIC_ENABLED = bool(ANTHROPIC_API_KEY and _ANTHROPIC_LIB)
AI_BRIEF_CACHE_HOURS = 24
AI_BRIEF_MODEL = "claude-haiku-4-5-20251001"

# ---------- Community chat ----------
TIER_HIERARCHY = {"free": 0, "apprentice": 1, "patron": 2}
CHAT_MAX_MESSAGE_LEN = 2000
CHAT_RATE_LIMITS = {
    # tier: (max_messages, window_seconds) or None for unlimited
    "free": (15, 600),         # 15 messages per 10 minutes
    "apprentice": (100, 600),  # 100 per 10 minutes
    "patron": None,            # unlimited
}
ACTIVE_USER_WINDOW_SECONDS = 5 * 60  # users seen in last 5 minutes
DEFAULT_ROOMS = [
    ("general",         "general",         "Welcome",  "free",    "General chat. Stocks, life, anything."),
    ("introductions",   "introductions",   "Welcome",  "free",    "Say hi. Tell us how you got into investing."),
    ("rules",           "rules-and-tips",  "Welcome",  "free",    "Community rules and how to get the most out of the app."),
    ("stock-ideas",     "stock-ideas",     "Research", "free",    "Share what you're researching."),
    ("earnings-watch",  "earnings-watch",  "Research", "free",    "Reactions and notes on earnings reports."),
    ("sectors",         "sectors",         "Research", "free",    "Industry and sector talk."),
    ("long-term",       "long-term",       "Strategy", "free",    "Buy and hold. Multi-year theses."),
    ("value",           "value",           "Strategy", "free",    "Value investing, deep dives, screens."),
    ("growth",          "growth",          "Strategy", "free",    "Growth, momentum, secular winners."),
    ("dividends",       "dividends",       "Strategy", "free",    "Dividend stocks and income strategies."),
    ("off-topic",       "off-topic",       "Lounge",   "free",    "Anything that's not stocks."),
    ("patron-lounge",   "patron-lounge",   "Lounge",   "patron",  "Patron-only chat. Founders pop in here."),
]

# ---------- Affiliate program ----------
# Industry-standard SaaS terms (modeled on ConvertKit, Notion, Webflow):
COMMISSION_PCT = 0.30          # 30% commission on every paid conversion
COMMISSION_DURATION_MONTHS = 12  # recurring for 12 months on subscriptions
COOKIE_TTL_SECONDS = 30 * 24 * 60 * 60  # 30-day attribution window
PAYOUT_MINIMUM = 50.0          # $50 minimum to request payout
REFERRAL_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no I, L, O, 0, 1
REFERRAL_LENGTH = 7

# ---------- SEC ----------
UA = "Equity Research Hub research@example.com"
SEC_HEADERS = {"User-Agent": UA, "Accept-Encoding": "gzip, deflate", "Host": "data.sec.gov"}
SEC_TICKER_HEADERS = {"User-Agent": UA, "Accept-Encoding": "gzip, deflate"}


# ============================================================================
# DATABASE
# ============================================================================
def init_db():
    db = sqlite3.connect(app.config["DATABASE"])
    db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            display_name TEXT,
            tier TEXT DEFAULT 'free' CHECK (tier IN ('free','apprentice','patron')),
            billing_cycle TEXT,
            subscription_until TEXT,
            lifetime INTEGER DEFAULT 0,
            is_owner INTEGER DEFAULT 0,
            lookups_count INTEGER DEFAULT 0,
            lookups_date TEXT,
            lifetime_offer_status TEXT DEFAULT 'available'
                CHECK (lifetime_offer_status IN ('available','expired','purchased')),
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS watchlist (
            user_id INTEGER NOT NULL,
            ticker TEXT NOT NULL,
            notes TEXT,
            added_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, ticker),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS referral_clicks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            referral_code TEXT NOT NULL,
            ip_hash TEXT,
            user_agent TEXT,
            referer TEXT,
            clicked_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_clicks_code ON referral_clicks(referral_code);
        CREATE TABLE IF NOT EXISTS referral_conversions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            referrer_user_id INTEGER NOT NULL,
            referred_user_id INTEGER NOT NULL,
            event_type TEXT NOT NULL CHECK (event_type IN ('signup','subscription','lifetime')),
            plan TEXT,
            cycle TEXT,
            gross_amount REAL DEFAULT 0,
            commission_amount REAL DEFAULT 0,
            status TEXT DEFAULT 'pending' CHECK (status IN ('pending','paid','reversed')),
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            paid_at TEXT,
            FOREIGN KEY (referrer_user_id) REFERENCES users(id),
            FOREIGN KEY (referred_user_id) REFERENCES users(id)
        );
        CREATE INDEX IF NOT EXISTS idx_conv_referrer ON referral_conversions(referrer_user_id);
        CREATE TABLE IF NOT EXISTS chat_rooms (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            description TEXT,
            category TEXT,
            tier_required TEXT DEFAULT 'free' CHECK (tier_required IN ('free','apprentice','patron')),
            sort_order INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            body TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (room_id) REFERENCES chat_rooms(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_messages_room ON chat_messages(room_id, id DESC);
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            ticker TEXT NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('price_above','price_below','pe_below','volume_above')),
            threshold REAL NOT NULL,
            note TEXT,
            status TEXT DEFAULT 'active' CHECK (status IN ('active','triggered','disabled')),
            triggered_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_alerts_user ON alerts(user_id, status);
        CREATE TABLE IF NOT EXISTS research_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            ticker TEXT NOT NULL,
            thesis TEXT,
            what_must_be_true TEXT,
            what_changes_mind TEXT,
            entry_target REAL,
            exit_target REAL,
            notes TEXT,
            revision_number INTEGER DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_notes_user_ticker ON research_notes(user_id, ticker);
        CREATE TABLE IF NOT EXISTS note_revisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            note_id INTEGER NOT NULL,
            snapshot TEXT NOT NULL,
            saved_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (note_id) REFERENCES research_notes(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS ai_briefs (
            ticker TEXT PRIMARY KEY,
            brief TEXT NOT NULL,
            sources TEXT,
            generated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS ai_brief_usage (
            user_id INTEGER NOT NULL,
            ticker TEXT NOT NULL,
            used_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_brief_usage_user ON ai_brief_usage(user_id, used_at);
    """)
    # Lightweight migrations for existing DBs
    cur = db.execute("PRAGMA table_info(users)")
    cols = {row[1] for row in cur}
    if "referral_code" not in cols:
        db.execute("ALTER TABLE users ADD COLUMN referral_code TEXT")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_refcode ON users(referral_code) WHERE referral_code IS NOT NULL")
    if "referred_by_user_id" not in cols:
        db.execute("ALTER TABLE users ADD COLUMN referred_by_user_id INTEGER")
    if "payout_email" not in cols:
        db.execute("ALTER TABLE users ADD COLUMN payout_email TEXT")
    if "payout_method" not in cols:
        db.execute("ALTER TABLE users ADD COLUMN payout_method TEXT")
    if "last_active" not in cols:
        db.execute("ALTER TABLE users ADD COLUMN last_active TEXT")
    if "stripe_customer_id" not in cols:
        db.execute("ALTER TABLE users ADD COLUMN stripe_customer_id TEXT")
    if "stripe_subscription_id" not in cols:
        db.execute("ALTER TABLE users ADD COLUMN stripe_subscription_id TEXT")
    # Watchlist position tracking columns
    cur = db.execute("PRAGMA table_info(watchlist)")
    wcols = {row[1] for row in cur}
    if "shares" not in wcols:
        db.execute("ALTER TABLE watchlist ADD COLUMN shares REAL")
    if "avg_cost" not in wcols:
        db.execute("ALTER TABLE watchlist ADD COLUMN avg_cost REAL")
    # Seed default chat rooms
    for i, (slug, name, category, tier_req, desc) in enumerate(DEFAULT_ROOMS):
        db.execute(
            """INSERT OR IGNORE INTO chat_rooms
               (slug, name, description, category, tier_required, sort_order)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (slug, name, desc, category, tier_req, i),
        )
    db.commit()
    db.close()


def get_db():
    db = getattr(g, "_db", None)
    if db is None:
        db = sqlite3.connect(app.config["DATABASE"])
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        g._db = db
    return db


@app.teardown_appcontext
def close_db(_):
    db = getattr(g, "_db", None)
    if db is not None:
        db.close()


# ============================================================================
# AUTH HELPERS
# ============================================================================
def current_user():
    if not session.get("user_id"):
        return None
    cached = getattr(g, "_user", None)
    if cached is not None:
        return cached
    row = get_db().execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()
    if row is None:
        session.clear()
        return None
    user = dict(row)
    if user["email"] == OWNER_EMAIL:
        user["is_owner"] = 1
    user["effective_tier"] = compute_effective_tier(user)
    g._user = user
    return user


def compute_effective_tier(user):
    if user.get("is_owner") or user.get("lifetime"):
        return "patron"
    until = user.get("subscription_until")
    if until:
        try:
            if datetime.fromisoformat(until) > datetime.now(tz=timezone.utc):
                return user.get("tier") or "free"
        except (ValueError, TypeError):
            pass
        return "free"
    return user.get("tier") or "free"


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user():
            if request.path.startswith("/api/"):
                return jsonify({"error": "Sign in to continue.", "authRequired": True}), 401
            return redirect(url_for("login_page", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def user_for_template(user):
    if not user:
        return None
    return {
        "id": user["id"],
        "email": user["email"],
        "display_name": user.get("display_name"),
        "tier": user.get("tier"),
        "effective_tier": user.get("effective_tier"),
        "is_owner": bool(user.get("is_owner")),
        "lifetime": bool(user.get("lifetime")),
        "billing_cycle": user.get("billing_cycle"),
        "subscription_until": user.get("subscription_until"),
        "created_at": user.get("created_at"),
        "lifetime_offer_status": user.get("lifetime_offer_status"),
        "referral_code": user.get("referral_code"),
        "payout_email": user.get("payout_email"),
        "payout_method": user.get("payout_method"),
        "stripe_customer_id": user.get("stripe_customer_id"),
    }


@app.context_processor
def inject_stripe_flag():
    return {"stripe_enabled": STRIPE_ENABLED}


# ============================================================================
# AFFILIATE HELPERS
# ============================================================================
def generate_referral_code():
    return "".join(secrets.choice(REFERRAL_ALPHABET) for _ in range(REFERRAL_LENGTH))


def ensure_referral_code(user):
    """Lazy-backfill a unique referral code for a user that doesn't have one."""
    if user.get("referral_code"):
        return user["referral_code"]
    db = get_db()
    for _ in range(20):
        code = generate_referral_code()
        try:
            db.execute("UPDATE users SET referral_code = ? WHERE id = ?",
                       (code, user["id"]))
            db.commit()
            user["referral_code"] = code
            return code
        except sqlite3.IntegrityError:
            continue
    raise RuntimeError("Could not generate unique referral code")


def hash_ip(ip):
    salt = app.config.get("SECRET_KEY", "")
    return hashlib.sha256((ip + salt).encode("utf-8")).hexdigest()[:32]


def _valid_ref_code(code):
    return bool(code) and bool(re.fullmatch(r"[A-Z0-9]{4,12}", code.upper()))


def attribute_signup(new_user_id):
    """If a referral cookie is set and valid, attribute the new user to that referrer."""
    code = request.cookies.get("erh_ref")
    if not code or not _valid_ref_code(code):
        return
    code = code.upper()
    db = get_db()
    referrer = db.execute(
        "SELECT id FROM users WHERE referral_code = ?", (code,)
    ).fetchone()
    if not referrer or referrer["id"] == new_user_id:
        return
    db.execute("UPDATE users SET referred_by_user_id = ? WHERE id = ?",
               (referrer["id"], new_user_id))
    db.execute(
        """INSERT INTO referral_conversions
           (referrer_user_id, referred_user_id, event_type, gross_amount, commission_amount, status)
           VALUES (?, ?, 'signup', 0, 0, 'paid')""",
        (referrer["id"], new_user_id),
    )
    db.commit()


def record_subscription_commission(user, plan, cycle, gross):
    """If the paying user was referred, log a commission entry for the referrer."""
    if not user.get("referred_by_user_id"):
        return
    commission = round(gross * COMMISSION_PCT, 2)
    event = "lifetime" if plan == "lifetime" else "subscription"
    get_db().execute(
        """INSERT INTO referral_conversions
           (referrer_user_id, referred_user_id, event_type, plan, cycle,
            gross_amount, commission_amount, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')""",
        (user["referred_by_user_id"], user["id"], event, plan, cycle, gross, commission),
    )
    get_db().commit()


@app.before_request
def capture_referral_param():
    """If ?ref=CODE is on any URL, log a click and stash the cookie."""
    ref = request.args.get("ref")
    if not ref or not _valid_ref_code(ref):
        return
    code = ref.upper()
    referrer = get_db().execute(
        "SELECT id FROM users WHERE referral_code = ?", (code,)
    ).fetchone()
    if not referrer:
        return
    user = current_user()
    if user and user.get("referral_code") == code:
        return  # self-referral
    # De-duplicate clicks per cookie session
    if request.cookies.get("erh_ref") != code:
        ip = request.headers.get("X-Forwarded-For", request.remote_addr or "")
        get_db().execute(
            """INSERT INTO referral_clicks (referral_code, ip_hash, user_agent, referer)
               VALUES (?, ?, ?, ?)""",
            (code, hash_ip(ip), (request.user_agent.string or "")[:200],
             (request.referrer or "")[:200]),
        )
        get_db().commit()
    g._set_ref_cookie = code


@app.after_request
def set_referral_cookie(response):
    code = getattr(g, "_set_ref_cookie", None)
    if code:
        response.set_cookie(
            "erh_ref", code, max_age=COOKIE_TTL_SECONDS,
            httponly=True, samesite="Lax",
        )
    return response


def anonymize_email(email):
    if not email or "@" not in email:
        return "—"
    local, domain = email.split("@", 1)
    visible = local[:2] if len(local) >= 2 else local[0]
    return f"{visible}{'*' * max(3, len(local) - len(visible))}@{domain}"


# ============================================================================
# CHAT HELPERS
# ============================================================================
def has_required_tier(user, required):
    if user.get("is_owner") or user.get("lifetime"):
        return True
    user_lvl = TIER_HIERARCHY.get(user.get("effective_tier") or "free", 0)
    req_lvl = TIER_HIERARCHY.get(required or "free", 0)
    return user_lvl >= req_lvl


def chat_rate_limit_ok(user_id, tier):
    if tier == "patron":
        return True, None
    limit = CHAT_RATE_LIMITS.get(tier or "free")
    if limit is None:
        return True, None
    max_msgs, window = limit
    cutoff = (datetime.now(tz=timezone.utc) - timedelta(seconds=window)).isoformat()
    n = get_db().execute(
        "SELECT COUNT(*) AS c FROM chat_messages WHERE user_id = ? AND created_at > ?",
        (user_id, cutoff),
    ).fetchone()["c"]
    if n >= max_msgs:
        mins = window // 60
        return False, (f"You've sent {max_msgs} messages in the last {mins} minutes. "
                       "Upgrade to Patron for unlimited chat.")
    return True, None


def touch_user_activity(user_id):
    try:
        get_db().execute(
            "UPDATE users SET last_active = ? WHERE id = ?",
            (datetime.now(tz=timezone.utc).isoformat(), user_id),
        )
        get_db().commit()
    except Exception:
        pass


def display_username(row):
    return row.get("display_name") or (row.get("email") or "").split("@")[0] or "user"


def chat_user_badges(row):
    is_owner = bool(row.get("is_owner"))
    lifetime = bool(row.get("lifetime"))
    tier = row.get("tier") or "free"
    # owner override (matches current_user logic)
    if (row.get("email") or "").lower() == OWNER_EMAIL:
        is_owner = True
    if is_owner or lifetime:
        effective = "patron"
    else:
        effective = tier
    return {"tier": effective, "is_owner": is_owner, "lifetime": lifetime}


def serialize_message(row):
    badges = chat_user_badges(row)
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "username": display_username(row),
        "body": row["body"],
        "created_at": row["created_at"],
        **badges,
    }


@app.context_processor
def inject_user():
    return {"user": user_for_template(current_user())}


# ============================================================================
# LIFETIME OFFER EXPIRATION
# ============================================================================
@app.before_request
def expire_lifetime_offer_if_navigated_away():
    if not session.get("offer_armed"):
        return
    if request.path == "/subscribe":
        return
    if (request.path.startswith("/api/")
        or request.path.startswith("/static/")
        or request.path == "/favicon.ico"
        or request.path == "/logout"):
        return
    user = current_user()
    if user and user.get("lifetime_offer_status") == "available":
        db = get_db()
        db.execute("UPDATE users SET lifetime_offer_status = 'expired' WHERE id = ?",
                   (user["id"],))
        db.commit()
        user["lifetime_offer_status"] = "expired"
    session.pop("offer_armed", None)


# ============================================================================
# DATA FETCHERS (yfinance + SEC EDGAR)
# ============================================================================
@lru_cache(maxsize=1)
def _ticker_cik_map():
    try:
        r = requests.get("https://www.sec.gov/files/company_tickers.json",
                         headers=SEC_TICKER_HEADERS, timeout=10)
        r.raise_for_status()
        return {e["ticker"].upper(): str(e["cik_str"]).zfill(10) for e in r.json().values()}
    except Exception:
        return {}


def _num(value):
    if value is None:
        return None
    try:
        f = float(value)
        if f != f:
            return None
        return f
    except (TypeError, ValueError):
        return None


def _series(df, candidates):
    if df is None or getattr(df, "empty", True):
        return []
    for name in candidates:
        if name in df.index:
            row = df.loc[name]
            out = []
            for col, val in row.items():
                v = _num(val)
                if v is None:
                    continue
                d = col.strftime("%Y-%m-%d") if hasattr(col, "strftime") else str(col)
                out.append({"date": d, "value": v})
            out.sort(key=lambda x: x["date"])
            return out
    return []


def get_overview(t, info):
    officers = info.get("companyOfficers") or []
    ceo = None
    for o in officers:
        title = (o.get("title") or "").lower()
        if "chief executive" in title or title.strip() == "ceo":
            ceo = o.get("name"); break
    if not ceo and officers:
        ceo = officers[0].get("name")
    return {
        "ticker": t.ticker.upper(),
        "name": info.get("longName") or info.get("shortName") or t.ticker.upper(),
        "exchange": info.get("exchange") or info.get("fullExchangeName"),
        "sector": info.get("sector"),
        "industry": info.get("industry"),
        "country": info.get("country"),
        "city": info.get("city"),
        "state": info.get("state"),
        "website": info.get("website"),
        "employees": info.get("fullTimeEmployees"),
        "summary": info.get("longBusinessSummary"),
        "ceo": ceo,
    }


def get_quote(info):
    price = info.get("currentPrice") or info.get("regularMarketPrice")
    prev = info.get("previousClose") or info.get("regularMarketPreviousClose")
    change = price - prev if (price is not None and prev is not None) else None
    change_pct = (change / prev * 100) if (change is not None and prev) else None
    return {
        "price": _num(price), "change": _num(change), "changePct": _num(change_pct),
        "previousClose": _num(prev),
        "open": _num(info.get("open") or info.get("regularMarketOpen")),
        "dayLow": _num(info.get("dayLow") or info.get("regularMarketDayLow")),
        "dayHigh": _num(info.get("dayHigh") or info.get("regularMarketDayHigh")),
        "fiftyTwoWeekLow": _num(info.get("fiftyTwoWeekLow")),
        "fiftyTwoWeekHigh": _num(info.get("fiftyTwoWeekHigh")),
        "volume": _num(info.get("volume") or info.get("regularMarketVolume")),
        "averageVolume": _num(info.get("averageVolume")),
        "marketCap": _num(info.get("marketCap")),
        "sharesOutstanding": _num(info.get("sharesOutstanding")),
        "currency": info.get("currency", "USD"),
    }


def get_valuation(info, t=None):
    market_cap = _num(info.get("marketCap"))
    fcf = _num(info.get("freeCashflow"))
    fcf_yield = (fcf / market_cap) if (fcf and market_cap) else None

    # ROIC = NOPAT / Invested Capital. Approximate using available info.
    op_income = _num(info.get("operatingCashflow"))
    ebit = _num(info.get("ebitda"))  # rough proxy
    total_debt = _num(info.get("totalDebt")) or 0
    cash = _num(info.get("totalCash")) or 0
    equity = _num(info.get("totalStockholderEquity") or info.get("bookValue"))
    roic = None
    if t is not None:
        try:
            inc = t.financials
            bs = t.balance_sheet
            if inc is not None and not inc.empty and bs is not None and not bs.empty:
                ni_row = None
                for k in ("Net Income", "Net Income Common Stockholders"):
                    if k in inc.index:
                        ni_row = inc.loc[k].iloc[0]
                        break
                eq_row = None
                for k in ("Stockholders Equity", "Total Stockholder Equity"):
                    if k in bs.index:
                        eq_row = bs.loc[k].iloc[0]
                        break
                debt_row = None
                for k in ("Total Debt", "Long Term Debt"):
                    if k in bs.index:
                        debt_row = bs.loc[k].iloc[0]
                        break
                if ni_row is not None and eq_row:
                    invested = float(eq_row) + float(debt_row or 0)
                    if invested:
                        roic = (float(ni_row) * 0.79) / invested  # rough after-tax adjustment
        except Exception:
            roic = None

    return {
        "peRatio": _num(info.get("trailingPE")),
        "forwardPE": _num(info.get("forwardPE")),
        "pegRatio": _num(info.get("pegRatio") or info.get("trailingPegRatio")),
        "priceToBook": _num(info.get("priceToBook")),
        "priceToSales": _num(info.get("priceToSalesTrailing12Months")),
        "evToRevenue": _num(info.get("enterpriseToRevenue")),
        "evToEbitda": _num(info.get("enterpriseToEbitda")),
        "profitMargin": _num(info.get("profitMargins")),
        "operatingMargin": _num(info.get("operatingMargins")),
        "grossMargin": _num(info.get("grossMargins")),
        "returnOnEquity": _num(info.get("returnOnEquity")),
        "returnOnAssets": _num(info.get("returnOnAssets")),
        "returnOnInvestedCapital": _num(roic),
        "fcfYield": _num(fcf_yield),
        "debtToEquity": _num(info.get("debtToEquity")),
        "currentRatio": _num(info.get("currentRatio")),
        "quickRatio": _num(info.get("quickRatio")),
        "dividendYield": _num(info.get("dividendYield")),
        "payoutRatio": _num(info.get("payoutRatio")),
        "beta": _num(info.get("beta")),
        "eps": _num(info.get("trailingEps")),
        "forwardEps": _num(info.get("forwardEps")),
        "bookValue": _num(info.get("bookValue")),
        "revenueGrowth": _num(info.get("revenueGrowth")),
        "earningsGrowth": _num(info.get("earningsGrowth")),
        "freeCashFlowTtm": _num(fcf),
        "totalCashTtm": _num(info.get("totalCash")),
        "totalDebtTtm": _num(info.get("totalDebt")),
    }


def get_intraday(t):
    """Today's price action. 5-minute bars over the last day."""
    try:
        h = t.history(period="1d", interval="5m", auto_adjust=False, prepost=False)
    except Exception:
        return []
    if h is None or h.empty:
        return []
    out = []
    for ts, row in h.iterrows():
        c = _num(row.get("Close"))
        v = _num(row.get("Volume"))
        if c is None:
            continue
        out.append({
            "time": ts.strftime("%Y-%m-%d %H:%M"),
            "close": c,
            "volume": v,
        })
    return out


def get_volume_analysis(history):
    """Basic volume and price action analysis from daily history."""
    if not history or len(history) < 5:
        return {}
    closes = [p["close"] for p in history]
    last = closes[-1]
    high_52 = max(closes)
    low_52 = min(closes)
    pct_off_high = ((high_52 - last) / high_52 * 100) if high_52 else None
    pct_off_low = ((last - low_52) / low_52 * 100) if low_52 else None
    # Volatility, std dev of daily returns annualized
    rets = []
    for i in range(1, len(closes)):
        if closes[i - 1]:
            rets.append((closes[i] - closes[i - 1]) / closes[i - 1])
    if rets:
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / len(rets)
        ann_vol = (var ** 0.5) * (252 ** 0.5) * 100
    else:
        ann_vol = None
    return {
        "pctOffHigh": _num(pct_off_high),
        "pctOffLow": _num(pct_off_low),
        "annualizedVolatility": _num(ann_vol),
        "totalReturn1Y": _num(((last - closes[0]) / closes[0] * 100) if closes[0] else None),
    }


def get_moving_averages(history):
    """Calculate SMA 50 and SMA 200 for each point in history."""
    if not history:
        return {"sma50": [], "sma200": []}
    closes = [p["close"] for p in history]
    dates = [p["date"] for p in history]
    def sma(window):
        out = []
        for i in range(len(closes)):
            if i + 1 >= window:
                avg = sum(closes[i + 1 - window:i + 1]) / window
                out.append({"date": dates[i], "value": avg})
        return out
    return {"sma50": sma(50), "sma200": sma(200)}


def get_forecasts(t, info):
    """Forward looking estimates from yfinance."""
    out = {
        "targetMeanPrice": _num(info.get("targetMeanPrice")),
        "earningsGrowth": _num(info.get("earningsGrowth")),
        "revenueGrowth": _num(info.get("revenueGrowth")),
        "forwardEps": _num(info.get("forwardEps")),
        "forwardPE": _num(info.get("forwardPE")),
        "trailingEps": _num(info.get("trailingEps")),
        "items": [],
    }
    try:
        et = getattr(t, "earnings_estimate", None)
        if et is not None and not et.empty:
            for period, row in et.iterrows():
                avg = _num(row.get("avg")) or _num(row.get("estimate"))
                if avg is None:
                    continue
                out["items"].append({
                    "period": str(period),
                    "type": "EPS estimate",
                    "value": avg,
                    "low": _num(row.get("low")),
                    "high": _num(row.get("high")),
                    "analysts": _num(row.get("numberOfAnalysts")),
                })
    except Exception:
        pass
    try:
        rt = getattr(t, "revenue_estimate", None)
        if rt is not None and not rt.empty:
            for period, row in rt.iterrows():
                avg = _num(row.get("avg")) or _num(row.get("estimate"))
                if avg is None:
                    continue
                out["items"].append({
                    "period": str(period),
                    "type": "Revenue estimate",
                    "value": avg,
                    "low": _num(row.get("low")),
                    "high": _num(row.get("high")),
                    "analysts": _num(row.get("numberOfAnalysts")),
                })
    except Exception:
        pass
    return out


def get_earnings_calendar(t, info):
    """Next earnings date and recent earnings history with surprises."""
    out = {"nextDate": None, "estEps": None, "estRevenue": None, "history": []}
    try:
        cal = getattr(t, "calendar", None)
        if cal is not None and isinstance(cal, dict):
            ed = cal.get("Earnings Date")
            if isinstance(ed, list) and ed:
                first = ed[0]
                out["nextDate"] = first.strftime("%Y-%m-%d") if hasattr(first, "strftime") else str(first)
            out["estEps"] = _num(cal.get("Earnings Average") or cal.get("EPS Average"))
            out["estRevenue"] = _num(cal.get("Revenue Average"))
    except Exception:
        pass
    try:
        eh = getattr(t, "earnings_history", None)
        if eh is not None and not eh.empty:
            for ts, row in eh.tail(8).iterrows():
                date_str = ts.strftime("%Y-%m-%d") if hasattr(ts, "strftime") else str(ts)
                out["history"].append({
                    "date": date_str,
                    "epsEstimate": _num(row.get("epsEstimate")),
                    "epsActual": _num(row.get("epsActual")),
                    "epsDifference": _num(row.get("epsDifference")),
                    "surprisePct": _num(row.get("surprisePercent") or row.get("surprisePct")),
                })
    except Exception:
        pass
    return out


def get_dividend_schedule(ticker_symbol, t=None, info=None):
    """Dividend tracking info for a single ticker: upcoming dates, last payment, frequency."""
    try:
        if t is None:
            t = yf.Ticker(ticker_symbol)
        if info is None:
            info = t.info or {}
    except Exception:
        return None
    if not info:
        return None

    def _ts_to_date(ts):
        if not ts:
            return None
        try:
            ts = int(ts)
            if ts <= 0:
                return None
            return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        except Exception:
            return None

    ex_date = _ts_to_date(info.get("exDividendDate"))
    pay_date = _ts_to_date(info.get("dividendDate"))
    div_rate = _num(info.get("dividendRate"))
    div_yield = _num(info.get("dividendYield"))
    last_amt = _num(info.get("lastDividendValue"))
    last_date = _ts_to_date(info.get("lastDividendDate"))
    payout = _num(info.get("payoutRatio"))
    five_yr_yield = _num(info.get("fiveYearAvgDividendYield"))

    recent = []
    frequency = None
    try:
        divs = t.dividends
        if divs is not None and not divs.empty:
            tail = divs.tail(8)
            for ts, amount in tail.items():
                date_str = ts.strftime("%Y-%m-%d") if hasattr(ts, "strftime") else str(ts)
                recent.append({"date": date_str, "amount": float(amount)})
            # Infer payout frequency from gaps between the last few dividends
            from datetime import datetime as _dt
            dts = []
            for entry in recent[-5:]:
                try:
                    dts.append(_dt.strptime(entry["date"], "%Y-%m-%d"))
                except Exception:
                    pass
            if len(dts) >= 2:
                gaps = [(dts[i + 1] - dts[i]).days for i in range(len(dts) - 1)]
                avg_gap = sum(gaps) / len(gaps)
                if avg_gap < 45:
                    frequency = "Monthly"
                elif avg_gap < 130:
                    frequency = "Quarterly"
                elif avg_gap < 220:
                    frequency = "Semi-annual"
                else:
                    frequency = "Annual"
            # If yfinance lacks lastDividendValue, fall back to most recent historical row
            if not last_amt and recent:
                last_amt = recent[-1]["amount"]
            if not last_date and recent:
                last_date = recent[-1]["date"]
    except Exception:
        pass

    pays_dividend = bool((div_rate and div_rate > 0) or last_amt)

    return {
        "ticker": ticker_symbol,
        "company_name": info.get("shortName") or info.get("longName") or ticker_symbol,
        "ex_date": ex_date,
        "pay_date": pay_date,
        "last_amount": last_amt,
        "last_date": last_date,
        "annual_rate": div_rate,
        "yield_pct": div_yield,
        "five_yr_yield": five_yr_yield,
        "payout_ratio": payout,
        "frequency": frequency,
        "pays_dividend": pays_dividend,
        "recent_dividends": recent,
    }


def get_esg(t):
    """Environmental, social, governance scores from yfinance."""
    try:
        s = t.sustainability
    except Exception:
        s = None
    if s is None or getattr(s, "empty", True):
        return None
    try:
        col = s.columns[0]
        d = s[col].to_dict()
        return {
            "totalEsg": _num(d.get("totalEsg")),
            "environmentScore": _num(d.get("environmentScore")),
            "socialScore": _num(d.get("socialScore")),
            "governanceScore": _num(d.get("governanceScore")),
            "esgPerformance": d.get("esgPerformance"),
            "controversyLevel": _num(d.get("highestControversy")),
            "peerCount": _num(d.get("peerCount")),
        }
    except Exception:
        return None


# Curated peer map by sector / industry, lightweight defaults
SECTOR_PEERS = {
    "Technology": ["AAPL", "MSFT", "GOOGL", "META", "NVDA", "ORCL", "ADBE", "CRM"],
    "Financial Services": ["JPM", "BAC", "WFC", "C", "GS", "MS", "BLK", "AXP"],
    "Healthcare": ["JNJ", "UNH", "LLY", "PFE", "MRK", "ABBV", "ABT", "TMO"],
    "Consumer Cyclical": ["AMZN", "TSLA", "HD", "MCD", "NKE", "SBUX", "BKNG", "LOW"],
    "Communication Services": ["GOOGL", "META", "DIS", "NFLX", "CMCSA", "VZ", "T"],
    "Energy": ["XOM", "CVX", "COP", "SLB", "EOG", "PSX", "MPC", "OXY"],
    "Industrials": ["BA", "CAT", "DE", "GE", "HON", "RTX", "UPS", "LMT"],
    "Consumer Defensive": ["WMT", "PG", "KO", "PEP", "COST", "PM", "MO", "CL"],
    "Real Estate": ["AMT", "PLD", "CCI", "EQIX", "PSA", "O", "SPG", "WELL"],
    "Utilities": ["NEE", "DUK", "SO", "AEP", "EXC", "XEL", "SRE", "D"],
    "Basic Materials": ["LIN", "SHW", "FCX", "APD", "ECL", "NEM", "DOW", "DD"],
}

SECTOR_ETFS = {
    "Technology": "XLK", "Financial Services": "XLF", "Healthcare": "XLV",
    "Consumer Cyclical": "XLY", "Communication Services": "XLC", "Energy": "XLE",
    "Industrials": "XLI", "Consumer Defensive": "XLP", "Real Estate": "XLRE",
    "Utilities": "XLU", "Basic Materials": "XLB",
}


def get_peers(ticker, sector):
    if not sector or sector not in SECTOR_PEERS:
        return []
    candidates = [t for t in SECTOR_PEERS[sector] if t.upper() != ticker.upper()][:5]
    out = []
    for sym in candidates:
        try:
            tk = yf.Ticker(sym)
            i = tk.info or {}
            if not (i.get("longName") or i.get("regularMarketPrice")):
                continue
            out.append({
                "ticker": sym,
                "name": i.get("longName") or i.get("shortName") or sym,
                "price": _num(i.get("currentPrice") or i.get("regularMarketPrice")),
                "marketCap": _num(i.get("marketCap")),
                "peRatio": _num(i.get("trailingPE")),
                "priceToBook": _num(i.get("priceToBook")),
                "profitMargin": _num(i.get("profitMargins")),
                "returnOnEquity": _num(i.get("returnOnEquity")),
                "revenueGrowth": _num(i.get("revenueGrowth")),
                "dividendYield": _num(i.get("dividendYield")),
            })
        except Exception:
            continue
    return out


def get_sector_performance(sector):
    """Pull the sector ETF's recent performance for context."""
    etf = SECTOR_ETFS.get(sector)
    if not etf:
        return None
    try:
        tk = yf.Ticker(etf)
        i = tk.info or {}
        h = tk.history(period="1y", interval="1d", auto_adjust=False)
        if h is None or h.empty:
            return None
        first = float(h["Close"].iloc[0])
        last = float(h["Close"].iloc[-1])
        ret_1y = ((last - first) / first * 100) if first else None
        ret_1m = None
        if len(h) > 22:
            ago = float(h["Close"].iloc[-22])
            ret_1m = ((last - ago) / ago * 100) if ago else None
        return {
            "etf": etf,
            "name": i.get("longName") or i.get("shortName") or etf,
            "price": _num(last),
            "return1Y": _num(ret_1y),
            "return1M": _num(ret_1m),
        }
    except Exception:
        return None


def get_capital_events(filings):
    """Extract M&A, buyback, debt issuance signals from recent 8-K filing descriptions."""
    if not filings.get("available"):
        return []
    keywords = {
        "Acquisition": ["acquisition", "merger", "acquire"],
        "Buyback": ["repurchase", "buyback", "share repurchase"],
        "Debt": ["notes offering", "senior notes", "credit facility", "debt issuance", "indenture"],
        "Dividend": ["dividend declar", "dividend increase"],
        "Restructuring": ["restructuring", "layoff", "workforce reduction"],
    }
    out = []
    for f in filings["categorized"].get("8-K", []):
        desc = (f.get("description") or "").lower()
        for label, kws in keywords.items():
            if any(k in desc for k in kws):
                out.append({
                    "type": label,
                    "date": f["filingDate"],
                    "description": f.get("description"),
                    "url": f["documentUrl"],
                })
                break
    return out


def get_financials(t):
    inc, bs, cf = t.financials, t.balance_sheet, t.cashflow
    return {
        "income": {
            "revenue": _series(inc, ["Total Revenue", "Revenue"]),
            "grossProfit": _series(inc, ["Gross Profit"]),
            "operatingIncome": _series(inc, ["Operating Income"]),
            "netIncome": _series(inc, ["Net Income", "Net Income Common Stockholders"]),
            "ebitda": _series(inc, ["EBITDA", "Normalized EBITDA"]),
        },
        "balance": {
            "totalAssets": _series(bs, ["Total Assets"]),
            "totalLiabilities": _series(bs, ["Total Liabilities Net Minority Interest", "Total Liab"]),
            "totalEquity": _series(bs, ["Stockholders Equity", "Total Stockholder Equity"]),
            "cash": _series(bs, ["Cash And Cash Equivalents", "Cash"]),
            "totalDebt": _series(bs, ["Total Debt", "Long Term Debt"]),
        },
        "cash": {
            "operating": _series(cf, ["Operating Cash Flow", "Total Cash From Operating Activities"]),
            "investing": _series(cf, ["Investing Cash Flow", "Total Cashflows From Investing Activities"]),
            "financing": _series(cf, ["Financing Cash Flow", "Total Cash From Financing Activities"]),
            "freeCashFlow": _series(cf, ["Free Cash Flow"]),
            "capex": _series(cf, ["Capital Expenditure", "Capital Expenditures"]),
        },
    }


def get_history(t):
    try:
        hist = t.history(period="1y", interval="1d", auto_adjust=False)
    except Exception:
        return []
    if hist is None or hist.empty:
        return []
    out = []
    for d, row in hist.iterrows():
        c = _num(row.get("Close"))
        if c is not None:
            out.append({"date": d.strftime("%Y-%m-%d"), "close": c})
    return out


def get_news(t):
    try:
        items = t.news or []
    except Exception:
        items = []
    out = []
    for item in items[:12]:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if isinstance(content, dict):
            title = content.get("title")
            url = (content.get("canonicalUrl") or {}).get("url") or (content.get("clickThroughUrl") or {}).get("url")
            publisher = (content.get("provider") or {}).get("displayName")
            published = content.get("pubDate") or content.get("displayTime")
        else:
            title = item.get("title"); url = item.get("link")
            publisher = item.get("publisher"); published = item.get("providerPublishTime")
            if isinstance(published, (int, float)):
                published = datetime.fromtimestamp(published, tz=timezone.utc).isoformat()
        if title and url:
            out.append({"title": title, "url": url, "publisher": publisher, "published": published})
    return out


def get_holders(t):
    out = []
    try:
        df = t.institutional_holders
    except Exception:
        df = None
    if df is None or df.empty:
        return out
    for _, row in df.head(10).iterrows():
        date_rep = row.get("Date Reported")
        out.append({
            "holder": row.get("Holder"),
            "shares": _num(row.get("Shares")),
            "value": _num(row.get("Value")),
            "pctOut": _num(row.get("pctHeld") or row.get("% Out")),
            "dateReported": date_rep.strftime("%Y-%m-%d") if hasattr(date_rep, "strftime") else None,
        })
    return out


def get_recommendations(t):
    try:
        df = t.recommendations_summary
    except Exception:
        df = None
    if df is None or df.empty:
        return None
    try:
        row = df.iloc[0].to_dict()
        return {
            "strongBuy": int(row.get("strongBuy", 0) or 0),
            "buy": int(row.get("buy", 0) or 0),
            "hold": int(row.get("hold", 0) or 0),
            "sell": int(row.get("sell", 0) or 0),
            "strongSell": int(row.get("strongSell", 0) or 0),
        }
    except Exception:
        return None


def get_analyst_targets(info):
    return {
        "targetMean": _num(info.get("targetMeanPrice")),
        "targetHigh": _num(info.get("targetHighPrice")),
        "targetLow": _num(info.get("targetLowPrice")),
        "targetMedian": _num(info.get("targetMedianPrice")),
        "numAnalysts": info.get("numberOfAnalystOpinions"),
        "recommendationKey": info.get("recommendationKey"),
    }


def get_sec_filings(ticker_symbol):
    cik_map = _ticker_cik_map()
    cik = cik_map.get(ticker_symbol.upper())
    if not cik:
        return {"available": False,
                "reason": "Ticker not found in SEC EDGAR (may be a foreign issuer or non-reporting)."}
    try:
        r = requests.get(f"https://data.sec.gov/submissions/CIK{cik}.json",
                         headers=SEC_HEADERS, timeout=10)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        return {"available": False, "reason": f"SEC EDGAR request failed: {e}"}
    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accession = recent.get("accessionNumber", [])
    primary_doc = recent.get("primaryDocument", [])
    descriptions = recent.get("primaryDocDescription", [])
    categorized = {"10-K": [], "10-Q": [], "8-K": [], "DEF 14A": [], "other": []}
    for i, form in enumerate(forms):
        if i >= len(dates) or i >= len(accession) or i >= len(primary_doc):
            continue
        acc = accession[i].replace("-", "")
        item = {
            "form": form,
            "filingDate": dates[i],
            "accession": accession[i],
            "documentUrl": f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}/{primary_doc[i]}",
            "description": descriptions[i] if i < len(descriptions) else None,
        }
        target = categorized.get(form, categorized["other"])
        if len(target) < 10:
            target.append(item)
    return {"available": True, "cik": cik, "name": data.get("name"),
            "sicDescription": data.get("sicDescription"),
            "categorized": categorized,
            "edgarUrl": f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}"}


def get_legal_signals(filings):
    signals = []
    if not filings.get("available"):
        return signals
    cat = filings["categorized"]
    for f in cat.get("8-K", [])[:8]:
        signals.append({"type": "8-K · Material Event", "date": f["filingDate"],
                        "url": f["documentUrl"], "description": f.get("description")})
    if cat.get("10-K"):
        f = cat["10-K"][0]
        signals.append({"type": "10-K · Annual Report", "date": f["filingDate"],
                        "url": f["documentUrl"],
                        "description": "Review Item 3 (Legal Proceedings) and Item 1A (Risk Factors)"})
    return signals


# ============================================================================
# RESEARCH GATING (feature-based, not count-based)
# ============================================================================
def can_lookup(user):
    """Everyone with an account can look up any stock. Free tier is feature-gated, not count-gated."""
    return True, None


def filter_payload_by_tier(payload, tier):
    """Feature gating: free gets every public-data feature, premium gates are AI/journal/alerts/news/holders."""
    payload["tier"] = tier
    if tier == "patron":
        return payload
    if tier == "apprentice":
        # Apprentice gets news, basic SEC filings (10-K), but no analyst/legal/capital events/holders
        payload["holders"] = []
        payload["recommendations"] = None
        payload["analystTargets"] = None
        payload["legalSignals"] = []
        payload["capitalEvents"] = []
        if payload.get("filings", {}).get("available"):
            cat = payload["filings"]["categorized"]
            payload["filings"]["categorized"] = {
                "10-K": cat.get("10-K", []),
                "10-Q": cat.get("10-Q", []),
                "8-K": [], "DEF 14A": [], "other": [],
            }
        payload["locked"] = ["holders", "analyst", "legal", "fullFilings", "capitalEvents"]
        return payload
    # Free tier: full market data (overview, quote, history, financials, charts, valuation,
    # peers, sector, ESG). Locked features stay locked because that's where the value is.
    payload["news"] = []
    payload["holders"] = []
    payload["recommendations"] = None
    payload["analystTargets"] = None
    payload["forecasts"] = {"items": []}
    payload["earningsCalendar"] = {"history": []}
    payload["capitalEvents"] = []
    payload["legalSignals"] = []
    if payload.get("filings", {}).get("available"):
        cat = payload["filings"]["categorized"]
        payload["filings"]["filingsCount"] = sum(len(v) for v in cat.values())
        payload["filings"]["categorized"] = {"10-K": [], "10-Q": [], "8-K": [], "DEF 14A": [], "other": []}
    payload["locked"] = [
        "aiBrief", "researchJournal", "alerts", "filings", "news",
        "holders", "analyst", "legal", "forecasts", "earnings",
        "capitalEvents", "pdfExport",
    ]
    return payload


# ============================================================================
# ROUTES — pages
# ============================================================================
@app.route("/")
def index():
    user = current_user()
    watchlist = []
    if user:
        rows = get_db().execute(
            "SELECT ticker FROM watchlist WHERE user_id = ? ORDER BY added_at DESC LIMIT 6",
            (user["id"],)
        ).fetchall()
        watchlist = [r["ticker"] for r in rows]
    return render_template(
        "index.html",
        watchlist=watchlist,
        user_json=json.dumps(user_for_template(user)),
        prefilled_ticker=request.args.get("ticker", "").upper(),
    )


@app.route("/signup", methods=["GET", "POST"])
def signup_page():
    if current_user():
        return redirect(url_for("index"))
    next_url = request.values.get("next") or url_for("index")
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        display = (request.form.get("display_name") or "").strip()
        ctx = {"email": email, "display_name": display, "next_url": next_url}
        if not email or not password:
            flash("Email and password are required.", "error"); return render_template("signup.html", **ctx)
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            flash("Enter a valid email address.", "error"); return render_template("signup.html", **ctx)
        if len(password) < 8:
            flash("Password must be at least 8 characters.", "error"); return render_template("signup.html", **ctx)
        db = get_db()
        if db.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
            flash("That email is already registered.", "error"); return render_template("signup.html", **ctx)
        is_owner = 1 if email == OWNER_EMAIL else 0
        # Pre-generate a referral code so new users have one immediately
        ref_code = None
        for _ in range(20):
            candidate = generate_referral_code()
            existing = db.execute("SELECT 1 FROM users WHERE referral_code = ?", (candidate,)).fetchone()
            if not existing:
                ref_code = candidate
                break
        cur = db.execute(
            """INSERT INTO users (email, password_hash, display_name, is_owner, referral_code)
               VALUES (?, ?, ?, ?, ?)""",
            (email, generate_password_hash(password), display or email.split("@")[0], is_owner, ref_code),
        )
        db.commit()
        new_id = cur.lastrowid
        attribute_signup(new_id)  # honor ?ref= cookie if present
        session["user_id"] = new_id
        session.permanent = True
        flash("Welcome aboard. Free tier is active, try a search.", "success")
        return redirect(next_url)
    return render_template("signup.html", next_url=next_url)


@app.route("/login", methods=["GET", "POST"])
def login_page():
    if current_user():
        return redirect(url_for("index"))
    next_url = request.values.get("next") or url_for("index")
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        row = get_db().execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if not row or not check_password_hash(row["password_hash"], password):
            flash("Invalid email or password.", "error")
            return render_template("login.html", email=email, next_url=next_url)
        session["user_id"] = row["id"]
        session.permanent = True
        return redirect(next_url)
    return render_template("login.html", next_url=next_url)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("Signed out.", "info")
    return redirect(url_for("index"))


@app.route("/account")
@login_required
def account_page():
    return render_template("account.html")


@app.route("/account/cancel", methods=["POST"])
@login_required
def cancel_subscription():
    user = current_user()
    if user.get("is_owner"):
        flash("Owner accounts always have access, nothing to cancel.", "info")
        return redirect(url_for("account_page"))
    if user.get("lifetime"):
        flash("Lifetime access can't be cancelled.", "info")
        return redirect(url_for("account_page"))
    db = get_db()
    db.execute("""UPDATE users SET tier='free', subscription_until=NULL, billing_cycle=NULL
                  WHERE id=?""", (user["id"],))
    db.commit()
    flash("Subscription cancelled. You're now on free tier.", "info")
    return redirect(url_for("account_page"))


@app.route("/watchlist")
@login_required
def watchlist_page():
    user = current_user()
    rows = get_db().execute(
        """SELECT ticker, notes, added_at FROM watchlist
           WHERE user_id = ? ORDER BY added_at DESC""",
        (user["id"],)
    ).fetchall()
    return render_template(
        "watchlist.html",
        watchlist=[dict(r) for r in rows],
        free_limit=FREE_WATCHLIST_LIMIT,
    )


@app.route("/dividends")
@login_required
def dividends_page():
    """Dividend schedule for stocks in the user's watchlist."""
    user = current_user()
    rows = get_db().execute(
        """SELECT ticker FROM watchlist
           WHERE user_id = ? ORDER BY added_at DESC""",
        (user["id"],),
    ).fetchall()

    paying_upcoming = []
    paying_past = []
    non_paying = []
    failed = []

    today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    for row in rows:
        ticker = row["ticker"]
        try:
            data = get_dividend_schedule(ticker)
        except Exception:
            failed.append(ticker)
            continue
        if data is None:
            failed.append(ticker)
            continue
        if not data["pays_dividend"]:
            non_paying.append(data)
            continue
        # Bucket by whether ex-date or pay-date is still ahead
        ex = data.get("ex_date") or ""
        pay = data.get("pay_date") or ""
        if (ex and ex >= today_iso) or (pay and pay >= today_iso):
            paying_upcoming.append(data)
        else:
            paying_past.append(data)

    def _next_event_key(d):
        for key in ("ex_date", "pay_date", "last_date"):
            v = d.get(key)
            if v:
                return v
        return "9999-99-99"

    paying_upcoming.sort(key=_next_event_key)
    # Past payments: most recent first
    paying_past.sort(key=lambda d: d.get("last_date") or d.get("pay_date") or "", reverse=True)

    return render_template(
        "dividends.html",
        upcoming=paying_upcoming,
        recent=paying_past,
        non_paying=non_paying,
        failed=failed,
        watchlist_empty=(len(rows) == 0),
    )


@app.route("/r/<code>")
def referral_redirect(code):
    if not _valid_ref_code(code or ""):
        return redirect(url_for("index"))
    return redirect(url_for("index", ref=code.upper()))


@app.route("/affiliate")
@login_required
def affiliate_page():
    user = current_user()
    code = ensure_referral_code(user)
    db = get_db()

    clicks = db.execute(
        "SELECT COUNT(*) c FROM referral_clicks WHERE referral_code = ?", (code,)
    ).fetchone()["c"]
    signups = db.execute(
        """SELECT COUNT(*) c FROM referral_conversions
           WHERE referrer_user_id = ? AND event_type = 'signup'""",
        (user["id"],),
    ).fetchone()["c"]
    paid_conv = db.execute(
        """SELECT COUNT(*) c FROM referral_conversions
           WHERE referrer_user_id = ? AND event_type IN ('subscription','lifetime')""",
        (user["id"],),
    ).fetchone()["c"]
    earnings = db.execute(
        """SELECT
              COALESCE(SUM(CASE WHEN status='pending' THEN commission_amount END), 0) AS pending,
              COALESCE(SUM(CASE WHEN status='paid'    THEN commission_amount END), 0) AS paid,
              COALESCE(SUM(commission_amount), 0) AS total
           FROM referral_conversions WHERE referrer_user_id = ?""",
        (user["id"],),
    ).fetchone()
    recent_rows = db.execute(
        """SELECT c.event_type, c.plan, c.cycle, c.gross_amount, c.commission_amount,
                  c.status, c.created_at, u.email AS referred_email
           FROM referral_conversions c
           LEFT JOIN users u ON c.referred_user_id = u.id
           WHERE c.referrer_user_id = ?
           ORDER BY c.created_at DESC
           LIMIT 25""",
        (user["id"],),
    ).fetchall()
    recent = []
    for r in recent_rows:
        d = dict(r)
        d["referred_email"] = anonymize_email(d.get("referred_email"))
        recent.append(d)

    base = request.host_url.rstrip("/")
    conv_rate = (paid_conv / clicks * 100) if clicks else 0

    return render_template(
        "affiliate.html",
        referral_code=code,
        referral_link=f"{base}/?ref={code}",
        short_link=f"{base}/r/{code}",
        clicks=clicks,
        signups=signups,
        paid_conversions=paid_conv,
        conversion_rate=conv_rate,
        pending_earnings=earnings["pending"],
        paid_earnings=earnings["paid"],
        total_earnings=earnings["total"],
        recent=recent,
        commission_pct=int(COMMISSION_PCT * 100),
        commission_duration=COMMISSION_DURATION_MONTHS,
        cookie_days=int(COOKIE_TTL_SECONDS / 86400),
        payout_minimum=PAYOUT_MINIMUM,
        plans=PLANS,
        lifetime_price=LIFETIME_PRICE,
    )


@app.route("/affiliate/payout", methods=["POST"])
@login_required
def save_payout_info():
    user = current_user()
    payout_email = (request.form.get("payout_email") or "").strip()
    payout_method = request.form.get("payout_method") or "paypal"
    if payout_email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", payout_email):
        flash("Enter a valid payout email.", "error")
        return redirect(url_for("affiliate_page"))
    if payout_method not in ("paypal", "wise", "stripe", "ach"):
        payout_method = "paypal"
    db = get_db()
    db.execute(
        "UPDATE users SET payout_email = ?, payout_method = ? WHERE id = ?",
        (payout_email, payout_method, user["id"]),
    )
    db.commit()
    flash("Payout settings saved.", "success")
    return redirect(url_for("affiliate_page"))


@app.route("/affiliate/payout/request", methods=["POST"])
@login_required
def request_payout():
    user = current_user()
    db = get_db()
    earnings = db.execute(
        """SELECT COALESCE(SUM(commission_amount),0) AS pending
           FROM referral_conversions
           WHERE referrer_user_id = ? AND status = 'pending'""",
        (user["id"],),
    ).fetchone()
    if (earnings["pending"] or 0) < PAYOUT_MINIMUM:
        flash(f"You need at least ${PAYOUT_MINIMUM:.0f} in pending earnings to request a payout.", "error")
    elif not user.get("payout_email"):
        flash("Add a payout email below first.", "error")
    else:
        flash(
            f"Payout request submitted for ${earnings['pending']:.2f}. "
            "We'll process it within 5 business days.", "success",
        )
    return redirect(url_for("affiliate_page"))


@app.route("/api/dcf", methods=["POST"])
@login_required
def api_dcf():
    """Simple two-stage discounted cash flow calculator."""
    data = request.get_json(silent=True) or {}
    try:
        fcf = float(data.get("fcf"))
        growth_high = float(data.get("growthHigh", 10)) / 100
        growth_term = float(data.get("growthTerm", 3)) / 100
        years_high = int(data.get("yearsHigh", 5))
        discount = float(data.get("discount", 9)) / 100
        shares = float(data.get("shares", 0))
        net_debt = float(data.get("netDebt", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid inputs."}), 400
    if discount <= growth_term:
        return jsonify({"error": "Discount rate must exceed terminal growth rate."}), 400

    pv = 0.0
    yearly = []
    cur = fcf
    for yr in range(1, years_high + 1):
        cur = cur * (1 + growth_high)
        disc = cur / ((1 + discount) ** yr)
        pv += disc
        yearly.append({"year": yr, "fcf": cur, "discounted": disc})
    terminal_fcf = cur * (1 + growth_term)
    terminal_value = terminal_fcf / (discount - growth_term)
    pv_terminal = terminal_value / ((1 + discount) ** years_high)
    enterprise = pv + pv_terminal
    equity_value = enterprise - net_debt
    intrinsic_per_share = equity_value / shares if shares else None
    return jsonify({
        "yearly": yearly,
        "terminalValue": terminal_value,
        "pvOfTerminal": pv_terminal,
        "enterpriseValue": enterprise,
        "equityValue": equity_value,
        "intrinsicPerShare": intrinsic_per_share,
        "assumptions": {
            "fcf": fcf, "growthHigh": growth_high * 100, "growthTerm": growth_term * 100,
            "yearsHigh": years_high, "discount": discount * 100,
            "shares": shares, "netDebt": net_debt,
        },
    })


@app.route("/api/intraday/<ticker>")
@login_required
def api_intraday(ticker):
    user = current_user()
    if user["effective_tier"] == "free":
        return jsonify({"error": "Intraday requires Apprentice or higher.", "upgradeRequired": True}), 403
    raw = (ticker or "").strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", raw):
        return jsonify({"error": "Invalid ticker."}), 400
    try:
        t = yf.Ticker(raw)
        return jsonify({"ticker": raw, "intraday": get_intraday(t)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/research/export/<ticker>")
@login_required
def api_export_csv(ticker):
    """Download a research bundle as CSV."""
    user = current_user()
    if user["effective_tier"] == "free":
        return jsonify({"error": "Export requires Apprentice or higher.", "upgradeRequired": True}), 403
    raw = (ticker or "").strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", raw):
        return jsonify({"error": "Invalid ticker."}), 400
    try:
        t = yf.Ticker(raw)
        info = t.info or {}
        if not info:
            return jsonify({"error": "No data."}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    overview = get_overview(t, info)
    quote = get_quote(info)
    val = get_valuation(info, t)
    fin = get_financials(t)

    lines = []
    lines.append(f"Equity Research Hub Export, {raw}, {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append("")
    lines.append("Section,Field,Value")
    for k, v in overview.items():
        if v is not None and not isinstance(v, (list, dict)):
            lines.append(f"Overview,{k},\"{str(v).replace(chr(34), '')}\"")
    for k, v in quote.items():
        if v is not None:
            lines.append(f"Quote,{k},{v}")
    for k, v in val.items():
        if v is not None:
            lines.append(f"Valuation,{k},{v}")
    lines.append("")
    lines.append("Income Statement")
    lines.append("Metric,Year,Value")
    for metric, series in fin["income"].items():
        for p in series:
            lines.append(f"{metric},{p['date'][:4]},{p['value']}")
    lines.append("")
    lines.append("Balance Sheet")
    lines.append("Metric,Year,Value")
    for metric, series in fin["balance"].items():
        for p in series:
            lines.append(f"{metric},{p['date'][:4]},{p['value']}")
    lines.append("")
    lines.append("Cash Flow")
    lines.append("Metric,Year,Value")
    for metric, series in fin["cash"].items():
        for p in series:
            lines.append(f"{metric},{p['date'][:4]},{p['value']}")

    from flask import Response
    return Response(
        "\n".join(lines),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={raw}_research.csv"},
    )


@app.route("/api/watchlist/position", methods=["POST"])
@login_required
def api_watchlist_position():
    """Add or update shares + average cost on a watchlist row."""
    user = current_user()
    data = request.get_json(silent=True) or {}
    ticker = (data.get("ticker") or "").strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", ticker):
        return jsonify({"error": "Invalid ticker."}), 400
    shares = data.get("shares")
    avg_cost = data.get("avgCost")
    notes = data.get("notes")
    try:
        shares = float(shares) if shares not in (None, "") else None
        avg_cost = float(avg_cost) if avg_cost not in (None, "") else None
    except (TypeError, ValueError):
        return jsonify({"error": "Shares and average cost must be numbers."}), 400
    db = get_db()
    db.execute(
        """INSERT INTO watchlist (user_id, ticker, shares, avg_cost, notes)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(user_id, ticker) DO UPDATE SET
             shares = excluded.shares,
             avg_cost = excluded.avg_cost,
             notes = COALESCE(excluded.notes, watchlist.notes)""",
        (user["id"], ticker, shares, avg_cost, notes),
    )
    db.commit()
    return jsonify({"success": True, "ticker": ticker})


@app.route("/alerts")
@login_required
def alerts_page():
    user = current_user()
    rows = get_db().execute(
        """SELECT id, ticker, kind, threshold, note, status, triggered_at, created_at
           FROM alerts WHERE user_id = ? ORDER BY status DESC, created_at DESC""",
        (user["id"],),
    ).fetchall()
    return render_template("alerts.html", alerts=[dict(r) for r in rows])


@app.route("/api/alerts", methods=["GET", "POST"])
@login_required
def api_alerts():
    user = current_user()
    if request.method == "GET":
        rows = get_db().execute(
            "SELECT * FROM alerts WHERE user_id = ? ORDER BY created_at DESC",
            (user["id"],),
        ).fetchall()
        return jsonify({"alerts": [dict(r) for r in rows]})
    data = request.get_json(silent=True) or {}
    ticker = (data.get("ticker") or "").strip().upper()
    kind = data.get("kind")
    try:
        threshold = float(data.get("threshold"))
    except (TypeError, ValueError):
        return jsonify({"error": "Threshold must be a number."}), 400
    note = (data.get("note") or "").strip()
    if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", ticker):
        return jsonify({"error": "Invalid ticker."}), 400
    if kind not in ("price_above", "price_below", "pe_below", "volume_above"):
        return jsonify({"error": "Invalid alert type."}), 400
    db = get_db()
    cur = db.execute(
        """INSERT INTO alerts (user_id, ticker, kind, threshold, note)
           VALUES (?, ?, ?, ?, ?)""",
        (user["id"], ticker, kind, threshold, note),
    )
    db.commit()
    return jsonify({"success": True, "id": cur.lastrowid})


@app.route("/api/alerts/<int:alert_id>", methods=["DELETE"])
@login_required
def api_alerts_delete(alert_id):
    user = current_user()
    db = get_db()
    db.execute("DELETE FROM alerts WHERE id = ? AND user_id = ?", (alert_id, user["id"]))
    db.commit()
    return jsonify({"success": True})


@app.route("/portfolio")
@login_required
def portfolio_page():
    """Portfolio dashboard with positions, current prices, and total return."""
    user = current_user()
    rows = get_db().execute(
        """SELECT ticker, notes, shares, avg_cost, added_at FROM watchlist
           WHERE user_id = ? AND shares IS NOT NULL AND shares > 0
           ORDER BY added_at DESC""",
        (user["id"],),
    ).fetchall()
    positions = []
    total_value = 0.0
    total_cost = 0.0
    for r in rows:
        pos = dict(r)
        try:
            tk = yf.Ticker(pos["ticker"])
            i = tk.info or {}
            price = _num(i.get("currentPrice") or i.get("regularMarketPrice"))
        except Exception:
            price = None
        pos["current_price"] = price
        if price and pos["shares"]:
            pos["market_value"] = price * pos["shares"]
            total_value += pos["market_value"]
            if pos["avg_cost"]:
                pos["cost_basis"] = pos["avg_cost"] * pos["shares"]
                pos["gain_loss"] = pos["market_value"] - pos["cost_basis"]
                pos["gain_loss_pct"] = (pos["gain_loss"] / pos["cost_basis"] * 100) if pos["cost_basis"] else None
                total_cost += pos["cost_basis"]
        positions.append(pos)
    total_return = total_value - total_cost if total_cost else None
    total_return_pct = (total_return / total_cost * 100) if total_cost else None
    return render_template(
        "portfolio.html",
        positions=positions,
        total_value=total_value,
        total_cost=total_cost,
        total_return=total_return,
        total_return_pct=total_return_pct,
    )


@app.route("/community")
@login_required
def community_home():
    return redirect(url_for("community_room", slug="general"))


@app.route("/community/<slug>")
@login_required
def community_room(slug):
    user = current_user()
    db = get_db()
    rooms_rows = db.execute(
        "SELECT * FROM chat_rooms ORDER BY sort_order, category, name"
    ).fetchall()
    all_rooms = [dict(r) for r in rooms_rows]
    if not all_rooms:
        flash("Chat rooms aren't set up yet.", "error")
        return redirect(url_for("index"))

    room = next((r for r in all_rooms if r["slug"] == slug), None)
    if not room:
        flash(f"Room '{slug}' not found.", "error")
        return redirect(url_for("community_home"))

    locked = not has_required_tier(user, room["tier_required"])
    messages = []
    last_msg_id = 0
    if not locked:
        rows = db.execute(
            """SELECT m.id, m.user_id, m.body, m.created_at,
                      u.display_name, u.email, u.tier, u.is_owner, u.lifetime
               FROM chat_messages m
               JOIN users u ON m.user_id = u.id
               WHERE m.room_id = ?
               ORDER BY m.id DESC LIMIT 100""",
            (room["id"],),
        ).fetchall()
        messages = [serialize_message(dict(r)) for r in reversed(rows)]
        last_msg_id = messages[-1]["id"] if messages else 0
        touch_user_activity(user["id"])

    # Group rooms by category for the sidebar
    rooms_by_category = {}
    for r in all_rooms:
        cat = r.get("category") or "Other"
        rooms_by_category.setdefault(cat, []).append(r)

    return render_template(
        "community.html",
        all_rooms=all_rooms,
        rooms_by_category=rooms_by_category,
        current_room=room,
        messages=messages,
        last_message_id=last_msg_id,
        locked=locked,
        max_message_len=CHAT_MAX_MESSAGE_LEN,
    )


@app.route("/api/chat/<slug>/messages")
@login_required
def api_chat_messages(slug):
    user = current_user()
    db = get_db()
    room = db.execute("SELECT * FROM chat_rooms WHERE slug = ?", (slug,)).fetchone()
    if not room:
        return jsonify({"error": "Room not found."}), 404
    room = dict(room)
    if not has_required_tier(user, room["tier_required"]):
        return jsonify({"error": "Locked.", "locked": True}), 403
    try:
        since = int(request.args.get("since", "0"))
    except (TypeError, ValueError):
        since = 0
    rows = db.execute(
        """SELECT m.id, m.user_id, m.body, m.created_at,
                  u.display_name, u.email, u.tier, u.is_owner, u.lifetime
           FROM chat_messages m
           JOIN users u ON m.user_id = u.id
           WHERE m.room_id = ? AND m.id > ?
           ORDER BY m.id ASC LIMIT 200""",
        (room["id"], since),
    ).fetchall()
    touch_user_activity(user["id"])
    return jsonify({"messages": [serialize_message(dict(r)) for r in rows]})


@app.route("/api/chat/<slug>/send", methods=["POST"])
@login_required
def api_chat_send(slug):
    user = current_user()
    db = get_db()
    room = db.execute("SELECT * FROM chat_rooms WHERE slug = ?", (slug,)).fetchone()
    if not room:
        return jsonify({"error": "Room not found."}), 404
    room = dict(room)
    if not has_required_tier(user, room["tier_required"]):
        tier_label = (room["tier_required"] or "free").capitalize()
        return jsonify({
            "error": f"This room is for {tier_label} members only.",
            "locked": True,
            "upgradeUrl": url_for("subscribe_page"),
        }), 403

    data = request.get_json(silent=True) or {}
    body = (data.get("body") or "").strip()
    if not body:
        return jsonify({"error": "Message can't be empty."}), 400
    if len(body) > CHAT_MAX_MESSAGE_LEN:
        return jsonify({"error": f"Message too long. Max {CHAT_MAX_MESSAGE_LEN} characters."}), 400

    # Block exact duplicate of the user's previous message in this room
    last = db.execute(
        """SELECT body FROM chat_messages
           WHERE user_id = ? AND room_id = ?
           ORDER BY id DESC LIMIT 1""",
        (user["id"], room["id"]),
    ).fetchone()
    if last and last["body"].strip() == body:
        return jsonify({"error": "Don't send the same message twice in a row."}), 429

    ok, msg = chat_rate_limit_ok(user["id"], user["effective_tier"])
    if not ok:
        return jsonify({
            "error": msg, "rateLimited": True,
            "upgradeUrl": url_for("subscribe_page"),
        }), 429

    cur = db.execute(
        "INSERT INTO chat_messages (room_id, user_id, body) VALUES (?, ?, ?)",
        (room["id"], user["id"], body),
    )
    db.commit()
    new_id = cur.lastrowid
    touch_user_activity(user["id"])
    row = db.execute(
        """SELECT m.id, m.user_id, m.body, m.created_at,
                  u.display_name, u.email, u.tier, u.is_owner, u.lifetime
           FROM chat_messages m
           JOIN users u ON m.user_id = u.id
           WHERE m.id = ?""",
        (new_id,),
    ).fetchone()
    return jsonify({"success": True, "message": serialize_message(dict(row))})


# ============================================================================
# AI COMPANY BRIEF (flagship Pro feature)
# ============================================================================
def _fmt_money(n):
    if n is None:
        return "unknown"
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "unknown"
    if abs(n) >= 1e12: return f"${n/1e12:.2f}T"
    if abs(n) >= 1e9:  return f"${n/1e9:.2f}B"
    if abs(n) >= 1e6:  return f"${n/1e6:.2f}M"
    return f"${n:,.0f}"


def _generate_ai_brief(ticker, t, info, filings, financials):
    if not ANTHROPIC_ENABLED:
        return None
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    name = info.get("longName") or info.get("shortName") or ticker
    sector = info.get("sector") or "Unknown"
    industry = info.get("industry") or "Unknown"
    employees = info.get("fullTimeEmployees")
    summary = (info.get("longBusinessSummary") or "")[:2000]
    market_cap = info.get("marketCap")
    revenue = (financials.get("income", {}).get("revenue") or [{}])[-1].get("value") if financials.get("income", {}).get("revenue") else None
    net_income = (financials.get("income", {}).get("netIncome") or [{}])[-1].get("value") if financials.get("income", {}).get("netIncome") else None
    fcf = (financials.get("cash", {}).get("freeCashFlow") or [{}])[-1].get("value") if financials.get("cash", {}).get("freeCashFlow") else None
    debt = (financials.get("balance", {}).get("totalDebt") or [{}])[-1].get("value") if financials.get("balance", {}).get("totalDebt") else None
    pe = info.get("trailingPE")
    profit_margin = info.get("profitMargins")

    recent_events = []
    cat = (filings or {}).get("categorized", {})
    for f in (cat.get("8-K") or [])[:6]:
        recent_events.append(f"  - {f['filingDate']}: {f.get('description') or '8-K material event'}")
    if cat.get("10-K"):
        recent_events.append(f"  - {cat['10-K'][0]['filingDate']}: Most recent 10-K annual report")

    prompt = f"""You are an equity analyst writing a research brief for an individual investor researching {name} ({ticker}). Use only the data below. Do not invent numbers, ratings, or events not listed.

COMPANY DATA:
- Sector: {sector}
- Industry: {industry}
- Employees: {employees if employees else "unknown"}
- Market cap: {_fmt_money(market_cap)}
- Latest annual revenue: {_fmt_money(revenue)}
- Latest annual net income: {_fmt_money(net_income)}
- Latest free cash flow: {_fmt_money(fcf)}
- Total debt: {_fmt_money(debt)}
- Trailing P/E: {f"{pe:.1f}" if pe else "n/a"}
- Profit margin: {f"{profit_margin*100:.1f}%" if profit_margin else "n/a"}

BUSINESS DESCRIPTION (from 10-K):
{summary}

RECENT SEC FILINGS (chronological, most recent first):
{chr(10).join(recent_events) if recent_events else "  - No recent filings available"}

YOUR TASK:
Write a 3-paragraph plain-English brief for a retail investor who's never heard of this company. Each paragraph 4 to 6 sentences.

Paragraph 1, "What it does": Explain the business model in clear language. Who pays them and for what. What's the core product or service. Don't just rephrase the description, distill it.

Paragraph 2, "Financial picture": Use the actual numbers. Comment on profitability, leverage, and capital efficiency. Whether the business looks healthy on the numbers alone. Cite specifics.

Paragraph 3, "Recent moves and risks": What material events have happened recently per the 8-K filings. What are the known risk factors based on the business and sector. What should an investor watch.

Rules:
- No price targets, no buy/sell recommendations, no "should you invest"
- Cite specific numbers from the data above
- Plain English, no jargon. If you must use a term, define it
- Do not say anything that isn't supported by the data above
- End with a single line: "Sources: 10-K filing, recent 8-K disclosures, market data"
"""
    try:
        message = client.messages.create(
            model=AI_BRIEF_MODEL,
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        text = message.content[0].text.strip()
        return text
    except Exception as e:
        app.logger.exception("AI brief generation failed: %s", e)
        return None


def _ai_brief_quota_check(user):
    """Apprentice gets APPRENTICE_AI_BRIEFS_PER_DAY, Patron/Lifetime/Owner unlimited."""
    if user.get("is_owner") or user.get("lifetime") or user["effective_tier"] == "patron":
        return True, None
    if user["effective_tier"] == "free":
        return False, "AI briefs are a Pro feature. Upgrade to Apprentice or Patron to unlock."
    # apprentice
    cutoff = (datetime.now(tz=timezone.utc) - timedelta(hours=24)).isoformat()
    used = get_db().execute(
        "SELECT COUNT(*) AS c FROM ai_brief_usage WHERE user_id = ? AND used_at > ?",
        (user["id"], cutoff),
    ).fetchone()["c"]
    if used >= APPRENTICE_AI_BRIEFS_PER_DAY:
        return False, f"Daily AI brief limit reached ({APPRENTICE_AI_BRIEFS_PER_DAY}). Upgrade to Patron for unlimited."
    return True, None


@app.route("/api/ai-brief/<ticker>")
@login_required
def api_ai_brief(ticker):
    user = current_user()
    raw = (ticker or "").strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", raw):
        return jsonify({"error": "Invalid ticker."}), 400

    ok, msg = _ai_brief_quota_check(user)
    if not ok:
        return jsonify({"error": msg, "upgradeRequired": True,
                        "upgradeUrl": url_for("subscribe_page")}), 403

    if not ANTHROPIC_ENABLED:
        return jsonify({
            "error": "AI briefs aren't configured on this server. Set the ANTHROPIC_API_KEY environment variable.",
            "configMissing": True,
        }), 503

    db = get_db()
    # Check cache (briefs are good for 24h)
    cutoff = (datetime.now(tz=timezone.utc) - timedelta(hours=AI_BRIEF_CACHE_HOURS)).isoformat()
    cached = db.execute(
        "SELECT brief, generated_at FROM ai_briefs WHERE ticker = ? AND generated_at > ?",
        (raw, cutoff),
    ).fetchone()
    if cached:
        db.execute(
            "INSERT INTO ai_brief_usage (user_id, ticker) VALUES (?, ?)",
            (user["id"], raw),
        )
        db.commit()
        return jsonify({
            "ticker": raw, "brief": cached["brief"],
            "generatedAt": cached["generated_at"], "cached": True,
        })

    # Generate fresh
    try:
        t = yf.Ticker(raw)
        info = t.info or {}
    except Exception as e:
        return jsonify({"error": f"Failed to fetch data: {e}"}), 500
    if not info:
        return jsonify({"error": f"No data for {raw}."}), 404

    filings = get_sec_filings(raw)
    financials = get_financials(t)
    brief = _generate_ai_brief(raw, t, info, filings, financials)
    if not brief:
        return jsonify({"error": "Could not generate brief. Try again in a moment."}), 500

    now = datetime.now(tz=timezone.utc).isoformat()
    db.execute(
        """INSERT INTO ai_briefs (ticker, brief, generated_at) VALUES (?, ?, ?)
           ON CONFLICT(ticker) DO UPDATE SET brief = excluded.brief, generated_at = excluded.generated_at""",
        (raw, brief, now),
    )
    db.execute("INSERT INTO ai_brief_usage (user_id, ticker) VALUES (?, ?)", (user["id"], raw))
    db.commit()
    return jsonify({"ticker": raw, "brief": brief, "generatedAt": now, "cached": False})


# ============================================================================
# RESEARCH JOURNAL
# ============================================================================
@app.route("/journal")
@login_required
def journal_page():
    user = current_user()
    rows = get_db().execute(
        """SELECT id, ticker, thesis, what_must_be_true, what_changes_mind,
                  entry_target, exit_target, notes, revision_number, created_at, updated_at
           FROM research_notes WHERE user_id = ? ORDER BY updated_at DESC""",
        (user["id"],),
    ).fetchall()
    return render_template(
        "journal.html",
        notes=[dict(r) for r in rows],
        apprentice_limit=APPRENTICE_JOURNAL_TICKERS,
    )


@app.route("/journal/<ticker>")
@login_required
def journal_ticker(ticker):
    user = current_user()
    raw = (ticker or "").strip().upper()
    note = get_db().execute(
        "SELECT * FROM research_notes WHERE user_id = ? AND ticker = ?",
        (user["id"], raw),
    ).fetchone()
    revisions = []
    if note:
        revisions = get_db().execute(
            "SELECT id, snapshot, saved_at FROM note_revisions WHERE note_id = ? ORDER BY saved_at DESC LIMIT 20",
            (note["id"],),
        ).fetchall()
        revisions = [dict(r) for r in revisions]
    return render_template(
        "journal_entry.html",
        ticker=raw,
        note=dict(note) if note else None,
        revisions=revisions,
    )


@app.route("/api/journal", methods=["GET", "POST"])
@login_required
def api_journal():
    user = current_user()
    db = get_db()
    if request.method == "GET":
        rows = db.execute(
            "SELECT * FROM research_notes WHERE user_id = ? ORDER BY updated_at DESC",
            (user["id"],),
        ).fetchall()
        return jsonify({"notes": [dict(r) for r in rows]})

    if user["effective_tier"] == "free":
        return jsonify({
            "error": "The research journal is a Pro feature. Upgrade to save your thesis on any stock.",
            "upgradeRequired": True,
            "upgradeUrl": url_for("subscribe_page"),
        }), 403

    data = request.get_json(silent=True) or {}
    ticker = (data.get("ticker") or "").strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", ticker):
        return jsonify({"error": "Invalid ticker."}), 400

    # Apprentice gets a journal ticker cap
    if user["effective_tier"] == "apprentice":
        existing = db.execute(
            "SELECT 1 FROM research_notes WHERE user_id = ? AND ticker = ?",
            (user["id"], ticker),
        ).fetchone()
        if not existing:
            count = db.execute(
                "SELECT COUNT(DISTINCT ticker) AS c FROM research_notes WHERE user_id = ?",
                (user["id"],),
            ).fetchone()["c"]
            if count >= APPRENTICE_JOURNAL_TICKERS:
                return jsonify({
                    "error": f"Apprentice tier allows journals on up to {APPRENTICE_JOURNAL_TICKERS} stocks. Upgrade to Patron for unlimited.",
                    "upgradeRequired": True,
                    "upgradeUrl": url_for("subscribe_page"),
                }), 403

    fields = {
        "thesis": (data.get("thesis") or "").strip()[:5000],
        "what_must_be_true": (data.get("whatMustBeTrue") or "").strip()[:3000],
        "what_changes_mind": (data.get("whatChangesMind") or "").strip()[:3000],
        "notes": (data.get("notes") or "").strip()[:10000],
    }
    try:
        entry_target = float(data["entryTarget"]) if data.get("entryTarget") not in (None, "") else None
        exit_target = float(data["exitTarget"]) if data.get("exitTarget") not in (None, "") else None
    except (TypeError, ValueError):
        return jsonify({"error": "Entry and exit targets must be numbers."}), 400

    now = datetime.now(tz=timezone.utc).isoformat()
    existing = db.execute(
        "SELECT id, revision_number FROM research_notes WHERE user_id = ? AND ticker = ?",
        (user["id"], ticker),
    ).fetchone()
    if existing:
        # Snapshot previous version into revisions
        prev = db.execute("SELECT * FROM research_notes WHERE id = ?", (existing["id"],)).fetchone()
        if prev:
            db.execute(
                "INSERT INTO note_revisions (note_id, snapshot) VALUES (?, ?)",
                (existing["id"], json.dumps(dict(prev), default=str)),
            )
        new_rev = (existing["revision_number"] or 1) + 1
        db.execute(
            """UPDATE research_notes
               SET thesis=?, what_must_be_true=?, what_changes_mind=?,
                   entry_target=?, exit_target=?, notes=?,
                   revision_number=?, updated_at=?
               WHERE id=?""",
            (fields["thesis"], fields["what_must_be_true"], fields["what_changes_mind"],
             entry_target, exit_target, fields["notes"], new_rev, now, existing["id"]),
        )
        note_id = existing["id"]
    else:
        cur = db.execute(
            """INSERT INTO research_notes
               (user_id, ticker, thesis, what_must_be_true, what_changes_mind,
                entry_target, exit_target, notes, revision_number, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
            (user["id"], ticker, fields["thesis"], fields["what_must_be_true"],
             fields["what_changes_mind"], entry_target, exit_target,
             fields["notes"], now, now),
        )
        note_id = cur.lastrowid
    db.commit()
    return jsonify({"success": True, "id": note_id, "ticker": ticker})


@app.route("/api/journal/<int:note_id>", methods=["DELETE"])
@login_required
def api_journal_delete(note_id):
    user = current_user()
    get_db().execute(
        "DELETE FROM research_notes WHERE id = ? AND user_id = ?",
        (note_id, user["id"]),
    )
    get_db().commit()
    return jsonify({"success": True})


# ============================================================================
# COMPARISON TOOL
# ============================================================================
@app.route("/compare")
@login_required
def compare_page():
    tickers_param = request.args.get("tickers", "")
    tickers = [t.strip().upper() for t in tickers_param.split(",") if t.strip()]
    tickers = [t for t in tickers if re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", t)][:4]
    rows = []
    if tickers:
        for sym in tickers:
            try:
                tk = yf.Ticker(sym)
                i = tk.info or {}
                if not (i.get("longName") or i.get("regularMarketPrice")):
                    continue
                rows.append({
                    "ticker": sym,
                    "name": i.get("longName") or i.get("shortName") or sym,
                    "sector": i.get("sector"),
                    "price": _num(i.get("currentPrice") or i.get("regularMarketPrice")),
                    "marketCap": _num(i.get("marketCap")),
                    "peRatio": _num(i.get("trailingPE")),
                    "forwardPE": _num(i.get("forwardPE")),
                    "pegRatio": _num(i.get("pegRatio") or i.get("trailingPegRatio")),
                    "priceToBook": _num(i.get("priceToBook")),
                    "priceToSales": _num(i.get("priceToSalesTrailing12Months")),
                    "evToEbitda": _num(i.get("enterpriseToEbitda")),
                    "profitMargin": _num(i.get("profitMargins")),
                    "operatingMargin": _num(i.get("operatingMargins")),
                    "grossMargin": _num(i.get("grossMargins")),
                    "returnOnEquity": _num(i.get("returnOnEquity")),
                    "returnOnAssets": _num(i.get("returnOnAssets")),
                    "debtToEquity": _num(i.get("debtToEquity")),
                    "revenueGrowth": _num(i.get("revenueGrowth")),
                    "earningsGrowth": _num(i.get("earningsGrowth")),
                    "dividendYield": _num(i.get("dividendYield")),
                    "beta": _num(i.get("beta")),
                    "fiftyTwoWeekChangePct": _num(i.get("52WeekChange") or i.get("fiftyTwoWeekChange")),
                })
            except Exception:
                continue
    return render_template("compare.html", tickers=tickers, rows=rows)


# ============================================================================
# PRINT-FRIENDLY RESEARCH VIEW (export to PDF via browser)
# ============================================================================
@app.route("/research/<ticker>/print")
@login_required
def research_print(ticker):
    user = current_user()
    if user["effective_tier"] == "free":
        flash("PDF export is a Pro feature. Upgrade to print your research.", "error")
        return redirect(url_for("subscribe_page"))
    raw = (ticker or "").strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", raw):
        return redirect(url_for("index"))
    try:
        t = yf.Ticker(raw)
        info = t.info or {}
    except Exception:
        flash("Could not load data.", "error")
        return redirect(url_for("index"))
    note = get_db().execute(
        "SELECT * FROM research_notes WHERE user_id = ? AND ticker = ?",
        (user["id"], raw),
    ).fetchone()
    filings = get_sec_filings(raw)
    return render_template(
        "research_print.html",
        ticker=raw,
        overview=get_overview(t, info),
        quote=get_quote(info),
        valuation=get_valuation(info, t),
        financials=get_financials(t),
        filings=filings,
        legal_signals=get_legal_signals(filings),
        capital_events=get_capital_events(filings),
        note=dict(note) if note else None,
        generated_at=datetime.now().strftime("%B %d, %Y"),
    )


@app.route("/api/chat/active-users")
@login_required
def api_chat_active_users():
    cutoff = (datetime.now(tz=timezone.utc)
              - timedelta(seconds=ACTIVE_USER_WINDOW_SECONDS)).isoformat()
    rows = get_db().execute(
        """SELECT id, email, display_name, tier, is_owner, lifetime, last_active
           FROM users
           WHERE last_active IS NOT NULL AND last_active > ?
           ORDER BY last_active DESC LIMIT 50""",
        (cutoff,),
    ).fetchall()
    users = []
    for r in rows:
        d = dict(r)
        badges = chat_user_badges(d)
        users.append({
            "username": display_username(d),
            **badges,
        })
    return jsonify({"users": users, "count": len(users)})


@app.route("/subscribe")
def subscribe_page():
    user = current_user()
    show_offer = False
    if user and user.get("lifetime_offer_status") == "available" and not user.get("is_owner"):
        show_offer = True
        session["offer_armed"] = True
    return render_template(
        "subscribe.html",
        plans=PLANS,
        lifetime_price=LIFETIME_PRICE,
        show_lifetime_offer=show_offer,
        free_watchlist_limit=FREE_WATCHLIST_LIMIT,
    )


# ============================================================================
# ROUTES — API
# ============================================================================
@app.route("/api/me")
def api_me():
    user = current_user()
    if not user:
        return jsonify({"authenticated": False})
    return jsonify({
        "authenticated": True,
        "email": user["email"],
        "displayName": user.get("display_name"),
        "tier": user["effective_tier"],
        "isOwner": bool(user.get("is_owner")),
        "lifetime": bool(user.get("lifetime")),
    })


@app.route("/api/research")
@login_required
def research():
    user = current_user()
    raw = (request.args.get("ticker") or "").strip().upper()
    if not raw or not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", raw):
        return jsonify({"error": "Please enter a valid ticker symbol."}), 400
    try:
        t = yf.Ticker(raw)
        info = t.info or {}
    except Exception as e:
        return jsonify({"error": f"Failed to fetch data: {e}"}), 500
    if not info or not (info.get("longName") or info.get("shortName") or info.get("regularMarketPrice")):
        return jsonify({"error": f"No data for ticker '{raw}'."}), 404
    filings = get_sec_filings(raw)
    history = get_history(t)
    overview = get_overview(t, info)
    payload = {
        "ticker": raw,
        "fetchedAt": datetime.now(tz=timezone.utc).isoformat(),
        "overview": overview,
        "quote": get_quote(info),
        "valuation": get_valuation(info, t),
        "financials": get_financials(t),
        "history": history,
        "intraday": get_intraday(t),
        "movingAverages": get_moving_averages(history),
        "volumeAnalysis": get_volume_analysis(history),
        "news": get_news(t),
        "holders": get_holders(t),
        "recommendations": get_recommendations(t),
        "analystTargets": get_analyst_targets(info),
        "forecasts": get_forecasts(t, info),
        "earningsCalendar": get_earnings_calendar(t, info),
        "esg": get_esg(t),
        "peers": get_peers(raw, overview.get("sector")),
        "sectorPerformance": get_sector_performance(overview.get("sector")),
        "capitalEvents": get_capital_events(filings),
        "filings": filings,
        "legalSignals": get_legal_signals(filings),
    }
    return jsonify(filter_payload_by_tier(payload, user["effective_tier"]))


@app.route("/api/watchlist", methods=["GET"])
@login_required
def api_watchlist_get():
    user = current_user()
    rows = get_db().execute(
        """SELECT ticker, notes, added_at FROM watchlist
           WHERE user_id = ? ORDER BY added_at DESC""",
        (user["id"],)
    ).fetchall()
    return jsonify({"watchlist": [dict(r) for r in rows]})


@app.route("/api/watchlist/add", methods=["POST"])
@login_required
def api_watchlist_add():
    user = current_user()
    data = request.get_json(silent=True) or {}
    ticker = (data.get("ticker") or "").strip().upper()
    notes = (data.get("notes") or "").strip()
    if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", ticker):
        return jsonify({"error": "Invalid ticker."}), 400
    db = get_db()
    if user["effective_tier"] == "free":
        existing = db.execute(
            "SELECT 1 FROM watchlist WHERE user_id=? AND ticker=?",
            (user["id"], ticker)
        ).fetchone()
        if not existing:
            count = db.execute(
                "SELECT COUNT(*) AS c FROM watchlist WHERE user_id=?",
                (user["id"],)
            ).fetchone()["c"]
            if count >= FREE_WATCHLIST_LIMIT:
                return jsonify({
                    "error": (f"Free tier saves up to {FREE_WATCHLIST_LIMIT} stocks. "
                              "Upgrade for unlimited."),
                    "upgradeRequired": True,
                    "upgradeUrl": url_for("subscribe_page"),
                }), 403
    db.execute(
        """INSERT INTO watchlist (user_id, ticker, notes) VALUES (?, ?, ?)
           ON CONFLICT(user_id, ticker) DO UPDATE SET notes = excluded.notes""",
        (user["id"], ticker, notes),
    )
    db.commit()
    return jsonify({"success": True, "ticker": ticker})


@app.route("/api/watchlist/remove", methods=["POST"])
@login_required
def api_watchlist_remove():
    user = current_user()
    data = request.get_json(silent=True) or {}
    ticker = (data.get("ticker") or "").strip().upper()
    db = get_db()
    db.execute("DELETE FROM watchlist WHERE user_id=? AND ticker=?", (user["id"], ticker))
    db.commit()
    return jsonify({"success": True, "ticker": ticker})


@app.route("/api/subscribe", methods=["POST"])
@login_required
def api_subscribe():
    """If Stripe is configured, create a Checkout Session. Otherwise simulate (dev mode)."""
    user = current_user()
    if user.get("is_owner"):
        return jsonify({"error": "Owner accounts already have full access."}), 400
    data = request.get_json(silent=True) or {}
    plan = data.get("plan")
    cycle = data.get("cycle", "monthly")
    if plan == "lifetime":
        if user.get("lifetime_offer_status") != "available":
            return jsonify({"error": "The lifetime offer is no longer available."}), 400
    elif plan not in PLANS or cycle not in ("monthly", "yearly"):
        return jsonify({"error": "Invalid plan."}), 400

    if STRIPE_ENABLED:
        return _stripe_create_checkout(user, plan, cycle)
    return _simulated_upgrade(user, plan, cycle)


def _simulated_upgrade(user, plan, cycle):
    """Local dev path: bump the user's tier directly without payment."""
    db = get_db()
    if plan == "lifetime":
        db.execute(
            """UPDATE users SET tier='patron', lifetime=1,
               billing_cycle='lifetime', subscription_until=NULL,
               lifetime_offer_status='purchased' WHERE id=?""",
            (user["id"],),
        )
        db.commit()
        session.pop("offer_armed", None)
        record_subscription_commission(user, "lifetime", "lifetime", LIFETIME_PRICE)
        return jsonify({"success": True, "tier": "patron", "lifetime": True})
    duration = timedelta(days=30) if cycle == "monthly" else timedelta(days=365)
    until = (datetime.now(tz=timezone.utc) + duration).isoformat()
    db.execute(
        "UPDATE users SET tier=?, billing_cycle=?, subscription_until=? WHERE id=?",
        (plan, cycle, until, user["id"]),
    )
    db.commit()
    gross = PLANS[plan]["yearly"] if cycle == "yearly" else PLANS[plan]["monthly"]
    record_subscription_commission(user, plan, cycle, gross)
    return jsonify({"success": True, "tier": plan, "cycle": cycle, "until": until})


def _stripe_create_checkout(user, plan, cycle):
    """Production path: redirect user to Stripe-hosted Checkout."""
    key = ("lifetime", "lifetime") if plan == "lifetime" else (plan, cycle)
    price_id = STRIPE_PRICES.get(key)
    if not price_id:
        return jsonify({"error": "Stripe price not configured. Set STRIPE_PRICE_* env vars."}), 500
    db = get_db()
    customer_id = user.get("stripe_customer_id")
    try:
        if not customer_id:
            customer = stripe.Customer.create(
                email=user["email"],
                metadata={"user_id": str(user["id"])},
            )
            customer_id = customer.id
            db.execute("UPDATE users SET stripe_customer_id = ? WHERE id = ?",
                       (customer_id, user["id"]))
            db.commit()
        base = APP_URL or request.host_url.rstrip("/")
        mode = "payment" if plan == "lifetime" else "subscription"
        checkout = stripe.checkout.Session.create(
            customer=customer_id,
            mode=mode,
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=f"{base}/account?checkout=success",
            cancel_url=f"{base}/subscribe?checkout=cancelled",
            allow_promotion_codes=True,
            metadata={
                "user_id": str(user["id"]),
                "plan": plan,
                "cycle": cycle,
            },
        )
        return jsonify({"checkoutUrl": checkout.url})
    except Exception as e:
        return jsonify({"error": f"Stripe error: {e}"}), 500


@app.route("/account/portal", methods=["POST"])
@login_required
def stripe_portal():
    """Hosted billing portal so users can manage their own subscription."""
    if not STRIPE_ENABLED:
        flash("Billing portal is not configured.", "error")
        return redirect(url_for("account_page"))
    user = current_user()
    if not user.get("stripe_customer_id"):
        flash("No billing record found.", "error")
        return redirect(url_for("account_page"))
    base = APP_URL or request.host_url.rstrip("/")
    try:
        portal = stripe.billing_portal.Session.create(
            customer=user["stripe_customer_id"],
            return_url=f"{base}/account",
        )
        return redirect(portal.url)
    except Exception as e:
        flash(f"Could not open billing portal: {e}", "error")
        return redirect(url_for("account_page"))


@app.route("/stripe/webhook", methods=["POST"])
def stripe_webhook():
    """Stripe calls this when subscription events happen."""
    if not STRIPE_ENABLED:
        return jsonify({"error": "Stripe not configured"}), 503
    payload = request.data
    sig_header = request.headers.get("Stripe-Signature", "")
    if not STRIPE_WEBHOOK_SECRET:
        return jsonify({"error": "Webhook secret not configured"}), 500
    try:
        # Verify signature only, we'll re-parse the JSON ourselves
        stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        return jsonify({"error": f"Invalid signature: {e}"}), 400

    # Parse the raw JSON ourselves so we always work with plain dicts,
    # avoiding Stripe v8+ object quirks where .get() raises AttributeError.
    try:
        raw_event = json.loads(payload.decode("utf-8") if isinstance(payload, (bytes, bytearray)) else payload)
    except (ValueError, UnicodeDecodeError) as e:
        return jsonify({"error": f"Invalid JSON: {e}"}), 400

    etype = raw_event.get("type")
    obj = (raw_event.get("data") or {}).get("object") or {}
    try:
        if etype == "checkout.session.completed":
            _stripe_handle_checkout_completed(obj)
        elif etype == "customer.subscription.updated":
            _stripe_handle_subscription_updated(obj)
        elif etype == "customer.subscription.deleted":
            _stripe_handle_subscription_deleted(obj)
        elif etype == "invoice.payment_succeeded":
            _stripe_handle_invoice_paid(obj)
    except Exception as e:
        app.logger.exception("Stripe webhook handler failed: %s", e)
        return jsonify({"error": "handler error"}), 500
    return jsonify({"received": True})


def _stripe_to_dict(obj):
    """Recursively convert a Stripe API object to plain Python types so .get() works."""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, (list, tuple)):
        return [_stripe_to_dict(v) for v in obj]
    # Dict-like (Stripe objects support bracket access via __getitem__)
    if hasattr(obj, "keys"):
        try:
            return {k: _stripe_to_dict(obj[k]) for k in list(obj.keys())}
        except (KeyError, TypeError, AttributeError):
            return {}
    return obj


def _stripe_handle_checkout_completed(session_obj):
    data = _stripe_to_dict(session_obj)
    meta = data.get("metadata") or {}
    user_id = meta.get("user_id")
    plan = meta.get("plan")
    cycle = meta.get("cycle")
    if not (user_id and plan):
        return
    user_id = int(user_id)
    db = get_db()
    user_row = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user_row:
        return
    user = dict(user_row)
    if plan == "lifetime":
        db.execute(
            """UPDATE users SET tier='patron', lifetime=1,
               billing_cycle='lifetime', subscription_until=NULL,
               lifetime_offer_status='purchased' WHERE id=?""",
            (user_id,),
        )
        db.commit()
        record_subscription_commission(user, "lifetime", "lifetime", LIFETIME_PRICE)
    else:
        sub_id = data.get("subscription")
        duration = timedelta(days=30) if cycle == "monthly" else timedelta(days=365)
        until = (datetime.now(tz=timezone.utc) + duration).isoformat()
        db.execute(
            """UPDATE users SET tier=?, billing_cycle=?,
               subscription_until=?, stripe_subscription_id=? WHERE id=?""",
            (plan, cycle, until, sub_id, user_id),
        )
        db.commit()
        gross = PLANS[plan]["yearly"] if cycle == "yearly" else PLANS[plan]["monthly"]
        record_subscription_commission(user, plan, cycle, gross)


def _stripe_handle_subscription_updated(sub):
    data = _stripe_to_dict(sub)
    sub_id = data.get("id")
    status = data.get("status")
    if not sub_id:
        return
    db = get_db()
    if status == "active":
        period_end_ts = data.get("current_period_end")
        if period_end_ts:
            period_end = datetime.fromtimestamp(period_end_ts, tz=timezone.utc)
            db.execute(
                "UPDATE users SET subscription_until = ? WHERE stripe_subscription_id = ?",
                (period_end.isoformat(), sub_id),
            )
            db.commit()
    elif status in ("canceled", "incomplete_expired", "unpaid", "past_due"):
        db.execute(
            """UPDATE users SET tier='free', subscription_until=NULL, billing_cycle=NULL
               WHERE stripe_subscription_id = ?""",
            (sub_id,),
        )
        db.commit()


def _stripe_handle_subscription_deleted(sub):
    data = _stripe_to_dict(sub)
    sub_id = data.get("id")
    if not sub_id:
        return
    db = get_db()
    db.execute(
        """UPDATE users SET tier='free', subscription_until=NULL, billing_cycle=NULL
           WHERE stripe_subscription_id = ?""",
        (sub_id,),
    )
    db.commit()


def _stripe_handle_invoice_paid(invoice):
    """Recurring monthly payment, log a commission for the affiliate (capped at 12)."""
    data = _stripe_to_dict(invoice)
    if data.get("billing_reason") == "subscription_create":
        return  # initial payment is handled by checkout.session.completed
    sub_id = data.get("subscription")
    if not sub_id:
        return
    db = get_db()
    user_row = db.execute(
        "SELECT * FROM users WHERE stripe_subscription_id = ?", (sub_id,)
    ).fetchone()
    if not user_row:
        return
    user = dict(user_row)
    if not user.get("referred_by_user_id"):
        return
    if user.get("billing_cycle") != "monthly":
        return  # yearly renewals don't generate new commissions
    existing = db.execute(
        """SELECT COUNT(*) c FROM referral_conversions
           WHERE referred_user_id = ? AND event_type = 'subscription'""",
        (user["id"],),
    ).fetchone()["c"]
    if existing >= COMMISSION_DURATION_MONTHS:
        return
    gross = (data.get("amount_paid") or 0) / 100  # Stripe uses cents
    record_subscription_commission(user, user.get("tier"), "monthly", gross)


# ============================================================================
init_db()

if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("  Equity Research Hub")
    print("  http://127.0.0.1:5000")
    print("-" * 60)
    print(f"  Owner email (gets free Patron access): {OWNER_EMAIL}")
    print("    To make YOUR email the owner, edit OWNER_EMAIL near")
    print("    the top of app.py, or set the OWNER_EMAIL env var.")
    print("    Then sign up with that email.")
    print("=" * 60 + "\n")
    app.run(host="127.0.0.1", port=5000, debug=False)
