import ast
import base64
import hashlib
import hmac
import secrets
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
import json
import math
import os
import random
import re
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))


# ============================================================
# ACCOUNTS / PLANS / STRIPE
# ============================================================

DATABASE_PATH = os.environ.get("THETA_DATABASE", "theta.db")
SESSION_DAYS = 30

# Daily server-side usage limits. A value of None means unlimited.
PLAN_LIMITS = {
    "Free": 10,
    "Pro": 100,
    "Engineer": 1000,
}

STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "").strip()
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()
STRIPE_PRO_PRICE_ID = os.environ.get("STRIPE_PRO_PRICE_ID", "").strip()
STRIPE_ENGINEER_PRICE_ID = os.environ.get("STRIPE_ENGINEER_PRICE_ID", "").strip()


def db_connect():
    connection = sqlite3.connect(
        DATABASE_PATH,
        timeout=30,
        check_same_thread=False,
    )
    connection.row_factory = sqlite3.Row
    return connection


def init_database():
    connection = db_connect()
    try:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                plan TEXT NOT NULL DEFAULT 'Free',
                stripe_customer_id TEXT,
                stripe_subscription_id TEXT,
                stripe_price_id TEXT,
                subscription_status TEXT,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS usage (
                user_id INTEGER NOT NULL,
                usage_date TEXT NOT NULL,
                count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(user_id, usage_date),
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_sessions_user_id
                ON sessions(user_id);
            CREATE INDEX IF NOT EXISTS idx_sessions_expires_at
                ON sessions(expires_at);
            """
        )
        connection.commit()
    finally:
        connection.close()


def hash_password(password):
    password = str(password)
    if len(password) < 8:
        raise ValueError("Password must be at least 8 characters.")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        310000,
    )
    return "pbkdf2_sha256$310000$" + base64.urlsafe_b64encode(salt).decode() + "$" + base64.urlsafe_b64encode(digest).decode()


def verify_password(password, stored):
    try:
        algorithm, rounds_text, salt_text, digest_text = stored.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        rounds = int(rounds_text)
        salt = base64.urlsafe_b64decode(salt_text.encode())
        expected = base64.urlsafe_b64decode(digest_text.encode())
        actual = hashlib.pbkdf2_hmac(
            "sha256",
            str(password).encode("utf-8"),
            salt,
            rounds,
        )
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


def normalize_email(email):
    return str(email or "").strip().lower()


def get_user_by_id(user_id):
    connection = db_connect()
    try:
        return connection.execute(
            "SELECT * FROM users WHERE id = ?",
            (int(user_id),),
        ).fetchone()
    finally:
        connection.close()


def get_user_by_email(email):
    connection = db_connect()
    try:
        return connection.execute(
            "SELECT * FROM users WHERE email = ?",
            (normalize_email(email),),
        ).fetchone()
    finally:
        connection.close()


def create_user(email, password):
    email = normalize_email(email)
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise ValueError("Enter a valid email address.")

    password_hash = hash_password(password)
    now = int(time.time())

    connection = db_connect()
    try:
        cursor = connection.execute(
            """
            INSERT INTO users
                (email, password_hash, plan, created_at, updated_at)
            VALUES (?, ?, 'Free', ?, ?)
            """,
            (email, password_hash, now, now),
        )
        connection.commit()
        return get_user_by_id(cursor.lastrowid)
    except sqlite3.IntegrityError:
        raise ValueError("An account with that email already exists.")
    finally:
        connection.close()


def create_session(user_id):
    raw_token = secrets.token_urlsafe(48)
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    now = int(time.time())
    expires = now + SESSION_DAYS * 86400

    connection = db_connect()
    try:
        connection.execute(
            "INSERT INTO sessions (token_hash, user_id, expires_at, created_at) VALUES (?, ?, ?, ?)",
            (token_hash, int(user_id), expires, now),
        )
        connection.commit()
    finally:
        connection.close()

    return raw_token


def get_user_from_token(raw_token):
    if not raw_token:
        return None

    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    now = int(time.time())

    connection = db_connect()
    try:
        row = connection.execute(
            """
            SELECT users.*
            FROM sessions
            JOIN users ON users.id = sessions.user_id
            WHERE sessions.token_hash = ?
              AND sessions.expires_at > ?
            """,
            (token_hash, now),
        ).fetchone()
        return row
    finally:
        connection.close()


def delete_session(raw_token):
    if not raw_token:
        return
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    connection = db_connect()
    try:
        connection.execute(
            "DELETE FROM sessions WHERE token_hash = ?",
            (token_hash,),
        )
        connection.commit()
    finally:
        connection.close()


def set_user_plan(user_id, plan, stripe_customer_id=None,
                  stripe_subscription_id=None, stripe_price_id=None,
                  subscription_status=None):
    if plan not in PLAN_LIMITS:
        plan = "Free"

    connection = db_connect()
    try:
        connection.execute(
            """
            UPDATE users
            SET plan = ?,
                stripe_customer_id = COALESCE(?, stripe_customer_id),
                stripe_subscription_id = COALESCE(?, stripe_subscription_id),
                stripe_price_id = COALESCE(?, stripe_price_id),
                subscription_status = COALESCE(?, subscription_status),
                updated_at = ?
            WHERE id = ?
            """,
            (
                plan,
                stripe_customer_id,
                stripe_subscription_id,
                stripe_price_id,
                subscription_status,
                int(time.time()),
                int(user_id),
            ),
        )
        connection.commit()
    finally:
        connection.close()


def set_user_stripe_data(user_id, customer_id=None, subscription_id=None,
                         price_id=None, status=None):
    connection = db_connect()
    try:
        connection.execute(
            """
            UPDATE users
            SET stripe_customer_id = COALESCE(?, stripe_customer_id),
                stripe_subscription_id = COALESCE(?, stripe_subscription_id),
                stripe_price_id = COALESCE(?, stripe_price_id),
                subscription_status = COALESCE(?, subscription_status),
                updated_at = ?
            WHERE id = ?
            """,
            (
                customer_id,
                subscription_id,
                price_id,
                status,
                int(time.time()),
                int(user_id),
            ),
        )
        connection.commit()
    finally:
        connection.close()


def get_user_from_stripe_ids(customer_id=None, subscription_id=None):
    connection = db_connect()
    try:
        if customer_id:
            row = connection.execute(
                "SELECT * FROM users WHERE stripe_customer_id = ?",
                (str(customer_id),),
            ).fetchone()
            if row:
                return row

        if subscription_id:
            row = connection.execute(
                "SELECT * FROM users WHERE stripe_subscription_id = ?",
                (str(subscription_id),),
            ).fetchone()
            if row:
                return row

        return None
    finally:
        connection.close()


def get_today_usage(user_id):
    today = time.strftime("%Y-%m-%d", time.gmtime())
    connection = db_connect()
    try:
        row = connection.execute(
            "SELECT count FROM usage WHERE user_id = ? AND usage_date = ?",
            (int(user_id), today),
        ).fetchone()
        return int(row["count"]) if row else 0
    finally:
        connection.close()


def consume_usage(user):
    plan = user["plan"] or "Free"
    limit = PLAN_LIMITS.get(plan, PLAN_LIMITS["Free"])
    used = get_today_usage(user["id"])

    if limit is not None and used >= limit:
        return False, used, limit

    today = time.strftime("%Y-%m-%d", time.gmtime())
    connection = db_connect()
    try:
        connection.execute(
            """
            INSERT INTO usage (user_id, usage_date, count)
            VALUES (?, ?, 1)
            ON CONFLICT(user_id, usage_date)
            DO UPDATE SET count = count + 1
            """,
            (int(user["id"]), today),
        )
        connection.commit()
    finally:
        connection.close()

    return True, used + 1, limit


def public_user(user):
    if not user:
        return {
            "authenticated": False,
            "plan": "Free",
            "limit": PLAN_LIMITS["Free"],
            "used": 0,
        }

    limit = PLAN_LIMITS.get(user["plan"], PLAN_LIMITS["Free"])
    return {
        "authenticated": True,
        "id": int(user["id"]),
        "email": user["email"],
        "plan": user["plan"],
        "used": get_today_usage(user["id"]),
        "limit": limit,
        "subscription_status": user["subscription_status"],
    }


def cookie_value(handler, name):
    header = handler.headers.get("Cookie", "")
    for piece in header.split(";"):
        piece = piece.strip()
        if piece.startswith(name + "="):
            return urllib.parse.unquote(piece.split("=", 1)[1])
    return ""


def auth_user(handler):
    return get_user_from_token(cookie_value(handler, "theta_session"))


def append_cookie(headers, name, value, max_age=None, expires=None):
    value = urllib.parse.quote(value, safe="")
    parts = [f"{name}={value}", "Path=/", "HttpOnly", "SameSite=Lax"]
    if os.environ.get("RENDER"):
        parts.append("Secure")
    if max_age is not None:
        parts.append(f"Max-Age={int(max_age)}")
    if expires is not None:
        parts.append(f"Expires={expires}")
    headers.append("; ".join(parts))


def stripe_request(method, endpoint, params=None):
    if not STRIPE_SECRET_KEY:
        raise RuntimeError("STRIPE_SECRET_KEY is not configured.")

    method = method.upper()
    url = "https://api.stripe.com/v1/" + endpoint.lstrip("/")
    encoded = urllib.parse.urlencode(params or {}).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=encoded if method != "GET" else None,
        method=method,
        headers={
            "Authorization": "Bearer " + STRIPE_SECRET_KEY,
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(body)
            message = data.get("error", {}).get("message", body)
        except Exception:
            message = body
        raise RuntimeError("Stripe error: " + str(message))


def plan_for_price(price_id):
    if price_id and STRIPE_ENGINEER_PRICE_ID and price_id == STRIPE_ENGINEER_PRICE_ID:
        return "Engineer"
    if price_id and STRIPE_PRO_PRICE_ID and price_id == STRIPE_PRO_PRICE_ID:
        return "Pro"
    return "Free"


def stripe_signature_valid(raw_body, signature_header):
    if not STRIPE_WEBHOOK_SECRET or not signature_header:
        return False

    timestamp = None
    signatures = []
    for part in signature_header.split(","):
        key, _, value = part.partition("=")
        if key == "t":
            try:
                timestamp = int(value)
            except ValueError:
                return False
        elif key == "v1":
            signatures.append(value)

    if timestamp is None or abs(int(time.time()) - timestamp) > 300:
        return False

    signed_payload = str(timestamp).encode("utf-8") + b"." + raw_body
    expected = hmac.new(
        STRIPE_WEBHOOK_SECRET.encode("utf-8"),
        signed_payload,
        hashlib.sha256,
    ).hexdigest()

    return any(hmac.compare_digest(expected, value) for value in signatures)


def stripe_price_from_subscription(subscription):
    try:
        return subscription["items"]["data"][0]["price"]["id"]
    except Exception:
        return ""


def handle_stripe_event(event):
    event_type = event.get("type", "")
    data = event.get("data", {}).get("object", {})

    if event_type == "checkout.session.completed":
        user_id = data.get("client_reference_id") or data.get("metadata", {}).get("user_id")
        customer_id = data.get("customer")
        subscription_id = data.get("subscription")
        price_id = data.get("metadata", {}).get("price_id", "")
        subscription_status = "active"

        # The subscription object is the source of truth for the actual price
        # and subscription status. Never infer a plan from the checkout amount.
        if subscription_id and STRIPE_SECRET_KEY:
            try:
                subscription = stripe_request("GET", f"subscriptions/{subscription_id}")
                price_id = stripe_price_from_subscription(subscription) or price_id
                subscription_status = subscription.get("status", "active")
            except Exception as error:
                print("[STRIPE] Could not retrieve completed subscription:", error)

        user = (
            get_user_by_id(user_id)
            if user_id
            else get_user_from_stripe_ids(customer_id, subscription_id)
        )

        if user:
            plan = plan_for_price(price_id)
            set_user_plan(
                user["id"],
                plan,
                customer_id,
                subscription_id,
                price_id,
                subscription_status,
            )
        return

    if event_type in (
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
    ):
        customer_id = data.get("customer")
        subscription_id = data.get("id")
        price_id = stripe_price_from_subscription(data)
        user = get_user_from_stripe_ids(customer_id, subscription_id)

        if not user:
            user_id = data.get("metadata", {}).get("user_id")
            user = get_user_by_id(user_id) if user_id else None

        if user:
            status = data.get("status", "")

            if event_type == "customer.subscription.deleted":
                plan = "Free"
            elif status in ("active", "trialing"):
                plan = plan_for_price(price_id)
            elif status in ("canceled", "unpaid", "incomplete_expired"):
                plan = "Free"
            else:
                # Keep the paid plan during transient states such as
                # incomplete/past_due. Stripe will send another subscription
                # event when the final state changes.
                plan = user["plan"] if user["plan"] in ("Pro", "Engineer") else "Free"

            set_user_plan(
                user["id"],
                plan,
                customer_id,
                subscription_id,
                price_id,
                status,
            )
        return

    if event_type == "invoice.payment_failed":
        customer_id = data.get("customer")
        subscription_id = data.get("subscription")
        user = get_user_from_stripe_ids(customer_id, subscription_id)
        if user:
            # Do not immediately revoke access on a single failed invoice.
            # The subscription webhook is responsible for the final status.
            set_user_stripe_data(
                user["id"],
                customer_id=customer_id,
                subscription_id=subscription_id,
                status="past_due",
            )
        return

    if event_type == "invoice.payment_succeeded":
        customer_id = data.get("customer")
        subscription_id = data.get("subscription")
        user = get_user_from_stripe_ids(customer_id, subscription_id)
        if user:
            price_id = user["stripe_price_id"] or ""
            status = "active"

            if subscription_id and STRIPE_SECRET_KEY:
                try:
                    subscription = stripe_request("GET", f"subscriptions/{subscription_id}")
                    price_id = stripe_price_from_subscription(subscription) or price_id
                    status = subscription.get("status", "active")
                except Exception as error:
                    print("[STRIPE] Could not retrieve paid subscription:", error)

            plan = plan_for_price(price_id)
            if status not in ("active", "trialing"):
                plan = user["plan"] if user["plan"] in ("Pro", "Engineer") else "Free"

            set_user_plan(
                user["id"],
                plan,
                customer_id,
                subscription_id,
                price_id,
                status,
            )
        return


def get_stripe_subscription(subscription_id):
    if not subscription_id:
        return None
    return stripe_request("GET", f"subscriptions/{subscription_id}")


def create_checkout_url(user, plan):
    if plan not in ("Pro", "Engineer"):
        raise ValueError("Invalid plan.")

    price_id = STRIPE_PRO_PRICE_ID if plan == "Pro" else STRIPE_ENGINEER_PRICE_ID

    if not price_id:
        raise RuntimeError(
            f"{plan} Stripe price is not configured. Set the {plan} Price ID in Render environment variables."
        )

    # A THETA account can have only one paid subscription. If the user is
    # already subscribed, change that subscription instead of creating a
    # second recurring charge.
    existing_subscription_id = user["stripe_subscription_id"]
    if existing_subscription_id:
        try:
            subscription = get_stripe_subscription(existing_subscription_id)
            status = subscription.get("status", "")
            if status in ("active", "trialing", "past_due", "incomplete"):
                current_price_id = stripe_price_from_subscription(subscription)

                if current_price_id == price_id:
                    raise RuntimeError(f"Your account is already subscribed to {plan}.")

                items = subscription.get("items", {}).get("data", [])
                if not items:
                    raise RuntimeError("Stripe subscription has no subscription item to update.")

                updated = stripe_request(
                    "POST",
                    f"subscriptions/{existing_subscription_id}",
                    {
                        "items[0][id]": items[0]["id"],
                        "items[0][price]": price_id,
                        "proration_behavior": "create_prorations",
                        "metadata[user_id]": str(user["id"]),
                        "metadata[plan]": plan,
                        "metadata[price_id]": price_id,
                    },
                )

                updated_price_id = stripe_price_from_subscription(updated) or price_id
                updated_status = updated.get("status", status)
                set_user_plan(
                    user["id"],
                    plan_for_price(updated_price_id),
                    user["stripe_customer_id"],
                    existing_subscription_id,
                    updated_price_id,
                    updated_status,
                )
                return os.environ.get("THETA_BASE_URL", "").strip().rstrip("/") or f"http://{HOST}:{PORT}"
        except RuntimeError:
            raise
        except Exception as error:
            print("[STRIPE] Existing subscription update failed:", error)

    customer_id = user["stripe_customer_id"]
    if not customer_id:
        customer = stripe_request(
            "POST",
            "customers",
            {
                "email": user["email"],
                "metadata[user_id]": str(user["id"]),
            },
        )
        customer_id = customer["id"]
        set_user_stripe_data(user["id"], customer_id=customer_id)

    base_url = os.environ.get("THETA_BASE_URL", "").strip().rstrip("/")
    if not base_url:
        base_url = f"http://{HOST}:{PORT}"

    session = stripe_request(
        "POST",
        "checkout/sessions",
        {
            "mode": "subscription",
            "customer": customer_id,
            "client_reference_id": str(user["id"]),
            "customer_update[address]": "auto",
            "success_url": base_url + "/?checkout=success",
            "cancel_url": base_url + "/?checkout=canceled",
            "line_items[0][price]": price_id,
            "line_items[0][quantity]": "1",
            "metadata[user_id]": str(user["id"]),
            "metadata[price_id]": price_id,
            "metadata[plan]": plan,
            "subscription_data[metadata][user_id]": str(user["id"]),
            "subscription_data[metadata][plan]": plan,
            "subscription_data[metadata][price_id]": price_id,
        },
    )

    if not session.get("url"):
        raise RuntimeError("Stripe did not return a Checkout URL.")

    return session["url"]


init_database()


# ============================================================
# SAFE MATH ENGINE
# ============================================================

SAFE_FUNCTIONS = {
    "sqrt": math.sqrt,
    "abs": abs,
    "min": min,
    "max": max,
    "log": math.log,
    "exp": math.exp,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
}

SAFE_CONSTANTS = {
    "pi": math.pi,
    "e": math.e,
}


class SafeExpression:
    def __init__(self, expression):
        self.expression = str(expression).strip()
        self.tree = ast.parse(self.expression, mode="eval")

    def evaluate(self, variables):
        return self._eval(self.tree.body, variables)

    def _eval(self, node, variables):
        if isinstance(node, ast.Constant):
            if isinstance(node.value, (int, float, bool)):
                return node.value
            raise ValueError("Unsupported constant")

        if isinstance(node, ast.Name):
            if node.id in variables:
                return variables[node.id]
            if node.id in SAFE_CONSTANTS:
                return SAFE_CONSTANTS[node.id]
            raise ValueError("Unknown variable: " + node.id)

        if isinstance(node, ast.BinOp):
            left = self._eval(node.left, variables)
            right = self._eval(node.right, variables)

            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if isinstance(node.op, ast.Div):
                return left / right
            if isinstance(node.op, ast.Pow):
                if abs(float(right)) > 20:
                    raise ValueError("Exponent too large")
                if abs(float(left)) > 1e6 and right > 2:
                    raise ValueError("Power result too large")
                return left ** right
            if isinstance(node.op, ast.Mod):
                return left % right

            raise ValueError("Unsupported operator")

        if isinstance(node, ast.UnaryOp):
            value = self._eval(node.operand, variables)

            if isinstance(node.op, ast.USub):
                return -value
            if isinstance(node.op, ast.UAdd):
                return value

            raise ValueError("Unsupported unary operator")

        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name):
                raise ValueError("Unsupported function")

            name = node.func.id

            if name not in SAFE_FUNCTIONS:
                raise ValueError("Function not allowed: " + name)

            args = [self._eval(arg, variables) for arg in node.args]
            return SAFE_FUNCTIONS[name](*args)

        if isinstance(node, ast.Compare):
            left = self._eval(node.left, variables)

            for operator, comparator in zip(node.ops, node.comparators):
                right = self._eval(comparator, variables)

                if isinstance(operator, ast.Lt):
                    result = left < right
                elif isinstance(operator, ast.LtE):
                    result = left <= right
                elif isinstance(operator, ast.Gt):
                    result = left > right
                elif isinstance(operator, ast.GtE):
                    result = left >= right
                elif isinstance(operator, ast.Eq):
                    result = left == right
                elif isinstance(operator, ast.NotEq):
                    result = left != right
                else:
                    raise ValueError("Unsupported comparison")

                if not result:
                    return False

                left = right

            return True

        if isinstance(node, ast.BoolOp):
            values = [self._eval(value, variables) for value in node.values]

            if isinstance(node.op, ast.And):
                return all(values)

            if isinstance(node.op, ast.Or):
                return any(values)

        raise ValueError(
            "Unsupported expression element: " + type(node).__name__
        )


def evaluate_expression(expression, variables):
    try:
        value = SafeExpression(expression).evaluate(variables)

        if isinstance(value, bool):
            return value

        if not math.isfinite(float(value)):
            return None

        return float(value)

    except Exception:
        return None


# ============================================================
# MODEL HELPERS
# ============================================================

def empty_model():
    return {
        "name": "THETA Engineering Project",
        "description": "",
        "variables": [],
        "equations": [],
        "constraints": [],
        "objectives": [],
    }


def clean_name(name, fallback="x"):
    name = str(name).strip()
    name = re.sub(r"[^a-zA-Z0-9_]", "_", name)

    if not name:
        name = fallback

    if name[0].isdigit():
        name = "_" + name

    return name


def normalize_model(model):
    base = empty_model()

    if not isinstance(model, dict):
        return base

    base["name"] = str(model.get("name", base["name"]))
    base["description"] = str(model.get("description", ""))

    for item in model.get("variables", []):
        if not isinstance(item, dict):
            continue

        name = clean_name(item.get("name", "x"), "x")

        try:
            minimum = float(item.get("min", 0.1))
        except Exception:
            minimum = 0.1

        try:
            maximum = float(item.get("max", 10.0))
        except Exception:
            maximum = 10.0

        if maximum <= minimum:
            maximum = minimum + 1.0

        base["variables"].append(
            {
                "name": name,
                "min": minimum,
                "max": maximum,
                "unit": str(item.get("unit", "")),
            }
        )

    for item in model.get("equations", []):
        if not isinstance(item, dict):
            continue

        base["equations"].append(
            {
                "name": clean_name(item.get("name", "eq"), "eq"),
                "expression": str(item.get("expression", "0")),
                "unit": str(item.get("unit", "")),
            }
        )

    for item in model.get("constraints", []):
        if isinstance(item, str):
            base["constraints"].append({
                "expression": item,
                "sense": "lte",
                "limit": 0.0,
                "name": "Constraint",
            })
            continue

        if not isinstance(item, dict):
            continue

        sense = str(item.get("sense", "lte")).lower()

        if sense not in ("lte", "gte", "eq"):
            sense = "lte"

        try:
            limit = float(item.get("limit", 0))
        except Exception:
            limit = 0.0

        base["constraints"].append(
            {
                "expression": str(item.get("expression", "0")),
                "sense": sense,
                "limit": limit,
                "name": str(item.get("name", "Constraint")),
            }
        )

    for item in model.get("objectives", []):
        if not isinstance(item, dict):
            continue

        direction = str(item.get("direction", "min")).lower()

        if direction not in ("min", "max"):
            direction = "min"

        try:
            weight = float(item.get("weight", 1))
        except Exception:
            weight = 1.0

        base["objectives"].append(
            {
                "expression": str(item.get("expression", "0")),
                "direction": direction,
                "name": str(item.get("name", "Objective")),
                "weight": weight,
            }
        )

    return base


# ============================================================
# NATURAL LANGUAGE ENGINE
# ============================================================

def extract_number(text, patterns, default):
    for pattern in patterns:
        match = re.search(pattern, text, re.I)

        if match:
            try:
                return float(match.group(1))
            except Exception:
                pass

    return default


def interpret_engineering_request(text):
    text = str(text).strip()
    lower = text.lower()

    if not text:
        return empty_model()

    # --------------------------------------------------------
    # BEAM
    # --------------------------------------------------------

    if "beam" in lower or "cantilever" in lower:
        load = extract_number(
            lower,
            [
                r"(\d+(?:\.\d+)?)\s*(?:n|newtons?)",
                r"load\s*(?:of|=)?\s*(\d+(?:\.\d+)?)",
                r"force\s*(?:of|=)?\s*(\d+(?:\.\d+)?)",
            ],
            500.0,
        )

        length = extract_number(
            lower,
            [
                r"(\d+(?:\.\d+)?)\s*m(?:eter|eters)?\s*(?:long)?",
                r"length\s*(?:of|=)?\s*(\d+(?:\.\d+)?)",
            ],
            1.0,
        )

        stress_limit = extract_number(
            lower,
            [
                r"stress\s*(?:limit|maximum|max)?\s*(?:of|=)?\s*(\d+(?:\.\d+)?)",
                r"(\d+(?:\.\d+)?)\s*(?:mpa|megapascals?)",
            ],
            250.0,
        )

        return {
            "name": "Lightweight Beam",
            "description": text,
            "variables": [
                {
                    "name": "b",
                    "min": 0.01,
                    "max": 0.20,
                    "unit": "m",
                },
                {
                    "name": "h",
                    "min": 0.01,
                    "max": 0.30,
                    "unit": "m",
                },
            ],
            "equations": [
                {
                    "name": "stress",
                    "expression": f"6*{load}*{length}/(b*h*h)",
                    "unit": "Pa",
                },
                {
                    "name": "mass",
                    "expression": f"b*h*7850*{length}",
                    "unit": "kg",
                },
            ],
            "constraints": [
                {
                    "name": "Stress Limit",
                    "expression": "stress",
                    "sense": "lte",
                    "limit": stress_limit * 1000000,
                }
            ],
            "objectives": [
                {
                    "name": "Minimize Mass",
                    "expression": "mass",
                    "direction": "min",
                    "weight": 1,
                }
            ],
        }

    # --------------------------------------------------------
    # SPRING
    # --------------------------------------------------------

    if "spring" in lower:
        force = extract_number(
            lower,
            [
                r"(\d+(?:\.\d+)?)\s*(?:n|newtons?)",
                r"force\s*(?:of|=)?\s*(\d+(?:\.\d+)?)",
            ],
            100.0,
        )

        return {
            "name": "Spring Design",
            "description": text,
            "variables": [
                {
                    "name": "d",
                    "min": 0.001,
                    "max": 0.02,
                    "unit": "m",
                },
                {
                    "name": "D",
                    "min": 0.01,
                    "max": 0.10,
                    "unit": "m",
                },
                {
                    "name": "n",
                    "min": 2,
                    "max": 20,
                    "unit": "turns",
                },
            ],
            "equations": [
                {
                    "name": "stiffness",
                    "expression": "79000000000*d**4/(8*D**3*n)",
                    "unit": "N/m",
                },
                {
                    "name": "mass",
                    "expression": "n*pi*pi*D*d*d*7850/4",
                    "unit": "kg",
                },
                {
                    "name": "deflection",
                    "expression": f"{force}/stiffness",
                    "unit": "m",
                },
            ],
            "constraints": [
                {
                    "name": "Minimum Stiffness",
                    "expression": "stiffness",
                    "sense": "gte",
                    "limit": force / 0.05,
                }
            ],
            "objectives": [
                {
                    "name": "Minimize Mass",
                    "expression": "mass",
                    "direction": "min",
                    "weight": 1,
                }
            ],
        }

    # --------------------------------------------------------
    # DRONE
    # --------------------------------------------------------

    if (
        "drone" in lower
        or "quadcopter" in lower
        or "quad" in lower
        or "uav" in lower
    ):
        return {
            "name": "Drone Optimization",
            "description": text,
            "variables": [
                {
                    "name": "arm",
                    "min": 0.10,
                    "max": 0.40,
                    "unit": "m",
                },
                {
                    "name": "motor_mass",
                    "min": 0.03,
                    "max": 0.20,
                    "unit": "kg",
                },
                {
                    "name": "battery_mass",
                    "min": 0.10,
                    "max": 1.00,
                    "unit": "kg",
                },
            ],
            "equations": [
                {
                    "name": "frame_mass",
                    "expression": "4*arm*0.15",
                    "unit": "kg",
                },
                {
                    "name": "total_mass",
                    "expression": "frame_mass+4*motor_mass+battery_mass",
                    "unit": "kg",
                },
                {
                    "name": "payload_margin",
                    "expression": "4*2.5-total_mass*9.81",
                    "unit": "N",
                },
            ],
            "constraints": [
                {
                    "name": "Positive Payload Margin",
                    "expression": "payload_margin",
                    "sense": "gte",
                    "limit": 0,
                }
            ],
            "objectives": [
                {
                    "name": "Minimize Total Mass",
                    "expression": "total_mass",
                    "direction": "min",
                    "weight": 1,
                }
            ],
        }

    # --------------------------------------------------------
    # BRACKET
    # --------------------------------------------------------

    if (
        "bracket" in lower
        or "mount" in lower
        or "plate" in lower
    ):
        return {
            "name": "Lightweight Bracket",
            "description": text,
            "variables": [
                {
                    "name": "width",
                    "min": 0.02,
                    "max": 0.20,
                    "unit": "m",
                },
                {
                    "name": "height",
                    "min": 0.02,
                    "max": 0.20,
                    "unit": "m",
                },
                {
                    "name": "thickness",
                    "min": 0.002,
                    "max": 0.030,
                    "unit": "m",
                },
            ],
            "equations": [
                {
                    "name": "volume",
                    "expression": "width*height*thickness",
                    "unit": "m^3",
                },
                {
                    "name": "mass",
                    "expression": "volume*2700",
                    "unit": "kg",
                },
                {
                    "name": "section",
                    "expression": "width*thickness**3/12",
                    "unit": "m^4",
                },
            ],
            "constraints": [
                {
                    "name": "Minimum Thickness",
                    "expression": "thickness",
                    "sense": "gte",
                    "limit": 0.005,
                }
            ],
            "objectives": [
                {
                    "name": "Minimize Mass",
                    "expression": "mass",
                    "direction": "min",
                    "weight": 1,
                }
            ],
        }

    # --------------------------------------------------------
    # GENERIC FALLBACK
    # --------------------------------------------------------

    return {
        "name": "THETA Concept Design",
        "description": text,
        "variables": [
            {
                "name": "x",
                "min": 0.1,
                "max": 10.0,
                "unit": "",
            },
            {
                "name": "y",
                "min": 0.1,
                "max": 10.0,
                "unit": "",
            },
        ],
        "equations": [
            {
                "name": "performance",
                "expression": "x+y",
                "unit": "",
            },
            {
                "name": "cost",
                "expression": "x*x+y*y",
                "unit": "",
            },
        ],
        "constraints": [],
        "objectives": [
            {
                "name": "Maximize Performance",
                "expression": "performance",
                "direction": "max",
                "weight": 1,
            }
        ],
    }


# ============================================================
# MODEL EVALUATION
# ============================================================

def calculate_model(model, design):
    values = dict(design)

    for equation in model["equations"]:
        name = clean_name(equation["name"], "eq")
        expression = equation["expression"]

        result = evaluate_expression(expression, values)

        if result is None:
            return None

        values[name] = result

    return values


def constraint_violation(constraint, values):
    expression = constraint["expression"]
    limit = constraint["limit"]
    sense = constraint["sense"]

    result = evaluate_expression(expression, values)

    if result is None:
        return float("inf")

    if sense == "lte":
        return max(0.0, result - limit)

    if sense == "gte":
        return max(0.0, limit - result)

    if sense == "eq":
        return abs(result - limit)

    return float("inf")


def objective_value(objective, values):
    result = evaluate_expression(
        objective["expression"],
        values,
    )

    if result is None:
        return None

    return float(result)


def evaluate_design(model, design):
    values = calculate_model(model, design)

    if values is None:
        return None

    violations = []

    for constraint in model["constraints"]:
        violations.append(
            constraint_violation(
                constraint,
                values,
            )
        )

    total_violation = sum(violations)

    objectives = []

    for objective in model["objectives"]:
        value = objective_value(
            objective,
            values,
        )

        if value is None:
            return None

        objectives.append(
            {
                "name": objective["name"],
                "value": value,
                "direction": objective["direction"],
            }
        )

    return {
        "design": dict(design),
        "values": values,
        "violations": violations,
        "total_violation": total_violation,
        "objectives": objectives,
    }


# ============================================================
# OPTIMIZER
# ============================================================

def score_result(result, model):
    if result is None:
        return float("-inf")

    penalty = result["total_violation"]

    if not math.isfinite(penalty):
        return float("-inf")

    score = -penalty * 1000000.0

    for objective in result["objectives"]:
        value = objective["value"]
        weight = 1.0

        for model_objective in model["objectives"]:
            if model_objective["name"] == objective["name"]:
                weight = model_objective.get("weight", 1.0)
                break

        if objective["direction"] == "min":
            score -= value * weight
        else:
            score += value * weight

    return score


def random_design(model):
    design = {}

    for variable in model["variables"]:
        minimum = variable["min"]
        maximum = variable["max"]

        design[variable["name"]] = random.uniform(
            minimum,
            maximum,
        )

    return design


def mutate_design(model, design):
    new_design = dict(design)

    for variable in model["variables"]:
        name = variable["name"]
        minimum = variable["min"]
        maximum = variable["max"]

        if random.random() < 0.75:
            span = maximum - minimum
            change = random.gauss(
                0,
                span * 0.12,
            )

            new_value = new_design[name] + change

            new_value = max(
                minimum,
                min(maximum, new_value),
            )

            new_design[name] = new_value

    return new_design


def optimize_model(
    model,
    population_size=700,
    generations=80,
):
    model = normalize_model(model)

    if not model["variables"]:
        return {
            "success": False,
            "error": "Add at least one variable.",
        }

    if not model["objectives"]:
        return {
            "success": False,
            "error": "Add at least one objective.",
        }

    population = []

    for _ in range(population_size):
        design = random_design(model)

        result = evaluate_design(
            model,
            design,
        )

        if result is not None:
            population.append(result)

    if not population:
        return {
            "success": False,
            "error": "THETA could not evaluate the model.",
        }

    for _ in range(generations):

        population.sort(
            key=lambda item: score_result(
                item,
                model,
            ),
            reverse=True,
        )

        elite_count = max(
            10,
            int(len(population) * 0.12),
        )

        elites = population[:elite_count]

        next_population = list(elites)

        while len(next_population) < population_size:

            parent = random.choice(elites)

            child_design = mutate_design(
                model,
                parent["design"],
            )

            child_result = evaluate_design(
                model,
                child_design,
            )

            if child_result is not None:
                next_population.append(
                    child_result
                )

        population = next_population

    population.sort(
        key=lambda item: score_result(
            item,
            model,
        ),
        reverse=True,
    )

    feasible = [
        item
        for item in population
        if item["total_violation"] <= 1e-12
    ]

    if feasible:
        ranked = feasible
    else:
        ranked = population

    best = ranked[0]

    return {
        "success": True,
        "best": best,
        "top": ranked[:20],
        "evaluated": population_size * generations,
        "feasible_count": len(feasible),
    }


# ============================================================
# EXAMPLES
# ============================================================

def beam_example():
    return interpret_engineering_request(
        "Design a lightweight cantilever beam that holds 500 N over 1 meter with a maximum stress of 250 MPa."
    )


def spring_example():
    return interpret_engineering_request(
        "Design a lightweight spring for 100 N of force."
    )


def drone_example():
    return interpret_engineering_request(
        "Design a lightweight drone with good payload performance."
    )


def bracket_example():
    return interpret_engineering_request(
        "Design a lightweight mounting bracket."
    )


# ============================================================
# HTML APPLICATION
# ============================================================

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>

<meta charset="UTF-8">

<meta name="google-site-verification" content="MMIdUHh9590WwUT1WeDykMUXzQPk8wpeor6DDPGCAp4" />

<meta
    name="viewport"
    content="width=device-width, initial-scale=1.0"
>

<title>THETA Technology Discovery Engine</title>

<style>

* {
    box-sizing: border-box;
}

body {
    margin: 0;
    background: #07090d;
    color: #f4f7fb;
    font-family: Arial, Helvetica, sans-serif;
}

button,
input,
textarea,
select {
    font: inherit;
}

button {
    cursor: pointer;
}

.topbar {
    height: 64px;
    border-bottom: 1px solid #252a33;
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0 28px;
    background: #090b10;
}

.logo {
    font-size: 25px;
    font-weight: 800;
    letter-spacing: 5px;
}

.status {
    color: #70ff9b;
    font-size: 13px;
}

.container {
    width: min(1200px, calc(100% - 32px));
    margin: 0 auto;
}

.hero {
    padding: 60px 0 30px;
}

.hero h1 {
    font-size: clamp(38px, 6vw, 76px);
    line-height: 0.95;
    margin: 0 0 20px;
    max-width: 850px;
}

.hero p {
    color: #aab3c2;
    font-size: 18px;
    line-height: 1.6;
    max-width: 760px;
}

.modebar {
    display: flex;
    gap: 8px;
    margin: 20px 0;
}

.modebutton {
    border: 1px solid #303744;
    background: #11151d;
    color: #b7c0ce;
    border-radius: 10px;
    padding: 10px 18px;
}

.modebutton.active {
    background: #f4f7fb;
    color: #080a0e;
}

.panel {
    border: 1px solid #272d38;
    border-radius: 16px;
    background: #0c1017;
    padding: 24px;
    margin-bottom: 22px;
}

.panel h2 {
    margin-top: 0;
}

.hidden {
    display: none !important;
}

.chat {
    min-height: 420px;
    display: flex;
    flex-direction: column;
}

.messages {
    flex: 1;
    min-height: 300px;
    max-height: 520px;
    overflow-y: auto;
    padding: 8px;
}

.message {
    max-width: 80%;
    padding: 15px 17px;
    margin: 10px 0;
    border-radius: 14px;
    line-height: 1.5;
}

.message.theta {
    background: #151b24;
    border: 1px solid #2c3542;
}

.message.user {
    background: #f4f7fb;
    color: #080a0e;
    margin-left: auto;
}

.chatrow {
    display: flex;
    gap: 10px;
    margin-top: 15px;
}

.chatrow input {
    flex: 1;
}

input,
textarea,
select {
    width: 100%;
    background: #080b10;
    color: #f4f7fb;
    border: 1px solid #303744;
    border-radius: 9px;
    padding: 11px 12px;
    outline: none;
}

textarea {
    min-height: 90px;
    resize: vertical;
}

input:focus,
textarea:focus,
select:focus {
    border-color: #758198;
}

.primary {
    background: #f4f7fb;
    color: #080a0e;
    border: 0;
    border-radius: 9px;
    padding: 11px 18px;
    font-weight: 700;
}

.secondary {
    background: #171c25;
    color: #f4f7fb;
    border: 1px solid #303744;
    border-radius: 9px;
    padding: 10px 15px;
}

.danger {
    background: #241418;
    color: #ff9a9a;
    border: 1px solid #5a2930;
    border-radius: 8px;
    padding: 7px 11px;
}

.section-title {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 12px;
    margin-bottom: 12px;
}

.section-title h3 {
    margin: 0;
}

.section-buttons {
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
}

.builder-row {
    border: 1px solid #252c36;
    border-radius: 11px;
    padding: 12px;
    margin-bottom: 10px;
    background: #0a0e14;
}

.grid {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 9px;
    align-items: end;
}

.field label {
    display: block;
    color: #7f8999;
    font-size: 11px;
    margin-bottom: 5px;
    text-transform: uppercase;
    letter-spacing: 0.6px;
}

.review {
    background: #090d13;
    border: 1px solid #252d39;
    border-radius: 12px;
    padding: 18px;
}

.review h3 {
    margin-top: 0;
}

.review-item {
    padding: 8px 0;
    border-bottom: 1px solid #202631;
}

.review-item:last-child {
    border-bottom: 0;
}

.example-buttons {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin-top: 12px;
}

.results-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 16px;
}

.result-card {
    border: 1px solid #282f3a;
    border-radius: 12px;
    padding: 17px;
    background: #090d13;
}

.result-card h3 {
    margin-top: 0;
}

.result-line {
    display: flex;
    justify-content: space-between;
    gap: 20px;
    padding: 8px 0;
    border-bottom: 1px solid #202631;
}

.result-line:last-child {
    border-bottom: 0;
}

.muted {
    color: #8993a3;
}

footer {
    color: #677181;
    padding: 50px 0;
    text-align: center;
}

@media (max-width: 800px) {

    .grid,
    .results-grid {
        grid-template-columns: 1fr;
    }

    .topbar {
        padding: 0 16px;
    }

    .container {
        width: min(100% - 20px, 1200px);
    }

    .message {
        max-width: 94%;
    }
}

</style>

</head>

<body>

<header class="topbar">

    <div class="logo">THETA</div>

    <div class="status">
        ● ENGINE ONLINE
    </div>

</header>


<main class="container">

<section class="hero">

    <h1>
        Design something better.
    </h1>

    <p>
        Describe what you want to build. THETA converts the idea into an
        engineering model, searches thousands of possible designs, and
        returns the strongest candidates.
    </p>

    <div class="modebar">

        <button
            id="beginnerMode"
            class="modebutton active"
            onclick="showMode('beginner')"
        >
            Beginner
        </button>

        <button
            id="advancedMode"
            class="modebutton"
            onclick="showMode('advanced')"
        >
            Advanced
        </button>

    </div>

</section>


<section id="beginnerPanel" class="panel">

    <h2>
        Tell THETA what you want to build
    </h2>

    <div class="chat">

        <div id="messages" class="messages">

            <div class="message theta">

                Tell me what you want to design.

                <br>
                <br>

                <strong>
                    "Design a lightweight beam that can hold 500 N."
                </strong>

            </div>

        </div>

        <div class="chatrow">

            <input
                id="chatInput"
                placeholder="Describe your engineering problem..."
                onkeydown="if(event.key === 'Enter') sendChat()"
            >

            <button
                class="primary"
                onclick="sendChat()"
            >
                Send
            </button>

            <button
                class="secondary"
                onclick="resetChat()"
            >
                Reset Chat
            </button>

        </div>

    </div>


    <div class="example-buttons">

        <button
            class="secondary"
            onclick="useExample('beam')"
        >
            Beam
        </button>

        <button
            class="secondary"
            onclick="useExample('spring')"
        >
            Spring
        </button>

        <button
            class="secondary"
            onclick="useExample('drone')"
        >
            Drone
        </button>

        <button
            class="secondary"
            onclick="useExample('bracket')"
        >
            Bracket
        </button>

    </div>

</section>


<section id="reviewPanel" class="panel hidden">

    <div class="section-title">

        <h2>
            Review Engineering Model
        </h2>

        <button
            class="secondary"
            onclick="openAdvancedEditor()"
        >
            Edit Model
        </button>

    </div>

    <div
        id="review"
        class="review"
    ></div>

    <br>

    <button
        class="primary"
        onclick="optimizeCurrent()"
    >
        RUN THETA
    </button>

</section>


<section id="resultsPanel" class="panel hidden">

    <h2>
        THETA Results
    </h2>

    <div id="results"></div>

</section>


<section id="advancedPanel" class="panel hidden">

    <h2>
        Advanced Engineering Builder
    </h2>

    <div class="field">

        <label>
            Project Name
        </label>

        <input
            id="projectName"
            value="THETA Engineering Project"
        >

    </div>

    <br>

    <div class="field">

        <label>
            Description
        </label>

        <textarea id="projectDescription"></textarea>

    </div>

    <br>


    <div class="section-title">

        <h3>
            Variables
        </h3>

        <div class="section-buttons">

            <button
                class="secondary"
                onclick="addVariable()"
            >
                + Variable
            </button>

            <button
                class="secondary"
                onclick="removeVariable()"
            >
                − Variable
            </button>

        </div>

    </div>

    <div id="variables"></div>


    <div class="section-title">

        <h3>
            Equations
        </h3>

        <div class="section-buttons">

            <button
                class="secondary"
                onclick="addEquation()"
            >
                + Equation
            </button>

            <button
                class="secondary"
                onclick="removeEquation()"
            >
                − Equation
            </button>

        </div>

    </div>

    <div id="equations"></div>


    <div class="section-title">

        <h3>
            Constraints
        </h3>

        <div class="section-buttons">

            <button
                class="secondary"
                onclick="addConstraint()"
            >
                + Constraint
            </button>

            <button
                class="secondary"
                onclick="removeConstraint()"
            >
                − Constraint
            </button>

        </div>

    </div>

    <div id="constraints"></div>


    <div class="section-title">

        <h3>
            Objectives
        </h3>

        <div class="section-buttons">

            <button
                class="secondary"
                onclick="addObjective()"
            >
                + Objective
            </button>

            <button
                class="secondary"
                onclick="removeObjective()"
            >
                − Objective
            </button>

        </div>

    </div>

    <div id="objectives"></div>


    <br>

    <div style="display:flex; gap:8px; flex-wrap:wrap;">

        <button
            class="primary"
            onclick="runAdvanced()"
        >
            RUN THETA
        </button>

        <button
            class="secondary"
            onclick="loadExample('beam')"
        >
            Load Beam
        </button>

        <button
            class="secondary"
            onclick="loadExample('spring')"
        >
            Load Spring
        </button>

        <button
            class="secondary"
            onclick="loadExample('drone')"
        >
            Load Drone
        </button>

        <button
            class="secondary"
            onclick="loadExample('bracket')"
        >
            Load Bracket
        </button>

        <button
            class="secondary"
            onclick="clearBuilder()"
        >
            Clear
        </button>

    </div>

</section>

</main>


<footer>
    THETA Technology Discovery Engine
</footer>


<script>

let currentModel = null;


function escapeHtml(value) {

    return String(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");

}


function showMode(mode) {

    const beginner =
        document.getElementById("beginnerPanel");

    const advanced =
        document.getElementById("advancedPanel");

    const beginnerButton =
        document.getElementById("beginnerMode");

    const advancedButton =
        document.getElementById("advancedMode");


    if (mode === "beginner") {

        beginner.classList.remove("hidden");
        advanced.classList.add("hidden");

        beginnerButton.classList.add("active");
        advancedButton.classList.remove("active");

    } else {

        beginner.classList.add("hidden");
        advanced.classList.remove("hidden");

        beginnerButton.classList.remove("active");
        advancedButton.classList.add("active");

    }

}


function addMessage(text, type) {

    const messages =
        document.getElementById("messages");

    const div =
        document.createElement("div");

    div.className =
        "message " + type;

    div.innerHTML =
        escapeHtml(text);

    messages.appendChild(div);

    messages.scrollTop =
        messages.scrollHeight;

}


function resetChat() {

    const messages =
        document.getElementById("messages");

    const input =
        document.getElementById("chatInput");

    messages.innerHTML = `
        <div class="message theta">

            Tell me what you want to design.

            <br>
            <br>

            <strong>
                "Design a lightweight beam that can hold 500 N."
            </strong>

        </div>
    `;

    input.value = "";

    currentModel = null;

    document.getElementById(
        "reviewPanel"
    ).classList.add(
        "hidden"
    );

    document.getElementById(
        "resultsPanel"
    ).classList.add(
        "hidden"
    );

    input.focus();

}


async function sendChat() {

    const input =
        document.getElementById("chatInput");

    const text =
        input.value.trim();

    if (!text) {
        return;
    }

    addMessage(
        text,
        "user"
    );

    input.value = "";

    addMessage(
        "Building engineering model...",
        "theta"
    );


    try {

        const response =
            await fetch(
                "/interpret",
                {
                    method: "POST",
                    headers: {
                        "Content-Type":
                            "application/json"
                    },
                    body: JSON.stringify({
                        text: text
                    })
                }
            );

        const data =
            await response.json();

        const messages =
            document.getElementById("messages");

        if (messages.lastElementChild) {
            messages.removeChild(
                messages.lastElementChild
            );
        }

        currentModel =
            data.model;

        addMessage(
            "I created an engineering model for this problem. Review it below, then run THETA.",
            "theta"
        );

        renderReview();

    } catch (error) {

        addMessage(
            "THETA could not reach the local engine.",
            "theta"
        );

    }

}


async function useExample(name) {

    let text = "";

    if (name === "beam") {
        text =
            "Design a lightweight beam that can hold 500 N.";
    }

    if (name === "spring") {
        text =
            "Design a lightweight spring for 100 N.";
    }

    if (name === "drone") {
        text =
            "Design a lightweight drone.";
    }

    if (name === "bracket") {
        text =
            "Design a lightweight mounting bracket.";
    }

    document.getElementById("chatInput").value =
        text;

    await sendChat();

}


function renderReview() {

    if (!currentModel) {
        return;
    }

    const panel =
        document.getElementById("reviewPanel");

    const review =
        document.getElementById("review");

    panel.classList.remove("hidden");

    let html = "";

    html +=
        "<h3>" +
        escapeHtml(currentModel.name) +
        "</h3>";


    if (currentModel.description) {

        html +=
            "<p class='muted'>" +
            escapeHtml(currentModel.description) +
            "</p>";

    }


    html +=
        "<div class='review-item'>" +
        "<strong>Variables</strong>" +
        "</div>";


    for (
        const variable
        of currentModel.variables
    ) {

        html +=
            "<div class='review-item'>" +
            escapeHtml(variable.name) +
            " = [" +
            escapeHtml(variable.min) +
            ", " +
            escapeHtml(variable.max) +
            "] " +
            escapeHtml(variable.unit || "") +
            "</div>";

    }


    html +=
        "<div class='review-item'>" +
        "<strong>Equations</strong>" +
        "</div>";


    for (
        const equation
        of currentModel.equations
    ) {

        html +=
            "<div class='review-item'>" +
            escapeHtml(equation.name) +
            " = " +
            escapeHtml(equation.expression) +
            "</div>";

    }


    html +=
        "<div class='review-item'>" +
        "<strong>Constraints</strong>" +
        "</div>";


    for (
        const constraint
        of currentModel.constraints
    ) {

        html +=
            "<div class='review-item'>" +
            escapeHtml(constraint.name) +
            ": " +
            escapeHtml(constraint.expression) +
            " " +
            escapeHtml(constraint.sense) +
            " " +
            escapeHtml(constraint.limit) +
            "</div>";

    }


    html +=
        "<div class='review-item'>" +
        "<strong>Objectives</strong>" +
        "</div>";


    for (
        const objective
        of currentModel.objectives
    ) {

        html +=
            "<div class='review-item'>" +
            escapeHtml(objective.name) +
            ": " +
            escapeHtml(objective.direction) +
            " " +
            escapeHtml(objective.expression) +
            "</div>";

    }


    review.innerHTML =
        html;

}


async function optimizeCurrent() {

    if (!currentModel) {
        return;
    }

    await runOptimization(
        currentModel
    );

}


async function runOptimization(model) {

    const resultsPanel =
        document.getElementById("resultsPanel");

    const results =
        document.getElementById("results");

    resultsPanel.classList.remove("hidden");

    results.innerHTML =
        "<p class='muted'>THETA is searching the design space...</p>";


    try {

        const response =
            await fetch(
                "/optimize",
                {
                    method: "POST",
                    headers: {
                        "Content-Type":
                            "application/json"
                    },
                    body: JSON.stringify(model)
                }
            );

        const data =
            await response.json();

        if (!data.success) {

            results.innerHTML =
                "<p>" +
                escapeHtml(
                    data.error ||
                    "Optimization failed."
                ) +
                "</p>";

            return;
        }

        renderResults(data);

    } catch (error) {

        results.innerHTML =
            "<p>Optimization request failed.</p>";

    }

}


function renderResults(data) {

    const results =
        document.getElementById("results");

    const best =
        data.best;

    let html = "";

    html +=
        "<div class='results-grid'>";


    html +=
        "<div class='result-card'>" +
        "<h3>Best Design</h3>";


    for (
        const [name, value]
        of Object.entries(best.design)
    ) {

        html +=
            "<div class='result-line'>" +
            "<span>" +
            escapeHtml(name) +
            "</span>" +
            "<strong>" +
            Number(value).toPrecision(7) +
            "</strong>" +
            "</div>";

    }


    html +=
        "</div>";


    html +=
        "<div class='result-card'>" +
        "<h3>Performance</h3>";


    for (
        const objective
        of best.objectives
    ) {

        html +=
            "<div class='result-line'>" +
            "<span>" +
            escapeHtml(objective.name) +
            "</span>" +
            "<strong>" +
            Number(objective.value).toPrecision(7) +
            "</strong>" +
            "</div>";

    }


    html +=
        "<div class='result-line'>" +
        "<span>Constraint violation</span>" +
        "<strong>" +
        Number(
            best.total_violation
        ).toPrecision(7) +
        "</strong>" +
        "</div>";


    html +=
        "</div>" +
        "</div>";


    html +=
        "<br>" +
        "<div class='result-card'>" +
        "<h3>Search Statistics</h3>" +


        "<div class='result-line'>" +
        "<span>Evaluations</span>" +
        "<strong>" +
        escapeHtml(data.evaluated) +
        "</strong>" +
        "</div>" +


        "<div class='result-line'>" +
        "<span>Feasible designs</span>" +
        "<strong>" +
        escapeHtml(data.feasible_count) +
        "</strong>" +
        "</div>" +


        "</div>";


    html +=
        "<br>" +
        "<div class='result-card'>" +
        "<h3>Calculated Values</h3>";


    for (
        const [name, value]
        of Object.entries(best.values)
    ) {

        if (best.design[name] !== undefined) {
            continue;
        }

        html +=
            "<div class='result-line'>" +
            "<span>" +
            escapeHtml(name) +
            "</span>" +
            "<strong>" +
            Number(value).toPrecision(7) +
            "</strong>" +
            "</div>";

    }


    html +=
        "</div>";


    html +=
        "<br>" +
        "<div class='result-card'>" +
        "<h3>Top Candidates</h3>";


    data.top
        .slice(0, 10)
        .forEach(
            function(item, index) {

                html +=
                    "<div class='result-line'>" +
                    "<span>" +
                    "Design " +
                    (index + 1) +
                    "</span>" +
                    "<strong>";

                const parts = [];

                for (
                    const [name, value]
                    of Object.entries(item.design)
                ) {

                    parts.push(
                        escapeHtml(name) +
                        "=" +
                        Number(value).toPrecision(4)
                    );

                }

                html +=
                    parts.join(" | ");

                html +=
                    "</strong>" +
                    "</div>";

            }
        );


    html +=
        "</div>";


    results.innerHTML =
        html;

}


function addVariable() {

    const container =
        document.getElementById("variables");

    const row =
        document.createElement("div");

    row.className =
        "builder-row variable-row";

    row.innerHTML =
        '<div class="grid">' +

            '<div class="field">' +
                '<label>Name</label>' +
                '<input class="v-name" value="x">' +
            '</div>' +

            '<div class="field">' +
                '<label>Minimum</label>' +
                '<input class="v-min" type="number" value="0.1">' +
            '</div>' +

            '<div class="field">' +
                '<label>Maximum</label>' +
                '<input class="v-max" type="number" value="10">' +
            '</div>' +

            '<div class="field">' +
                '<label>Unit</label>' +
                '<input class="v-unit" value="">' +
            '</div>' +

        '</div>' +

        '<br>' +

        '<button class="danger" onclick="this.parentElement.remove()">' +
            'Remove' +
        '</button>';

    container.appendChild(row);

}


function removeVariable() {

    const container =
        document.getElementById("variables");

    const rows =
        container.querySelectorAll(
            ".variable-row"
        );

    if (rows.length > 0) {
        rows[rows.length - 1].remove();
    }

}


function addEquation() {

    const container =
        document.getElementById("equations");

    const row =
        document.createElement("div");

    row.className =
        "builder-row equation-row";

    row.innerHTML =
        '<div class="grid">' +

            '<div class="field">' +
                '<label>Name</label>' +
                '<input class="e-name" value="equation">' +
            '</div>' +

            '<div class="field">' +
                '<label>Expression</label>' +
                '<input class="e-expression" value="x+y">' +
            '</div>' +

            '<div class="field">' +
                '<label>Unit</label>' +
                '<input class="e-unit" value="">' +
            '</div>' +

        '</div>' +

        '<br>' +

        '<button class="danger" onclick="this.parentElement.remove()">' +
            'Remove' +
        '</button>';

    container.appendChild(row);

}


function removeEquation() {

    const container =
        document.getElementById("equations");

    const rows =
        container.querySelectorAll(
            ".equation-row"
        );

    if (rows.length > 0) {
        rows[rows.length - 1].remove();
    }

}


function addConstraint() {

    const container =
        document.getElementById("constraints");

    const row =
        document.createElement("div");

    row.className =
        "builder-row constraint-row";

    row.innerHTML =
        '<div class="grid">' +

            '<div class="field">' +
                '<label>Name</label>' +
                '<input class="c-name" value="Constraint">' +
            '</div>' +

            '<div class="field">' +
                '<label>Expression</label>' +
                '<input class="c-expression" value="x">' +
            '</div>' +

            '<div class="field">' +
                '<label>Relation</label>' +
                '<select class="c-sense">' +
                    '<option value="lte">≤</option>' +
                    '<option value="gte">≥</option>' +
                    '<option value="eq">=</option>' +
                '</select>' +
            '</div>' +

            '<div class="field">' +
                '<label>Limit</label>' +
                '<input class="c-limit" type="number" value="10">' +
            '</div>' +

        '</div>' +

        '<br>' +

        '<button class="danger" onclick="this.parentElement.remove()">' +
            'Remove' +
        '</button>';

    container.appendChild(row);

}


function removeConstraint() {

    const container =
        document.getElementById("constraints");

    const rows =
        container.querySelectorAll(
            ".constraint-row"
        );

    if (rows.length > 0) {
        rows[rows.length - 1].remove();
    }

}


function addObjective() {

    const container =
        document.getElementById("objectives");

    const row =
        document.createElement("div");

    row.className =
        "builder-row objective-row";

    row.innerHTML =
        '<div class="grid">' +

            '<div class="field">' +
                '<label>Name</label>' +
                '<input class="o-name" value="Objective">' +
            '</div>' +

            '<div class="field">' +
                '<label>Expression</label>' +
                '<input class="o-expression" value="x">' +
            '</div>' +

            '<div class="field">' +
                '<label>Direction</label>' +
                '<select class="o-direction">' +
                    '<option value="min">Minimize</option>' +
                    '<option value="max">Maximize</option>' +
                '</select>' +
            '</div>' +

            '<div class="field">' +
                '<label>Weight</label>' +
                '<input class="o-weight" type="number" value="1">' +
            '</div>' +

        '</div>' +

        '<br>' +

        '<button class="danger" onclick="this.parentElement.remove()">' +
            'Remove' +
        '</button>';

    container.appendChild(row);

}


function removeObjective() {

    const container =
        document.getElementById("objectives");

    const rows =
        container.querySelectorAll(
            ".objective-row"
        );

    if (rows.length > 0) {
        rows[rows.length - 1].remove();
    }

}


function getBuilderModel() {

    const model = {

        name:
            document.getElementById(
                "projectName"
            ).value,

        description:
            document.getElementById(
                "projectDescription"
            ).value,

        variables: [],
        equations: [],
        constraints: [],
        objectives: []

    };


    document
        .querySelectorAll(".variable-row")
        .forEach(
            function(row) {

                model.variables.push({

                    name:
                        row.querySelector(
                            ".v-name"
                        ).value,

                    min:
                        Number(
                            row.querySelector(
                                ".v-min"
                            ).value
                        ),

                    max:
                        Number(
                            row.querySelector(
                                ".v-max"
                            ).value
                        ),

                    unit:
                        row.querySelector(
                            ".v-unit"
                        ).value

                });

            }
        );


    document
        .querySelectorAll(".equation-row")
        .forEach(
            function(row) {

                model.equations.push({

                    name:
                        row.querySelector(
                            ".e-name"
                        ).value,

                    expression:
                        row.querySelector(
                            ".e-expression"
                        ).value,

                    unit:
                        row.querySelector(
                            ".e-unit"
                        ).value

                });

            }
        );


    document
        .querySelectorAll(".constraint-row")
        .forEach(
            function(row) {

                model.constraints.push({

                    name:
                        row.querySelector(
                            ".c-name"
                        ).value,

                    expression:
                        row.querySelector(
                            ".c-expression"
                        ).value,

                    sense:
                        row.querySelector(
                            ".c-sense"
                        ).value,

                    limit:
                        Number(
                            row.querySelector(
                                ".c-limit"
                            ).value
                        )

                });

            }
        );


    document
        .querySelectorAll(".objective-row")
        .forEach(
            function(row) {

                model.objectives.push({

                    name:
                        row.querySelector(
                            ".o-name"
                        ).value,

                    expression:
                        row.querySelector(
                            ".o-expression"
                        ).value,

                    direction:
                        row.querySelector(
                            ".o-direction"
                        ).value,

                    weight:
                        Number(
                            row.querySelector(
                                ".o-weight"
                            ).value
                        )

                });

            }
        );


    return model;

}


function loadBuilder(model) {

    document.getElementById(
        "projectName"
    ).value =
        model.name ||
        "THETA Engineering Project";


    document.getElementById(
        "projectDescription"
    ).value =
        model.description || "";


    document.getElementById(
        "variables"
    ).innerHTML = "";

    document.getElementById(
        "equations"
    ).innerHTML = "";

    document.getElementById(
        "constraints"
    ).innerHTML = "";

    document.getElementById(
        "objectives"
    ).innerHTML = "";


    for (
        const variable
        of model.variables
    ) {

        addVariable();

        const rows =
            document.querySelectorAll(
                ".variable-row"
            );

        const row =
            rows[rows.length - 1];


        row.querySelector(
            ".v-name"
        ).value =
            variable.name;


        row.querySelector(
            ".v-min"
        ).value =
            variable.min;


        row.querySelector(
            ".v-max"
        ).value =
            variable.max;


        row.querySelector(
            ".v-unit"
        ).value =
            variable.unit || "";

    }


    for (
        const equation
        of model.equations
    ) {

        addEquation();

        const rows =
            document.querySelectorAll(
                ".equation-row"
            );

        const row =
            rows[rows.length - 1];


        row.querySelector(
            ".e-name"
        ).value =
            equation.name;


        row.querySelector(
            ".e-expression"
        ).value =
            equation.expression;


        row.querySelector(
            ".e-unit"
        ).value =
            equation.unit || "";

    }


    for (
        const constraint
        of model.constraints
    ) {

        addConstraint();

        const rows =
            document.querySelectorAll(
                ".constraint-row"
            );

        const row =
            rows[rows.length - 1];


        row.querySelector(
            ".c-name"
        ).value =
            constraint.name;


        row.querySelector(
            ".c-expression"
        ).value =
            constraint.expression;


        row.querySelector(
            ".c-sense"
        ).value =
            constraint.sense;


        row.querySelector(
            ".c-limit"
        ).value =
            constraint.limit;

    }


    for (
        const objective
        of model.objectives
    ) {

        addObjective();

        const rows =
            document.querySelectorAll(
                ".objective-row"
            );

        const row =
            rows[rows.length - 1];


        row.querySelector(
            ".o-name"
        ).value =
            objective.name;


        row.querySelector(
            ".o-expression"
        ).value =
            objective.expression;


        row.querySelector(
            ".o-direction"
        ).value =
            objective.direction;


        row.querySelector(
            ".o-weight"
        ).value =
            objective.weight;

    }

}


function openAdvancedEditor() {

    if (currentModel) {
        loadBuilder(
            currentModel
        );
    }

    showMode(
        "advanced"
    );

}


async function loadExample(name) {

    try {

        const response =
            await fetch(
                "/example?name=" +
                encodeURIComponent(name)
            );

        const data =
            await response.json();

        currentModel =
            data.model;

        loadBuilder(
            currentModel
        );

    } catch (error) {

        alert(
            "Could not load example."
        );

    }

}


function clearBuilder() {

    document.getElementById(
        "projectName"
    ).value =
        "THETA Engineering Project";


    document.getElementById(
        "projectDescription"
    ).value = "";


    document.getElementById(
        "variables"
    ).innerHTML = "";

    document.getElementById(
        "equations"
    ).innerHTML = "";

    document.getElementById(
        "constraints"
    ).innerHTML = "";

    document.getElementById(
        "objectives"
    ).innerHTML = "";


    currentModel = null;


    document.getElementById(
        "reviewPanel"
    ).classList.add(
        "hidden"
    );


    document.getElementById(
        "resultsPanel"
    ).classList.add(
        "hidden"
    );

}


async function runAdvanced() {

    const model =
        getBuilderModel();

    currentModel =
        model;

    await runOptimization(
        model
    );

}


loadExample("beam");

</script>

<style>
#thetaAccountDock{position:fixed;top:10px;right:18px;z-index:9999;font-family:Arial,Helvetica,sans-serif}
.theta-account-btn{border:1px solid #303744;background:#11151d;color:#f4f7fb;border-radius:10px;padding:9px 14px;font-weight:700}
.theta-account-panel{display:none;width:330px;margin-top:8px;background:#0c1017;border:1px solid #303744;border-radius:14px;padding:18px;box-shadow:0 20px 60px rgba(0,0,0,.45)}
.theta-account-panel.open{display:block}
.theta-account-panel h3{margin:0 0 8px}.theta-account-panel p{color:#aab3c2;font-size:13px;line-height:1.45}
.theta-account-panel input{margin:6px 0 10px;width:100%;padding:10px;background:#080b10;color:#fff;border:1px solid #303744;border-radius:8px}
.theta-account-panel button{padding:10px 12px;border:1px solid #303744;background:#f4f7fb;color:#080a0e;border-radius:8px;font-weight:700}
.theta-account-panel .secondary{background:#11151d;color:#f4f7fb}.theta-account-row{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}.theta-plan{display:inline-block;padding:4px 8px;border:1px solid #394352;border-radius:999px;font-size:11px;font-weight:700}.theta-error{color:#ff8585;font-size:12px;min-height:16px;margin-top:6px}.theta-usage{font-size:12px;color:#9aa6b5;margin:8px 0}
</style>
<div id="thetaAccountDock">
  <button class="theta-account-btn" onclick="thetaToggleAccount()">Account</button>
  <div id="thetaAccountPanel" class="theta-account-panel">
    <div id="thetaLoggedOut">
      <h3>THETA Account</h3>
      <p>Create an account or sign in. Your plan and daily usage are saved on the server.</p>
      <input id="thetaEmail" type="email" placeholder="Email">
      <input id="thetaPassword" type="password" placeholder="Password (8+ characters)">
      <div class="theta-account-row">
        <button onclick="thetaRegister()">Create account</button>
        <button class="secondary" onclick="thetaLogin()">Log in</button>
      </div>
      <div id="thetaAuthError" class="theta-error"></div>
    </div>
    <div id="thetaLoggedIn" style="display:none">
      <h3 id="thetaAccountEmail"></h3>
      <span id="thetaAccountPlan" class="theta-plan">Free</span>
      <div id="thetaUsage" class="theta-usage"></div>
      <div class="theta-account-row">
        <button onclick="thetaUpgrade('Pro')">Pro</button>
        <button onclick="thetaUpgrade('Engineer')">Engineer</button>
        <button class="secondary" onclick="thetaPortal()">Billing</button>
        <button class="secondary" onclick="thetaLogout()">Log out</button>
      </div>
      <div id="thetaAccountError" class="theta-error"></div>
    </div>
  </div>
</div>
<script>
async function thetaApi(path, options={}){
  const response=await fetch(path,{credentials:'same-origin',...options});
  let data={};
  try{data=await response.json();}catch(e){}
  if(!response.ok) throw new Error(data.error||'Request failed.');
  return data;
}
function thetaToggleAccount(){document.getElementById('thetaAccountPanel').classList.toggle('open');}
function thetaSetUser(user){
  const out=document.getElementById('thetaLoggedOut');
  const inn=document.getElementById('thetaLoggedIn');
  if(!user||!user.authenticated){out.style.display='block';inn.style.display='none';return;}
  out.style.display='none';inn.style.display='block';
  document.getElementById('thetaAccountEmail').textContent=user.email;
  document.getElementById('thetaAccountPlan').textContent=user.plan;
  document.getElementById('thetaUsage').textContent='Today: '+user.used+' / '+user.limit+' runs';
}
async function thetaRefreshAccount(){
  try{const data=await thetaApi('/auth/me');thetaSetUser(data.user);}catch(e){console.error(e);}
}
async function thetaRegister(){
  const error=document.getElementById('thetaAuthError');error.textContent='';
  try{const data=await thetaApi('/auth/register',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email:document.getElementById('thetaEmail').value,password:document.getElementById('thetaPassword').value})});thetaSetUser(data.user);}
  catch(e){error.textContent=e.message;}
}
async function thetaLogin(){
  const error=document.getElementById('thetaAuthError');error.textContent='';
  try{const data=await thetaApi('/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email:document.getElementById('thetaEmail').value,password:document.getElementById('thetaPassword').value})});thetaSetUser(data.user);}
  catch(e){error.textContent=e.message;}
}
async function thetaLogout(){
  try{await thetaApi('/auth/logout');thetaSetUser(null);}catch(e){document.getElementById('thetaAccountError').textContent=e.message;}
}
async function thetaUpgrade(plan){
  const error=document.getElementById('thetaAccountError');error.textContent='';
  try{const data=await thetaApi('/billing/checkout?plan='+encodeURIComponent(plan));window.location.href=data.url;}
  catch(e){error.textContent=e.message;}
}
async function thetaPortal(){
  const error=document.getElementById('thetaAccountError');error.textContent='';
  try{const data=await thetaApi('/billing/portal');window.location.href=data.url;}
  catch(e){error.textContent=e.message;}
}
thetaRefreshAccount();
</script>
</body>
</html>
"""


# ============================================================
# HTTP SERVER
# ============================================================

class ThetaHandler(BaseHTTPRequestHandler):

    def send_json(self, data, status=200, cookies=None):
        payload = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        for cookie in cookies or []:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(payload)


    def send_html(self, html, status=200, cookies=None):
        payload = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        for cookie in cookies or []:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(payload)


    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length > 2_000_000:
            raise ValueError("Request is too large.")
        body = self.rfile.read(length)
        if not body:
            return {}
        return json.loads(body.decode("utf-8"))


    def current_user(self):
        return auth_user(self)


    def require_user(self):
        user = self.current_user()
        if not user:
            self.send_json(
                {"success": False, "error": "Please sign in to use THETA."},
                401,
            )
            return None
        return user


    def require_usage(self):
        user = self.require_user()
        if not user:
            return None

        allowed, used, limit = consume_usage(user)
        if not allowed:
            self.send_json(
                {
                    "success": False,
                    "error": f"Daily {user['plan']} limit reached.",
                    "plan": user["plan"],
                    "used": used,
                    "limit": limit,
                },
                429,
            )
            return None

        return user


    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/":
            self.send_html(HTML)
            return

        if parsed.path == "/health":
            self.send_json({
                "status": "online",
                "engine": "THETA",
                "accounts": True,
                "stripe": bool(
                    STRIPE_SECRET_KEY
                    and STRIPE_WEBHOOK_SECRET
                    and STRIPE_PRO_PRICE_ID
                    and STRIPE_ENGINEER_PRICE_ID
                ),
            })
            return

        if parsed.path == "/auth/me":
            self.send_json({
                "success": True,
                "user": public_user(self.current_user()),
            })
            return

        if parsed.path == "/auth/logout":
            token = cookie_value(self, "theta_session")
            delete_session(token)
            cookies = []
            append_cookie(cookies, "theta_session", "", max_age=0)
            self.send_json({"success": True}, cookies=cookies)
            return

        if parsed.path == "/billing/checkout":
            user = self.require_user()
            if not user:
                return
            query = parse_qs(parsed.query)
            plan = query.get("plan", [""])[0]
            try:
                url = create_checkout_url(user, plan)
                self.send_json({"success": True, "url": url})
            except Exception as error:
                self.send_json({"success": False, "error": str(error)}, 400)
            return

        if parsed.path == "/billing/portal":
            user = self.require_user()
            if not user:
                return
            try:
                if not user["stripe_customer_id"]:
                    raise RuntimeError("No Stripe customer is associated with this account yet.")
                base_url = os.environ.get("THETA_BASE_URL", "").strip().rstrip("/") or f"http://{HOST}:{PORT}"
                portal = stripe_request(
                    "POST",
                    "billing_portal/sessions",
                    {
                        "customer": user["stripe_customer_id"],
                        "return_url": base_url + "/",
                    },
                )
                self.send_json({"success": True, "url": portal["url"]})
            except Exception as error:
                self.send_json({"success": False, "error": str(error)}, 400)
            return

        if parsed.path == "/example":
            query = parse_qs(parsed.query)
            name = query.get("name", ["beam"])[0]
            if name == "spring":
                model = spring_example()
            elif name == "drone":
                model = drone_example()
            elif name == "bracket":
                model = bracket_example()
            else:
                model = beam_example()
            self.send_json({"success": True, "model": normalize_model(model)})
            return

        self.send_json({"success": False, "error": "Not found"}, 404)


    def do_POST(self):
        parsed = urlparse(self.path)

        try:
            if parsed.path == "/auth/register":
                data = self.read_json()
                email = normalize_email(data.get("email", ""))
                password = data.get("password", "")
                user = create_user(email, password)
                token = create_session(user["id"])
                cookies = []
                append_cookie(cookies, "theta_session", token, max_age=SESSION_DAYS * 86400)
                self.send_json({"success": True, "user": public_user(user)}, cookies=cookies)
                return

            if parsed.path == "/auth/login":
                data = self.read_json()
                email = normalize_email(data.get("email", ""))
                password = data.get("password", "")
                user = get_user_by_email(email)
                if not user or not verify_password(password, user["password_hash"]):
                    self.send_json({"success": False, "error": "Invalid email or password."}, 401)
                    return
                token = create_session(user["id"])
                cookies = []
                append_cookie(cookies, "theta_session", token, max_age=SESSION_DAYS * 86400)
                self.send_json({"success": True, "user": public_user(user)}, cookies=cookies)
                return

            if parsed.path == "/stripe/webhook":
                length = int(self.headers.get("Content-Length", "0"))
                if length > 2_000_000:
                    self.send_json({"success": False, "error": "Webhook too large."}, 413)
                    return
                raw_body = self.rfile.read(length)
                signature = self.headers.get("Stripe-Signature", "")
                if not stripe_signature_valid(raw_body, signature):
                    self.send_json({"success": False, "error": "Invalid Stripe signature."}, 400)
                    return
                event = json.loads(raw_body.decode("utf-8"))
                handle_stripe_event(event)
                self.send_json({"received": True})
                return

            if parsed.path == "/interpret":
                user = self.require_usage()
                if not user:
                    return
                data = self.read_json()
                text = data.get("text", "")
                if not str(text).strip():
                    raise ValueError("Enter an engineering problem first.")
                model = interpret_engineering_request(text)
                self.send_json({
                    "success": True,
                    "model": normalize_model(model),
                    "usage": {
                        "used": get_today_usage(user["id"]),
                        "limit": PLAN_LIMITS[user["plan"]],
                        "plan": user["plan"],
                    },
                })
                return

            if parsed.path == "/optimize":
                user = self.require_usage()
                if not user:
                    return
                project = self.read_json()
                result = optimize_model(project)
                result["usage"] = {
                    "used": get_today_usage(user["id"]),
                    "limit": PLAN_LIMITS[user["plan"]],
                    "plan": user["plan"],
                }
                self.send_json(result)
                return

            self.send_json({"success": False, "error": "Not found"}, 404)

        except ValueError as error:
            self.send_json({"success": False, "error": str(error)}, 400)
        except Exception as error:
            print("[THETA ERROR]", repr(error))
            self.send_json({"success": False, "error": str(error)}, 500)


    def log_message(self, format_string, *args):
        print("[THETA]", format_string % args)


# ============================================================
# START THETA
# ============================================================

def open_browser():

    webbrowser.open(
        "http://127.0.0.1:" +
        str(PORT)
    )


def main():

    server = ThreadingHTTPServer(
        (HOST, PORT),
        ThetaHandler
    )

    print("")
    print("=" * 70)
    print("THETA TECHNOLOGY DISCOVERY ENGINE")
    print("=" * 70)
    print("")
    print("THETA is running at:")
    print(
        "http://127.0.0.1:" +
        str(PORT)
    )
    print("")
    print("Press CTRL+C to stop THETA.")
    print("")


    if not os.environ.get("RENDER"):

        threading.Timer(
            1.0,
            open_browser
        ).start()


    try:

        server.serve_forever()

    except KeyboardInterrupt:

        print("")
        print("Stopping THETA...")

    finally:

        server.server_close()


if __name__ == "__main__":

    main()
