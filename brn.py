import ast
import json
import math
import os
import random
import re
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs


# ============================================================
# THETA TECHNOLOGY DISCOVERY ENGINE
# V1 - MONETIZABLE LAUNCH EDITION
# ============================================================

HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "8000"))

# Put your Stripe Payment Links into Render environment variables
# after creating them.
PRO_PAYMENT_LINK = os.environ.get(
    "THETA_PRO_PAYMENT_LINK",
    "https://buy.stripe.com/REPLACE_WITH_YOUR_PRO_LINK"
)

ENGINEER_PAYMENT_LINK = os.environ.get(
    "THETA_ENGINEER_PAYMENT_LINK",
    "https://buy.stripe.com/REPLACE_WITH_YOUR_ENGINEER_LINK"
)


# ============================================================
# SAFE MATHEMATICS
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
    ALLOWED_NODES = (
        ast.Expression,
        ast.Constant,
        ast.Name,
        ast.BinOp,
        ast.UnaryOp,
        ast.Add,
        ast.Sub,
        ast.Mult,
        ast.Div,
        ast.Pow,
        ast.Mod,
        ast.USub,
        ast.UAdd,
        ast.Call,
        ast.Load,
        ast.Compare,
        ast.Gt,
        ast.GtE,
        ast.Lt,
        ast.LtE,
        ast.Eq,
        ast.NotEq,
        ast.BoolOp,
        ast.And,
        ast.Or,
    )

    def __init__(self, expression):
        self.expression = expression
        self.tree = ast.parse(expression, mode="eval")

        for node in ast.walk(self.tree):
            if not isinstance(node, self.ALLOWED_NODES):
                raise ValueError(
                    f"Unsupported expression element: {type(node).__name__}"
                )

    def evaluate(self, variables):
        return self._evaluate_node(self.tree.body, variables)

    def _evaluate_node(self, node, variables):
        if isinstance(node, ast.Constant):
            if isinstance(node.value, (int, float, bool)):
                return node.value
            raise ValueError("Invalid constant")

        if isinstance(node, ast.Name):
            if node.id in variables:
                return variables[node.id]

            if node.id in SAFE_CONSTANTS:
                return SAFE_CONSTANTS[node.id]

            raise ValueError(f"Unknown variable: {node.id}")

        if isinstance(node, ast.BinOp):
            left = self._evaluate_node(node.left, variables)
            right = self._evaluate_node(node.right, variables)

            if isinstance(node.op, ast.Add):
                return left + right

            if isinstance(node.op, ast.Sub):
                return left - right

            if isinstance(node.op, ast.Mult):
                return left * right

            if isinstance(node.op, ast.Div):
                return left / right

            if isinstance(node.op, ast.Pow):
                return left ** right

            if isinstance(node.op, ast.Mod):
                return left % right

            raise ValueError("Unsupported operator")

        if isinstance(node, ast.UnaryOp):
            value = self._evaluate_node(node.operand, variables)

            if isinstance(node.op, ast.USub):
                return -value

            if isinstance(node.op, ast.UAdd):
                return value

            raise ValueError("Unsupported unary operator")

        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name):
                raise ValueError("Invalid function")

            function_name = node.func.id

            if function_name not in SAFE_FUNCTIONS:
                raise ValueError(f"Function not allowed: {function_name}")

            args = [
                self._evaluate_node(argument, variables)
                for argument in node.args
            ]

            return SAFE_FUNCTIONS[function_name](*args)

        if isinstance(node, ast.Compare):
            left = self._evaluate_node(node.left, variables)

            results = []

            for operator, comparator in zip(
                node.ops,
                node.comparators
            ):
                right = self._evaluate_node(comparator, variables)

                if isinstance(operator, ast.Gt):
                    results.append(left > right)
                elif isinstance(operator, ast.GtE):
                    results.append(left >= right)
                elif isinstance(operator, ast.Lt):
                    results.append(left < right)
                elif isinstance(operator, ast.LtE):
                    results.append(left <= right)
                elif isinstance(operator, ast.Eq):
                    results.append(left == right)
                elif isinstance(operator, ast.NotEq):
                    results.append(left != right)
                else:
                    raise ValueError("Unsupported comparison")

                left = right

            return all(results)

        if isinstance(node, ast.BoolOp):
            values = [
                self._evaluate_node(value, variables)
                for value in node.values
            ]

            if isinstance(node.op, ast.And):
                return all(values)

            if isinstance(node.op, ast.Or):
                return any(values)

        raise ValueError(
            f"Could not evaluate {type(node).__name__}"
        )


def evaluate_expression(expression, variables):
    try:
        evaluator = SafeExpression(expression)
        return evaluator.evaluate(variables)
    except Exception:
        return float("nan")


# ============================================================
# MODEL HELPERS
# ============================================================

def empty_model():
    return {
        "name": "Untitled Design",
        "type": "custom",
        "description": "",
        "variables": [],
        "equations": [],
        "constraints": [],
        "objectives": [],
    }


def clean_name(value):
    value = str(value).strip()

    value = "".join(
        character
        for character in value
        if character.isalnum() or character == "_"
    )

    if not value:
        value = "x"

    if value[0].isdigit():
        value = "x_" + value

    return value


def normalize_model(model):
    result = empty_model()

    if not isinstance(model, dict):
        return result

    result["name"] = str(
        model.get("name", result["name"])
    )

    result["type"] = str(
        model.get("type", "custom")
    )

    result["description"] = str(
        model.get("description", "")
    )

    for variable in model.get("variables", []):
        if not isinstance(variable, dict):
            continue

        name = clean_name(variable.get("name", "x"))

        try:
            minimum = float(variable.get("min", 0.1))
            maximum = float(variable.get("max", 1.0))
        except Exception:
            minimum = 0.1
            maximum = 1.0

        if maximum <= minimum:
            maximum = minimum + 1.0

        result["variables"].append({
            "name": name,
            "min": minimum,
            "max": maximum,
            "unit": str(variable.get("unit", "")),
        })

    for equation in model.get("equations", []):
        if not isinstance(equation, dict):
            continue

        result["equations"].append({
            "name": clean_name(equation.get("name", "result")),
            "expression": str(
                equation.get("expression", "0")
            ),
            "unit": str(equation.get("unit", "")),
        })

    for constraint in model.get("constraints", []):
        if not isinstance(constraint, dict):
            continue

        result["constraints"].append({
            "expression": str(
                constraint.get("expression", "0 >= 0")
            )
        })

    for objective in model.get("objectives", []):
        if not isinstance(objective, dict):
            continue

        direction = str(
            objective.get("direction", "minimize")
        ).lower()

        if direction not in ("minimize", "maximize"):
            direction = "minimize"

        result["objectives"].append({
            "expression": str(
                objective.get("expression", "0")
            ),
            "direction": direction,
        })

    return result


# ============================================================
# NATURAL LANGUAGE ENGINEERING INTERPRETER
# ============================================================

def extract_number(text, patterns, default=None):
    for pattern in patterns:
        match = re.search(
            pattern,
            text,
            flags=re.IGNORECASE
        )

        if match:
            try:
                return float(match.group(1))
            except Exception:
                pass

    return default


def interpret_engineering_request(text):
    original = text
    text = text.lower().strip()

    model = empty_model()
    model["description"] = original

    # --------------------------------------------------------
    # BEAM
    # --------------------------------------------------------

    if any(
        word in text
        for word in [
            "beam",
            "cantilever",
            "beam design",
        ]
    ):
        model["name"] = "Lightweight Beam"
        model["type"] = "beam"

        load = extract_number(
            text,
            [
                r"hold\s+([0-9.]+)\s*n",
                r"load\s+(?:of\s+)?([0-9.]+)\s*n",
                r"([0-9.]+)\s*n\s+load",
            ],
            500.0,
        )

        length = extract_number(
            text,
            [
                r"over\s+([0-9.]+)\s*m",
                r"length\s+(?:of\s+)?([0-9.]+)\s*m",
                r"([0-9.]+)\s*m\s+(?:long|beam)",
            ],
            1.0,
        )

        stress_limit = extract_number(
            text,
            [
                r"maximum\s+stress\s+of\s+([0-9.]+)\s*mpa",
                r"stress\s+limit\s+(?:of\s+)?([0-9.]+)\s*mpa",
                r"([0-9.]+)\s*mpa\s+(?:stress|limit)",
            ],
            200.0,
        )

        model["variables"] = [
            {
                "name": "b",
                "min": 0.01,
                "max": 0.20,
                "unit": "m",
            },
            {
                "name": "h",
                "min": 0.01,
                "max": 0.20,
                "unit": "m",
            },
        ]

        model["equations"] = [
            {
                "name": "moment",
                "expression": f"{load} * {length}",
                "unit": "N*m",
            },
            {
                "name": "section",
                "expression": "b * h^2 / 6",
                "unit": "m^3",
            },
            {
                "name": "stress",
                "expression": f"({load} * {length}) / (b * h^2 / 6)",
                "unit": "Pa",
            },
            {
                "name": "area",
                "expression": "b * h",
                "unit": "m^2",
            },
            {
                "name": "mass",
                "expression": f"b * h * {length} * 7850",
                "unit": "kg",
            },
        ]

        model["constraints"] = [
            f"stress <= {stress_limit * 1000000}"
        ]

        model["objectives"] = [
            {
                "expression": "mass",
                "direction": "minimize",
            }
        ]

        return normalize_model(model)

    # --------------------------------------------------------
    # SPRING
    # --------------------------------------------------------

    if "spring" in text:
        model["name"] = "Lightweight Spring"
        model["type"] = "spring"

        force = extract_number(
            text,
            [
                r"spring\s+for\s+([0-9.]+)\s*n",
                r"handle\s+([0-9.]+)\s*n",
                r"force\s+(?:of\s+)?([0-9.]+)\s*n",
            ],
            100.0,
        )

        model["variables"] = [
            {
                "name": "d",
                "min": 0.001,
                "max": 0.010,
                "unit": "m",
            },
            {
                "name": "D",
                "min": 0.010,
                "max": 0.080,
                "unit": "m",
            },
            {
                "name": "n",
                "min": 3,
                "max": 20,
                "unit": "turns",
            },
        ]

        model["equations"] = [
            {
                "name": "stiffness",
                "expression": "79000000000 * d^4 / (8 * D^3 * n)",
                "unit": "N/m",
            },
            {
                "name": "deflection",
                "expression": f"{force} / (79000000000 * d^4 / (8 * D^3 * n))",
                "unit": "m",
            },
            {
                "name": "mass",
                "expression": "pi * D * n * pi * d^2 / 4 * 7850",
                "unit": "kg",
            },
        ]

        model["constraints"] = [
            f"stiffness >= {force / 0.05}"
        ]

        model["objectives"] = [
            {
                "expression": "mass",
                "direction": "minimize",
            }
        ]

        return normalize_model(model)

    # --------------------------------------------------------
    # DRONE
    # --------------------------------------------------------

    if any(
        word in text
        for word in [
            "drone",
            "quadcopter",
            "quad",
            "uav",
        ]
    ):
        model["name"] = "Lightweight Drone"
        model["type"] = "drone"

        model["variables"] = [
            {
                "name": "arm",
                "min": 0.10,
                "max": 0.40,
                "unit": "m",
            },
            {
                "name": "motor_mass",
                "min": 0.020,
                "max": 0.100,
                "unit": "kg",
            },
            {
                "name": "battery_mass",
                "min": 0.10,
                "max": 0.60,
                "unit": "kg",
            },
        ]

        model["equations"] = [
            {
                "name": "frame_mass",
                "expression": "4 * arm * 0.20",
                "unit": "kg",
            },
            {
                "name": "total_mass",
                "expression": "frame_mass + 4 * motor_mass + battery_mass",
                "unit": "kg",
            },
            {
                "name": "payload_margin",
                "expression": "4 * 2.5 - total_mass",
                "unit": "kg",
            },
        ]

        model["constraints"] = [
            "payload_margin >= 0"
        ]

        model["objectives"] = [
            {
                "expression": "total_mass",
                "direction": "minimize",
            }
        ]

        return normalize_model(model)

    # --------------------------------------------------------
    # BRACKET
    # --------------------------------------------------------

    if any(
        word in text
        for word in [
            "bracket",
            "mount",
            "mounting plate",
            "structural mount",
        ]
    ):
        model["name"] = "Lightweight Mounting Bracket"
        model["type"] = "bracket"

        model["variables"] = [
            {
                "name": "width",
                "min": 0.03,
                "max": 0.20,
                "unit": "m",
            },
            {
                "name": "height",
                "min": 0.03,
                "max": 0.20,
                "unit": "m",
            },
            {
                "name": "thickness",
                "min": 0.005,
                "max": 0.040,
                "unit": "m",
            },
        ]

        model["equations"] = [
            {
                "name": "volume",
                "expression": "width * height * thickness",
                "unit": "m^3",
            },
            {
                "name": "mass",
                "expression": "volume * 7850",
                "unit": "kg",
            },
            {
                "name": "section",
                "expression": "width * thickness^2 / 6",
                "unit": "m^3",
            },
        ]

        model["constraints"] = [
            "thickness >= 0.005"
        ]

        model["objectives"] = [
            {
                "expression": "mass",
                "direction": "minimize",
            }
        ]

        return normalize_model(model)

    # --------------------------------------------------------
    # GENERIC MODEL
    # --------------------------------------------------------

    model["name"] = "Technology Discovery Model"
    model["type"] = "custom"

    model["variables"] = [
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
    ]

    model["equations"] = [
        {
            "name": "performance",
            "expression": "x + y",
            "unit": "",
        },
        {
            "name": "cost",
            "expression": "x^2 + y^2",
            "unit": "",
        },
    ]

    model["constraints"] = []

    model["objectives"] = [
        {
            "expression": "performance",
            "direction": "maximize",
        }
    ]

    return normalize_model(model)


# ============================================================
# MODEL CALCULATION
# ============================================================

def calculate_model(model, design):
    variables = dict(design)
    equations = {}

    for equation in model.get("equations", []):
        expression = equation["expression"].replace("^", "**")

        try:
            value = evaluate_expression(
                expression,
                {
                    **variables,
                    **equations,
                }
            )
        except Exception:
            value = float("nan")

        equations[equation["name"]] = value

    return equations


def constraint_violation(model, design, results=None):
    if results is None:
        results = calculate_model(model, design)

    variables = {
        **design,
        **results,
    }

    total_violation = 0.0
    passed = True

    for constraint in model.get("constraints", []):
        expression = constraint["expression"].replace(
            "^",
            "**"
        )

        try:
            value = evaluate_expression(
                expression,
                variables
            )

            if isinstance(value, bool):
                if not value:
                    total_violation += 1.0
                    passed = False
                continue

            if value < 0:
                total_violation += abs(value)
                passed = False

        except Exception:
            total_violation += 1000000.0
            passed = False

    return total_violation, passed


def objective_value(model, design, results=None):
    if results is None:
        results = calculate_model(model, design)

    variables = {
        **design,
        **results,
    }

    objectives = model.get("objectives", [])

    if not objectives:
        return 0.0, "maximize"

    objective = objectives[0]

    expression = objective["expression"].replace(
        "^",
        "**"
    )

    try:
        value = evaluate_expression(
            expression,
            variables
        )

        return float(value), objective["direction"]

    except Exception:
        return float("inf"), objective["direction"]


def evaluate_design(model, design):
    results = calculate_model(model, design)

    violation, passed = constraint_violation(
        model,
        design,
        results
    )

    objective, direction = objective_value(
        model,
        design,
        results
    )

    return {
        "design": design,
        "results": results,
        "violation": violation,
        "passed": passed,
        "objective": objective,
        "direction": direction,
    }


# ============================================================
# OPTIMIZATION ENGINE
# ============================================================

def random_design(model):
    design = {}

    for variable in model.get("variables", []):
        minimum = float(variable["min"])
        maximum = float(variable["max"])

        design[variable["name"]] = (
            random.uniform(minimum, maximum)
        )

    return design


def mutate_design(model, design, strength=0.15):
    new_design = dict(design)

    for variable in model.get("variables", []):
        name = variable["name"]
        minimum = float(variable["min"])
        maximum = float(variable["max"])

        current = new_design[name]
        span = maximum - minimum

        change = random.gauss(
            0,
            span * strength
        )

        value = current + change

        value = max(
            minimum,
            min(maximum, value)
        )

        new_design[name] = value

    return new_design


def score_result(result):
    violation = result["violation"]
    objective = result["objective"]
    direction = result["direction"]

    if not math.isfinite(objective):
        return -1e30

    penalty = violation * 1000000

    if direction == "minimize":
        return -objective - penalty

    return objective - penalty


def optimize_model(
    model,
    population_size=500,
    generations=60
):
    model = normalize_model(model)

    population = []

    for _ in range(population_size):
        design = random_design(model)
        population.append(
            evaluate_design(model, design)
        )

    history = []

    for generation in range(generations):
        population.sort(
            key=score_result,
            reverse=True
        )

        best = population[0]

        history.append({
            "generation": generation + 1,
            "score": score_result(best),
            "objective": best["objective"],
            "passed": best["passed"],
        })

        survivors = population[
            :max(10, population_size // 10)
        ]

        new_population = survivors[:]

        while len(new_population) < population_size:
            parent = random.choice(survivors)

            design = mutate_design(
                model,
                parent["design"],
                strength=max(
                    0.02,
                    0.25 * (
                        1 -
                        generation / generations
                    )
                )
            )

            new_population.append(
                evaluate_design(model, design)
            )

        population = new_population

    population.sort(
        key=score_result,
        reverse=True
    )

    best_results = population[:10]

    return {
        "best": best_results[0],
        "alternatives": best_results[:10],
        "history": history,
        "population_size": population_size,
        "generations": generations,
    }


# ============================================================
# EXAMPLE MODELS
# ============================================================

def beam_example():
    return interpret_engineering_request(
        "Design a lightweight beam that can hold 500 N."
    )


def spring_example():
    return interpret_engineering_request(
        "Design a lightweight spring for 100 N."
    )


def drone_example():
    return interpret_engineering_request(
        "Design a lightweight drone."
    )


def bracket_example():
    return interpret_engineering_request(
        "Design a lightweight mounting bracket."
    )


# ============================================================
# HTML APPLICATION
# ============================================================

HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">

<meta
    name="google-site-verification"
    content="MMIdUHh9590WwUT1WeDykMUXzQPk8wpeor6DDPGCAp4"
>

<meta
    name="viewport"
    content="width=device-width, initial-scale=1.0"
>

<title>THETA — Technology Discovery Engine</title>

<style>

:root {
    --bg: #07090d;
    --panel: #0d1118;
    --panel2: #111722;
    --border: #252d3a;
    --text: #f4f7fb;
    --muted: #8e99a8;
    --accent: #ffffff;
    --green: #53e08b;
    --red: #ff6b6b;
}

* {
    box-sizing: border-box;
}

html {
    scroll-behavior: smooth;
}

body {
    margin: 0;
    background:
        radial-gradient(
            circle at 50% -20%,
            #182131 0,
            #07090d 42%
        );
    color: var(--text);
    font-family:
        Inter,
        Arial,
        Helvetica,
        sans-serif;
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
    height: 70px;
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0 28px;
    background: rgba(7, 9, 13, 0.90);
    backdrop-filter: blur(15px);
    position: sticky;
    top: 0;
    z-index: 20;
}

.logo {
    font-size: 23px;
    font-weight: 900;
    letter-spacing: 5px;
}

.status {
    color: var(--green);
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 1.5px;
}

.hero {
    max-width: 1100px;
    margin: 0 auto;
    padding: 90px 24px 50px;
    text-align: center;
}

.eyebrow {
    display: inline-block;
    border: 1px solid var(--border);
    background: rgba(255,255,255,.03);
    padding: 8px 14px;
    border-radius: 999px;
    font-size: 12px;
    letter-spacing: 1.4px;
    color: var(--muted);
    text-transform: uppercase;
}

.hero h1 {
    font-size: clamp(42px, 8vw, 82px);
    line-height: .95;
    margin: 24px 0;
    letter-spacing: -4px;
}

.hero p {
    max-width: 720px;
    margin: 0 auto;
    color: var(--muted);
    font-size: 18px;
    line-height: 1.7;
}

.modebar {
    max-width: 1100px;
    margin: 0 auto 25px;
    padding: 0 24px;
    display: flex;
    gap: 10px;
    justify-content: center;
}

.mode-button {
    background: var(--panel);
    border: 1px solid var(--border);
    color: var(--muted);
    padding: 11px 18px;
    border-radius: 9px;
}

.mode-button.active {
    background: #ffffff;
    color: #000000;
}

.main {
    max-width: 1100px;
    margin: 0 auto;
    padding: 0 24px 100px;
}

.panel {
    background: rgba(13,17,24,.92);
    border: 1px solid var(--border);
    border-radius: 18px;
    overflow: hidden;
    margin-bottom: 22px;
}

.panel-header {
    padding: 20px 22px;
    border-bottom: 1px solid var(--border);
}

.panel-header h2 {
    margin: 0;
    font-size: 17px;
}

.panel-header p {
    margin: 7px 0 0;
    color: var(--muted);
    font-size: 13px;
}

.chat {
    min-height: 300px;
    max-height: 500px;
    overflow-y: auto;
    padding: 22px;
}

.message {
    margin-bottom: 18px;
    max-width: 850px;
}

.message.user {
    margin-left: auto;
}

.message-bubble {
    display: inline-block;
    padding: 14px 16px;
    border-radius: 13px;
    line-height: 1.55;
    white-space: pre-wrap;
}

.message.ai .message-bubble {
    background: #111722;
    border: 1px solid var(--border);
}

.message.user .message-bubble {
    background: #ffffff;
    color: #000000;
}

.message-label {
    font-size: 10px;
    color: var(--muted);
    margin-bottom: 5px;
    letter-spacing: 1px;
    text-transform: uppercase;
}

.input-area {
    padding: 18px;
    border-top: 1px solid var(--border);
}

.chat-row {
    display: flex;
    gap: 10px;
}

.chat-input {
    flex: 1;
    background: #080b10;
    border: 1px solid var(--border);
    color: white;
    border-radius: 10px;
    padding: 14px;
    outline: none;
}

.chat-input:focus {
    border-color: #657083;
}

.primary-button {
    background: white;
    color: black;
    border: none;
    border-radius: 10px;
    padding: 12px 19px;
    font-weight: 800;
}

.secondary-button {
    background: transparent;
    color: white;
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 12px 19px;
}

.quick-buttons {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
    margin-top: 12px;
}

.quick-button {
    background: #111722;
    color: #dce3ed;
    border: 1px solid var(--border);
    border-radius: 999px;
    padding: 8px 13px;
    font-size: 12px;
}

.example-box {
    margin-top: 15px;
    border: 1px solid var(--border);
    background: #0a0e14;
    border-radius: 12px;
    padding: 13px;
}

.example-title {
    color: var(--muted);
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 1px;
    margin-bottom: 10px;
}

.example-choice {
    display: block;
    width: 100%;
    text-align: left;
    border: 1px solid var(--border);
    background: #101620;
    color: white;
    border-radius: 9px;
    padding: 11px;
    margin: 7px 0;
}

.example-choice:hover {
    background: #18202c;
}

.results-grid {
    display: grid;
    grid-template-columns:
        repeat(4, minmax(0, 1fr));
    gap: 12px;
    padding: 22px;
}

.stat {
    background: #0a0e14;
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 17px;
}

.stat-label {
    color: var(--muted);
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 1px;
}

.stat-value {
    font-size: 23px;
    font-weight: 800;
    margin-top: 9px;
}

.pass {
    color: var(--green);
}

.fail {
    color: var(--red);
}

.result-body {
    padding: 0 22px 22px;
}

.design-table {
    width: 100%;
    border-collapse: collapse;
}

.design-table th,
.design-table td {
    padding: 12px;
    border-bottom: 1px solid var(--border);
    text-align: left;
    font-size: 13px;
}

.design-table th {
    color: var(--muted);
    font-weight: 600;
}

.upgrade {
    margin-top: 20px;
    padding: 25px;
    border: 1px solid var(--border);
    border-radius: 15px;
    background:
        linear-gradient(
            135deg,
            #111722,
            #0a0d12
        );
}

.upgrade h3 {
    margin: 0 0 7px;
}

.upgrade p {
    color: var(--muted);
    line-height: 1.6;
}

.pricing-grid {
    display: grid;
    grid-template-columns:
        repeat(3, minmax(0, 1fr));
    gap: 14px;
    padding: 22px;
}

.price-card {
    border: 1px solid var(--border);
    border-radius: 15px;
    padding: 22px;
    background: #0a0e14;
}

.price-card.featured {
    border-color: #687486;
}

.price {
    font-size: 31px;
    font-weight: 900;
    margin: 15px 0;
}

.price span {
    font-size: 13px;
    color: var(--muted);
    font-weight: normal;
}

.feature-list {
    color: var(--muted);
    line-height: 2;
    padding-left: 20px;
    min-height: 135px;
}

.advanced {
    display: none;
}

.builder-grid {
    display: grid;
    grid-template-columns:
        repeat(2, minmax(0, 1fr));
    gap: 14px;
    padding: 22px;
}

.builder-section {
    border: 1px solid var(--border);
    border-radius: 13px;
    padding: 15px;
    background: #0a0e14;
}

.builder-section h3 {
    margin-top: 0;
}

.builder-input {
    width: 100%;
    background: #080b10;
    color: white;
    border: 1px solid var(--border);
    padding: 10px;
    border-radius: 8px;
    margin: 5px 0;
}

.builder-item {
    border: 1px solid var(--border);
    padding: 10px;
    border-radius: 8px;
    margin-top: 8px;
    color: var(--muted);
    font-size: 12px;
}

.remove-button {
    float: right;
    background: transparent;
    border: none;
    color: var(--red);
}

.footer {
    border-top: 1px solid var(--border);
    padding: 30px 24px;
    text-align: center;
    color: var(--muted);
    font-size: 12px;
}

.disclaimer {
    max-width: 850px;
    margin: 30px auto 0;
    color: #707a88;
    font-size: 11px;
    line-height: 1.6;
    text-align: center;
}

@media (max-width: 800px) {

    .topbar {
        padding: 0 16px;
    }

    .hero {
        padding-top: 60px;
    }

    .results-grid {
        grid-template-columns:
            repeat(2, minmax(0, 1fr));
    }

    .pricing-grid {
        grid-template-columns: 1fr;
    }

    .builder-grid {
        grid-template-columns: 1fr;
    }

    .chat-row {
        flex-direction: column;
    }

    .hero h1 {
        letter-spacing: -2px;
    }
}

</style>
</head>

<body>

<header class="topbar">

    <div class="logo">
        THETA
    </div>

    <div class="status">
        ● ENGINE ONLINE
    </div>

</header>


<section class="hero">

    <div class="eyebrow">
        Technology Discovery Engine
    </div>

    <h1>
        Describe a problem.<br>
        Discover a design.
    </h1>

    <p>
        THETA explores engineering design spaces,
        searches possible configurations, and identifies
        promising designs automatically.
    </p>

</section>


<div class="modebar">

    <button
        id="beginnerModeButton"
        class="mode-button active"
        onclick="showMode('beginner')"
    >
        Beginner
    </button>

    <button
        id="advancedModeButton"
        class="mode-button"
        onclick="showMode('advanced')"
    >
        Advanced
    </button>

</div>


<main class="main">


<!-- ====================================================== -->
<!-- BEGINNER PRODUCT -->
<!-- ====================================================== -->

<section id="beginnerPanel">

    <div class="panel">

        <div class="panel-header">

            <h2>
                THETA Design Assistant
            </h2>

            <p>
                Describe what you want to design in normal language.
            </p>

        </div>


        <div
            id="messages"
            class="chat"
        ></div>


        <div class="input-area">

            <div class="chat-row">

                <input
                    id="chatInput"
                    class="chat-input"
                    placeholder="Example: Design a lightweight beam that can hold 500 N."
                    onkeydown="handleChatKey(event)"
                >

                <button
                    class="primary-button"
                    onclick="sendChat()"
                >
                    Discover
                </button>

            </div>


            <div class="quick-buttons">

                <button
                    class="quick-button"
                    onclick="loadExample('beam')"
                >
                    Beam
                </button>

                <button
                    class="quick-button"
                    onclick="loadExample('spring')"
                >
                    Spring
                </button>

                <button
                    class="quick-button"
                    onclick="loadExample('drone')"
                >
                    Drone
                </button>

                <button
                    class="quick-button"
                    onclick="loadExample('bracket')"
                >
                    Bracket
                </button>

                <button
                    class="quick-button"
                    onclick="resetChat()"
                >
                    Reset Chat
                </button>

            </div>

        </div>

    </div>


    <div
        id="reviewPanel"
        class="panel"
        style="display:none"
    >

        <div class="panel-header">

            <h2>
                Design Model
            </h2>

            <p>
                THETA interpreted your engineering request.
            </p>

        </div>

        <div id="reviewBody" class="result-body"></div>

    </div>


    <div
        id="resultsPanel"
        class="panel"
        style="display:none"
    >

        <div class="panel-header">

            <h2>
                Discovery Results
            </h2>

            <p>
                THETA searched the design space.
            </p>

        </div>

        <div
            id="resultsBody"
            class="result-body"
        ></div>

    </div>

</section>


<!-- ====================================================== -->
<!-- ADVANCED -->
<!-- ====================================================== -->

<section
    id="advancedPanel"
    class="advanced"
>

    <div class="panel">

        <div class="panel-header">

            <h2>
                Advanced Design Builder
            </h2>

            <p>
                Define variables, equations, constraints and objectives.
            </p>

        </div>


        <div class="builder-grid">

            <div class="builder-section">

                <h3>Variables</h3>

                <input
                    id="variableName"
                    class="builder-input"
                    placeholder="Name e.g. width"
                >

                <input
                    id="variableMin"
                    class="builder-input"
                    type="number"
                    placeholder="Minimum"
                >

                <input
                    id="variableMax"
                    class="builder-input"
                    type="number"
                    placeholder="Maximum"
                >

                <input
                    id="variableUnit"
                    class="builder-input"
                    placeholder="Unit"
                >

                <button
                    class="secondary-button"
                    onclick="addVariable()"
                >
                    Add Variable
                </button>

                <div id="variableList"></div>

            </div>


            <div class="builder-section">

                <h3>Equations</h3>

                <input
                    id="equationName"
                    class="builder-input"
                    placeholder="Result name"
                >

                <input
                    id="equationExpression"
                    class="builder-input"
                    placeholder="Expression e.g. width * height"
                >

                <input
                    id="equationUnit"
                    class="builder-input"
                    placeholder="Unit"
                >

                <button
                    class="secondary-button"
                    onclick="addEquation()"
                >
                    Add Equation
                </button>

                <div id="equationList"></div>

            </div>


            <div class="builder-section">

                <h3>Constraints</h3>

                <input
                    id="constraintExpression"
                    class="builder-input"
                    placeholder="Example: stress <= 200000000"
                >

                <button
                    class="secondary-button"
                    onclick="addConstraint()"
                >
                    Add Constraint
                </button>

                <div id="constraintList"></div>

            </div>


            <div class="builder-section">

                <h3>Objective</h3>

                <input
                    id="objectiveExpression"
                    class="builder-input"
                    placeholder="Example: mass"
                >

                <select
                    id="objectiveDirection"
                    class="builder-input"
                >

                    <option value="minimize">
                        Minimize
                    </option>

                    <option value="maximize">
                        Maximize
                    </option>

                </select>

                <button
                    class="secondary-button"
                    onclick="addObjective()"
                >
                    Add Objective
                </button>

                <div id="objectiveList"></div>

            </div>

        </div>


        <div
            style="
                padding:0 22px 22px;
                display:flex;
                gap:10px;
                flex-wrap:wrap;
            "
        >

            <button
                class="primary-button"
                onclick="runAdvanced()"
            >
                Run Discovery
            </button>

            <button
                class="secondary-button"
                onclick="loadAdvancedExample('beam')"
            >
                Load Beam
            </button>

            <button
                class="secondary-button"
                onclick="clearBuilder()"
            >
                Clear
            </button>

        </div>

    </div>


    <div
        id="advancedResultsPanel"
        class="panel"
        style="display:none"
    >

        <div class="panel-header">

            <h2>
                Advanced Results
            </h2>

        </div>

        <div
            id="advancedResults"
            class="result-body"
        ></div>

    </div>

</section>


<!-- ====================================================== -->
<!-- PRICING -->
<!-- ====================================================== -->

<section class="panel">

    <div class="panel-header">

        <h2>
            Unlock THETA
        </h2>

        <p>
            Start free. Upgrade when you need deeper design exploration.
        </p>

    </div>


    <div class="pricing-grid">


        <div class="price-card">

            <div class="eyebrow">
                Free
            </div>

            <div class="price">
                $0
            </div>

            <ul class="feature-list">

                <li>Basic design searches</li>
                <li>Beam designs</li>
                <li>Spring designs</li>
                <li>Drone designs</li>
                <li>Bracket designs</li>

            </ul>

            <button
                class="secondary-button"
                onclick="showMode('beginner')"
            >
                Try THETA
            </button>

        </div>


        <div class="price-card featured">

            <div class="eyebrow">
                THETA Pro
            </div>

            <div class="price">
                $9.99
                <span>/month</span>
            </div>

            <ul class="feature-list">

                <li>Unlimited searches</li>
                <li>Advanced optimization</li>
                <li>Design comparisons</li>
                <li>Parameter exploration</li>
                <li>Engineering reports</li>

            </ul>

            <button
                class="primary-button"
                onclick="upgrade('pro')"
            >
                Upgrade to Pro
            </button>

        </div>


        <div class="price-card">

            <div class="eyebrow">
                Engineer
            </div>

            <div class="price">
                $29.99
                <span>/month</span>
            </div>

            <ul class="feature-list">

                <li>Everything in Pro</li>
                <li>Large optimization searches</li>
                <li>Custom models</li>
                <li>Batch design exploration</li>
                <li>Advanced constraints</li>

            </ul>

            <button
                class="primary-button"
                onclick="upgrade('engineer')"
            >
                Upgrade to Engineer
            </button>

        </div>


    </div>

</section>


<div class="disclaimer">

    THETA is a preliminary engineering design exploration tool.
    Results are computational explorations and should be independently
    verified before being used in real-world, safety-critical,
    structural, aerospace, or other professional applications.

</div>


</main>


<footer class="footer">

    THETA TECHNOLOGY DISCOVERY ENGINE

    <br><br>

    Built for engineering exploration.

</footer>


<script>

let currentModel = null;
let currentOptimization = null;

const QUICK_EXAMPLES = {

    beam:
        "Design a lightweight beam that can hold 500 N.",

    spring:
        "Design a lightweight spring for 100 N.",

    drone:
        "Design a lightweight drone.",

    bracket:
        "Design a lightweight mounting bracket."

};


const EXAMPLE_PROMPTS = [

    "Design a lightweight beam that can hold 500 N.",

    "Design a lightweight beam that can hold 1000 N over 2 meters with a maximum stress of 200 MPa.",

    "Design a cantilever beam for a 750 N load over 1.5 meters.",

    "Design a lightweight beam that can hold 250 N.",

    "Design a lightweight spring for 100 N.",

    "Design a compact spring for 250 N.",

    "Design a lightweight spring for 50 N.",

    "Design a spring that can handle 500 N.",

    "Design a lightweight drone.",

    "Design a drone with a lightweight frame and high payload capacity.",

    "Design a compact quadcopter.",

    "Design a lightweight UAV.",

    "Design a lightweight mounting bracket.",

    "Design a thin mounting plate.",

    "Design a lightweight structural mount.",

    "Design a small mounting bracket."

];


function escapeHtml(value) {

    return String(value)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");

}


function formatNumber(value) {

    if (
        value === null ||
        value === undefined ||
        !Number.isFinite(Number(value))
    ) {
        return "—";
    }

    const number = Number(value);

    if (
        Math.abs(number) >= 1000000 ||
        (
            Math.abs(number) > 0 &&
            Math.abs(number) < 0.001
        )
    ) {
        return number.toExponential(3);
    }

    return number.toPrecision(6).replace(
        /0+$/,
        ""
    ).replace(
        /\.$/,
        ""
    );

}


function addMessage(sender, text) {

    const messages =
        document.getElementById("messages");

    const wrapper =
        document.createElement("div");

    wrapper.className =
        "message " +
        (
            sender === "user"
                ? "user"
                : "ai"
        );

    const label =
        document.createElement("div");

    label.className =
        "message-label";

    label.textContent =
        sender === "user"
            ? "YOU"
            : "THETA";

    const bubble =
        document.createElement("div");

    bubble.className =
        "message-bubble";

    bubble.innerHTML =
        escapeHtml(text);

    wrapper.appendChild(label);
    wrapper.appendChild(bubble);

    messages.appendChild(wrapper);

    messages.scrollTop =
        messages.scrollHeight;

}


function addExampleChoices(
    examples,
    heading = "Try another design"
) {

    const messages =
        document.getElementById("messages");

    const box =
        document.createElement("div");

    box.className =
        "example-box";

    const title =
        document.createElement("div");

    title.className =
        "example-title";

    title.textContent =
        heading;

    box.appendChild(title);

    examples.forEach(example => {

        const button =
            document.createElement("button");

        button.className =
            "example-choice";

        button.textContent =
            example;

        button.onclick = () => {

            document.getElementById(
                "chatInput"
            ).value = example;

            sendChat();

        };

        box.appendChild(button);

    });

    messages.appendChild(box);

}


function resetChat() {

    document.getElementById(
        "messages"
    ).innerHTML = "";

    document.getElementById(
        "chatInput"
    ).value = "";

    document.getElementById(
        "reviewPanel"
    ).style.display = "none";

    document.getElementById(
        "resultsPanel"
    ).style.display = "none";

    currentModel = null;
    currentOptimization = null;

    addMessage(
        "ai",
        "Welcome to THETA.\n\n" +
        "Describe an engineering problem and " +
        "I will translate it into a design model " +
        "and search the design space."
    );

    addExampleChoices(
        [
            "Design a lightweight beam that can hold 500 N.",
            "Design a lightweight spring for 100 N.",
            "Design a lightweight drone."
        ],
        "Start with an example"
    );

}


function handleChatKey(event) {

    if (event.key === "Enter") {
        event.preventDefault();
        sendChat();
    }

}


async function loadExample(name) {

    const text =
        QUICK_EXAMPLES[name];

    if (!text) {
        return;
    }

    showMode("beginner");

    document.getElementById(
        "chatInput"
    ).value = text;

    await sendChat();

}


function shuffleArray(array) {

    const copy =
        [...array];

    for (
        let i = copy.length - 1;
        i > 0;
        i--
    ) {

        const j =
            Math.floor(
                Math.random() * (i + 1)
            );

        [
            copy[i],
            copy[j]
        ] = [
            copy[j],
            copy[i]
        ];

    }

    return copy;

}


function getRandomExamples(count = 3) {

    return shuffleArray(
        EXAMPLE_PROMPTS
    ).slice(
        0,
        count
    );

}


async function sendChat() {

    const input =
        document.getElementById(
            "chatInput"
        );

    const text =
        input.value.trim();

    if (!text) {
        return;
    }

    addMessage(
        "user",
        text
    );

    input.value = "";

    addMessage(
        "ai",
        "Analyzing engineering problem..."
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
            document.getElementById(
                "messages"
            );

        const lastMessage =
            messages.lastElementChild;

        if (lastMessage) {
            lastMessage.remove();
        }

        if (!response.ok) {
            throw new Error(
                data.error ||
                "Could not interpret request."
            );
        }

        currentModel =
            data.model;

        addMessage(
            "ai",
            "I interpreted your request as " +
            currentModel.name +
            ".\n\n" +
            "I can now search the design space " +
            "for promising configurations."
        );

        renderReview();

        await optimizeCurrent();

    } catch (error) {

        const messages =
            document.getElementById(
                "messages"
            );

        const lastMessage =
            messages.lastElementChild;

        if (lastMessage) {
            lastMessage.remove();
        }

        addMessage(
            "ai",
            "Error: " +
            error.message
        );

    }

}


function renderReview() {

    if (!currentModel) {
        return;
    }

    const panel =
        document.getElementById(
            "reviewPanel"
        );

    const body =
        document.getElementById(
            "reviewBody"
        );

    panel.style.display =
        "block";

    let html = "";

    html += `
        <h3>
            ${escapeHtml(currentModel.name)}
        </h3>
    `;

    html += `
        <p style="color:#8e99a8">
            ${escapeHtml(
                currentModel.description
            )}
        </p>
    `;

    html += `
        <h4>Variables</h4>
    `;

    if (
        currentModel.variables.length === 0
    ) {

        html += `
            <p>No variables.</p>
        `;

    } else {

        html += `
            <table class="design-table">
                <thead>
                    <tr>
                        <th>Name</th>
                        <th>Minimum</th>
                        <th>Maximum</th>
                        <th>Unit</th>
                    </tr>
                </thead>
                <tbody>
        `;

        currentModel.variables.forEach(variable => {

            html += `
                <tr>
                    <td>${escapeHtml(
                        variable.name
                    )}</td>
                    <td>${formatNumber(
                        variable.min
                    )}</td>
                    <td>${formatNumber(
                        variable.max
                    )}</td>
                    <td>${escapeHtml(
                        variable.unit
                    )}</td>
                </tr>
            `;

        });

        html += `
                </tbody>
            </table>
        `;

    }

    html += `
        <h4>Constraints</h4>
    `;

    if (
        currentModel.constraints.length === 0
    ) {

        html += `
            <p style="color:#8e99a8">
                No explicit constraints.
            </p>
        `;

    } else {

        currentModel.constraints.forEach(
            constraint => {

                html += `
                    <div class="builder-item">
                        ${escapeHtml(
                            constraint.expression
                        )}
                    </div>
                `;

            }
        );

    }

    body.innerHTML =
        html;

}


async function optimizeCurrent() {

    if (!currentModel) {
        return;
    }

    const panel =
        document.getElementById(
            "resultsPanel"
        );

    const body =
        document.getElementById(
            "resultsBody"
        );

    panel.style.display =
        "block";

    body.innerHTML = `
        <p style="color:#8e99a8">
            THETA is searching hundreds of possible
            designs...
        </p>
    `;

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
                    body: JSON.stringify({
                        model:
                            currentModel,
                        mode:
                            "beginner"
                    })
                }
            );

        const data =
            await response.json();

        if (!response.ok) {
            throw new Error(
                data.error ||
                "Optimization failed."
            );
        }

        currentOptimization =
            data;

        renderResults(
            data,
            body
        );

    } catch (error) {

        body.innerHTML = `
            <div class="upgrade">
                <h3>
                    Optimization Error
                </h3>

                <p>
                    ${escapeHtml(
                        error.message
                    )}
                </p>
            </div>
        `;

    }

}


function renderResults(
    data,
    container
) {

    const best =
        data.best;

    let html = "";

    html += `
        <div class="results-grid">

            <div class="stat">

                <div class="stat-label">
                    Objective
                </div>

                <div class="stat-value">
                    ${formatNumber(
                        best.objective
                    )}
                </div>

            </div>

            <div class="stat">

                <div class="stat-label">
                    Constraint
                </div>

                <div class="stat-value ${
                    best.passed
                        ? "pass"
                        : "fail"
                }">

                    ${
                        best.passed
                            ? "PASS"
                            : "FAIL"
                    }

                </div>

            </div>

            <div class="stat">

                <div class="stat-label">
                    Generations
                </div>

                <div class="stat-value">
                    ${data.generations}
                </div>

            </div>

            <div class="stat">

                <div class="stat-label">
                    Designs Searched
                </div>

                <div class="stat-value">
                    ${data.population_size}
                </div>

            </div>

        </div>
    `;


    html += `
        <h3>
            Best Design
        </h3>
    `;

    html += `
        <table class="design-table">

            <thead>

                <tr>
                    <th>Parameter</th>
                    <th>Value</th>
                    <th>Unit</th>
                </tr>

            </thead>

            <tbody>
    `;

    currentModel.variables.forEach(
        variable => {

            html += `
                <tr>

                    <td>
                        ${escapeHtml(
                            variable.name
                        )}
                    </td>

                    <td>
                        ${formatNumber(
                            best.design[
                                variable.name
                            ]
                        )}
                    </td>

                    <td>
                        ${escapeHtml(
                            variable.unit
                        )}
                    </td>

                </tr>
            `;

        }
    );

    html += `
            </tbody>
        </table>
    `;


    html += `
        <h3>
            Calculated Results
        </h3>
    `;

    html += `
        <table class="design-table">

            <thead>

                <tr>
                    <th>Result</th>
                    <th>Value</th>
                    <th>Unit</th>
                </tr>

            </thead>

            <tbody>
    `;

    currentModel.equations.forEach(
        equation => {

            html += `
                <tr>

                    <td>
                        ${escapeHtml(
                            equation.name
                        )}
                    </td>

                    <td>
                        ${formatNumber(
                            best.results[
                                equation.name
                            ]
                        )}
                    </td>

                    <td>
                        ${escapeHtml(
                            equation.unit
                        )}
                    </td>

                </tr>
            `;

        }
    );

    html += `
            </tbody>
        </table>
    `;


    html += `
        <div class="upgrade">

            <h3>
                Want deeper design exploration?
            </h3>

            <p>
                THETA Pro unlocks larger searches,
                advanced optimization, design comparison,
                and engineering reports.
            </p>

            <button
                class="primary-button"
                onclick="upgrade('pro')"
            >
                Upgrade to THETA Pro — $9.99/month
            </button>

        </div>
    `;


    html += `
        <h3>
            Alternative Designs
        </h3>
    `;

    html += `
        <table class="design-table">

            <thead>

                <tr>
                    <th>#</th>
                    <th>Objective</th>
                    <th>Constraint</th>
                </tr>

            </thead>

            <tbody>
    `;

    data.alternatives.forEach(
        (result, index) => {

            html += `
                <tr>

                    <td>
                        ${index + 1}
                    </td>

                    <td>
                        ${formatNumber(
                            result.objective
                        )}
                    </td>

                    <td class="${
                        result.passed
                            ? "pass"
                            : "fail"
                    }">

                        ${
                            result.passed
                                ? "PASS"
                                : "FAIL"
                        }

                    </td>

                </tr>
            `;

        }
    );

    html += `
            </tbody>
        </table>
    `;


    container.innerHTML =
        html;

}


function showMode(mode) {

    const beginner =
        document.getElementById(
            "beginnerPanel"
        );

    const advanced =
        document.getElementById(
            "advancedPanel"
        );

    const beginnerButton =
        document.getElementById(
            "beginnerModeButton"
        );

    const advancedButton =
        document.getElementById(
            "advancedModeButton"
        );

    if (mode === "advanced") {

        beginner.style.display =
            "none";

        advanced.style.display =
            "block";

        beginnerButton.classList.remove(
            "active"
        );

        advancedButton.classList.add(
            "active"
        );

    } else {

        beginner.style.display =
            "block";

        advanced.style.display =
            "none";

        advancedButton.classList.remove(
            "active"
        );

        beginnerButton.classList.add(
            "active"
        );

    }

}


function upgrade(plan) {

    let url = "";

    if (plan === "pro") {

        url =
            "__PRO_PAYMENT_LINK__";

    }

    if (plan === "engineer") {

        url =
            "__ENGINEER_PAYMENT_LINK__";

    }

    if (
        !url ||
        url.includes("REPLACE_WITH_YOUR")
    ) {

        alert(
            "THETA payments are not connected yet. " +
            "Add your Stripe Payment Link before launch."
        );

        return;
    }

    window.location.href =
        url;

}


// ========================================================
// ADVANCED BUILDER
// ========================================================

let builderModel = {
    name: "Custom Technology Design",
    type: "custom",
    description: "Custom engineering model.",
    variables: [],
    equations: [],
    constraints: [],
    objectives: []
};


function addVariable() {

    const name =
        document.getElementById(
            "variableName"
        ).value.trim();

    const min =
        Number(
            document.getElementById(
                "variableMin"
            ).value
        );

    const max =
        Number(
            document.getElementById(
                "variableMax"
            ).value
        );

    const unit =
        document.getElementById(
            "variableUnit"
        ).value.trim();

    if (
        !name ||
        !Number.isFinite(min) ||
        !Number.isFinite(max) ||
        max <= min
    ) {

        alert(
            "Enter a valid variable name, minimum, and maximum."
        );

        return;
    }

    builderModel.variables.push({
        name: name,
        min: min,
        max: max,
        unit: unit
    });

    document.getElementById(
        "variableName"
    ).value = "";

    document.getElementById(
        "variableMin"
    ).value = "";

    document.getElementById(
        "variableMax"
    ).value = "";

    document.getElementById(
        "variableUnit"
    ).value = "";

    renderBuilderLists();

}


function addEquation() {

    const name =
        document.getElementById(
            "equationName"
        ).value.trim();

    const expression =
        document.getElementById(
            "equationExpression"
        ).value.trim();

    const unit =
        document.getElementById(
            "equationUnit"
        ).value.trim();

    if (
        !name ||
        !expression
    ) {

        alert(
            "Enter an equation name and expression."
        );

        return;
    }

    builderModel.equations.push({
        name: name,
        expression: expression,
        unit: unit
    });

    document.getElementById(
        "equationName"
    ).value = "";

    document.getElementById(
        "equationExpression"
    ).value = "";

    document.getElementById(
        "equationUnit"
    ).value = "";

    renderBuilderLists();

}


function addConstraint() {

    const expression =
        document.getElementById(
            "constraintExpression"
        ).value.trim();

    if (!expression) {

        alert(
            "Enter a constraint."
        );

        return;
    }

    builderModel.constraints.push({
        expression: expression
    });

    document.getElementById(
        "constraintExpression"
    ).value = "";

    renderBuilderLists();

}


function addObjective() {

    const expression =
        document.getElementById(
            "objectiveExpression"
        ).value.trim();

    const direction =
        document.getElementById(
            "objectiveDirection"
        ).value;

    if (!expression) {

        alert(
            "Enter an objective expression."
        );

        return;
    }

    builderModel.objectives = [
        {
            expression: expression,
            direction: direction
        }
    ];

    document.getElementById(
        "objectiveExpression"
    ).value = "";

    renderBuilderLists();

}


function removeVariable(index) {

    builderModel.variables.splice(
        index,
        1
    );

    renderBuilderLists();

}


function removeEquation(index) {

    builderModel.equations.splice(
        index,
        1
    );

    renderBuilderLists();

}


function removeConstraint(index) {

    builderModel.constraints.splice(
        index,
        1
    );

    renderBuilderLists();

}


function removeObjective(index) {

    builderModel.objectives.splice(
        index,
        1
    );

    renderBuilderLists();

}


function renderBuilderLists() {

    document.getElementById(
        "variableList"
    ).innerHTML =
        builderModel.variables
        .map(
            (item, index) => `
                <div class="builder-item">

                    <button
                        class="remove-button"
                        onclick="removeVariable(${index})"
                    >
                        ×
                    </button>

                    <strong>
                        ${escapeHtml(item.name)}
                    </strong>

                    <br>

                    ${formatNumber(item.min)}
                    →
                    ${formatNumber(item.max)}

                    ${escapeHtml(item.unit)}

                </div>
            `
        )
        .join("");


    document.getElementById(
        "equationList"
    ).innerHTML =
        builderModel.equations
        .map(
            (item, index) => `
                <div class="builder-item">

                    <button
                        class="remove-button"
                        onclick="removeEquation(${index})"
                    >
                        ×
                    </button>

                    <strong>
                        ${escapeHtml(item.name)}
                    </strong>

                    <br>

                    ${escapeHtml(
                        item.expression
                    )}

                </div>
            `
        )
        .join("");


    document.getElementById(
        "constraintList"
    ).innerHTML =
        builderModel.constraints
        .map(
            (item, index) => `
                <div class="builder-item">

                    <button
                        class="remove-button"
                        onclick="removeConstraint(${index})"
                    >
                        ×
                    </button>

                    ${escapeHtml(
                        item.expression
                    )}

                </div>
            `
        )
        .join("");


    document.getElementById(
        "objectiveList"
    ).innerHTML =
        builderModel.objectives
        .map(
            (item, index) => `
                <div class="builder-item">

                    <button
                        class="remove-button"
                        onclick="removeObjective(${index})"
                    >
                        ×
                    </button>

                    ${escapeHtml(
                        item.direction
                    )}

                    :

                    ${escapeHtml(
                        item.expression
                    )}

                </div>
            `
        )
        .join("");

}


function clearBuilder() {

    builderModel = {
        name: "Custom Technology Design",
        type: "custom",
        description: "Custom engineering model.",
        variables: [],
        equations: [],
        constraints: [],
        objectives: []
    };

    renderBuilderLists();

    document.getElementById(
        "advancedResultsPanel"
    ).style.display = "none";

}


function loadAdvancedExample(name) {

    if (name === "beam") {

        builderModel =
            JSON.parse(
                JSON.stringify(
                    beamExampleLocal()
                )
            );

    }

    renderBuilderLists();

}


function beamExampleLocal() {

    return {
        name: "Lightweight Beam",
        type: "beam",
        description:
            "Example cantilever beam.",
        variables: [
            {
                name: "b",
                min: 0.01,
                max: 0.20,
                unit: "m"
            },
            {
                name: "h",
                min: 0.01,
                max: 0.20,
                unit: "m"
            }
        ],
        equations: [
            {
                name: "moment",
                expression: "500 * 1",
                unit: "N*m"
            },
            {
                name: "stress",
                expression:
                    "(500 * 1) / (b * h^2 / 6)",
                unit: "Pa"
            },
            {
                name: "mass",
                expression:
                    "b * h * 1 * 7850",
                unit: "kg"
            }
        ],
        constraints: [
            "stress <= 200000000"
        ],
        objectives: [
            {
                expression: "mass",
                direction: "minimize"
            }
        ]
    };

}


async function runAdvanced() {

    if (
        builderModel.variables.length === 0
    ) {

        alert(
            "Add at least one variable."
        );

        return;
    }

    if (
        builderModel.equations.length === 0
    ) {

        alert(
            "Add at least one equation."
        );

        return;
    }

    if (
        builderModel.objectives.length === 0
    ) {

        alert(
            "Add an objective."
        );

        return;
    }

    const panel =
        document.getElementById(
            "advancedResultsPanel"
        );

    const body =
        document.getElementById(
            "advancedResults"
        );

    panel.style.display =
        "block";

    body.innerHTML = `
        <p style="color:#8e99a8">
            THETA is searching the custom design space...
        </p>
    `;

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
                    body: JSON.stringify({
                        model:
                            builderModel,
                        mode:
                            "advanced"
                    })
                }
            );

        const data =
            await response.json();

        if (!response.ok) {

            throw new Error(
                data.error ||
                "Optimization failed."
            );

        }

        currentModel =
            builderModel;

        currentOptimization =
            data;

        renderResults(
            data,
            body
        );

    } catch (error) {

        body.innerHTML = `
            <div class="upgrade">

                <h3>
                    Error
                </h3>

                <p>
                    ${escapeHtml(
                        error.message
                    )}
                </p>

            </div>
        `;

    }

}


// ========================================================
// STARTUP
// ========================================================

resetChat();

loadAdvancedExample("beam");

</script>

</body>
</html>
"""


# ============================================================
# HTTP SERVER
# ============================================================

class ThetaHandler(BaseHTTPRequestHandler):

    def log_message(self, format_string, *args):
        print(
            "%s - %s"
            % (
                self.address_string(),
                format_string % args
            )
        )

    def send_json(self, data, status=200):

        body = json.dumps(
            data,
            allow_nan=False
        ).encode("utf-8")

        self.send_response(status)

        self.send_header(
            "Content-Type",
            "application/json"
        )

        self.send_header(
            "Content-Length",
            str(len(body))
        )

        self.send_header(
            "Access-Control-Allow-Origin",
            "*"
        )

        self.end_headers()

        self.wfile.write(body)

    def send_html(self, html):

        body = html.encode("utf-8")

        self.send_response(200)

        self.send_header(
            "Content-Type",
            "text/html; charset=utf-8"
        )

        self.send_header(
            "Content-Length",
            str(len(body))
        )

        self.end_headers()

        self.wfile.write(body)

    def read_json(self):

        try:

            length = int(
                self.headers.get(
                    "Content-Length",
                    "0"
                )
            )

            raw = self.rfile.read(length)

            return json.loads(
                raw.decode("utf-8")
            )

        except Exception:

            return {}

    def do_OPTIONS(self):

        self.send_response(204)

        self.send_header(
            "Access-Control-Allow-Origin",
            "*"
        )

        self.send_header(
            "Access-Control-Allow-Methods",
            "GET, POST, OPTIONS"
        )

        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type"
        )

        self.end_headers()

    def do_GET(self):

        parsed = urlparse(
            self.path
        )

        path = parsed.path

        if path == "/":

            html = HTML.replace(
                "__PRO_PAYMENT_LINK__",
                PRO_PAYMENT_LINK
            )

            html = html.replace(
                "__ENGINEER_PAYMENT_LINK__",
                ENGINEER_PAYMENT_LINK
            )

            self.send_html(html)

            return

        if path == "/health":

            self.send_json({
                "status": "online",
                "engine": "THETA",
                "version": "1.0"
            })

            return

        if path == "/example":

            params = parse_qs(
                parsed.query
            )

            name = params.get(
                "name",
                ["beam"]
            )[0]

            if name == "spring":
                model = spring_example()

            elif name == "drone":
                model = drone_example()

            elif name == "bracket":
                model = bracket_example()

            else:
                model = beam_example()

            self.send_json({
                "model": model
            })

            return

        self.send_json(
            {
                "error": "Not found"
            },
            404
        )

    def do_POST(self):

        parsed = urlparse(
            self.path
        )

        path = parsed.path

        if path == "/interpret":

            data = self.read_json()

            text = str(
                data.get(
                    "text",
                    ""
                )
            ).strip()

            if not text:

                self.send_json(
                    {
                        "error":
                            "Engineering request is empty."
                    },
                    400
                )

                return

            try:

                model = interpret_engineering_request(
                    text
                )

                self.send_json({
                    "model": model
                })

            except Exception as error:

                self.send_json(
                    {
                        "error":
                            str(error)
                    },
                    500
                )

            return

        if path == "/optimize":

            data = self.read_json()

            model = data.get(
                "model"
            )

            if not model:

                self.send_json(
                    {
                        "error":
                            "No model supplied."
                    },
                    400
                )

                return

            try:

                model = normalize_model(model)

                mode = str(
                    data.get(
                        "mode",
                        "beginner"
                    )
                )

                if mode == "advanced":

                    population_size = 800
                    generations = 90

                else:

                    population_size = 500
                    generations = 60

                result = optimize_model(
                    model,
                    population_size=population_size,
                    generations=generations
                )

                self.send_json(
                    result
                )

            except Exception as error:

                self.send_json(
                    {
                        "error":
                            str(error)
                    },
                    500
                )

            return

        self.send_json(
            {
                "error":
                    "Endpoint not found."
            },
            404
        )


# ============================================================
# LOCAL BROWSER
# ============================================================

def open_browser():

    try:

        webbrowser.open(
            f"http://127.0.0.1:{PORT}"
        )

    except Exception:

        pass


# ============================================================
# MAIN
# ============================================================

def main():

    server = ThreadingHTTPServer(
        (HOST, PORT),
        ThetaHandler
    )

    print()
    print("=" * 72)
    print("THETA TECHNOLOGY DISCOVERY ENGINE")
    print("V1 - MONETIZABLE LAUNCH EDITION")
    print("=" * 72)
    print()
    print(
        f"THETA running on port {PORT}"
    )
    print(
        f"Local address: http://127.0.0.1:{PORT}"
    )
    print()
    print(
        "Engine: ONLINE"
    )
    print(
        "Optimization: ONLINE"
    )
    print(
        "Product interface: ONLINE"
    )
    print()
    print(
        "Press CTRL+C to stop."
    )
    print()

    # Render provides its own public URL.
    # Only open a browser when running locally.
    if not os.environ.get("RENDER"):
        threading.Timer(
            1.0,
            open_browser
        ).start()

    try:

        server.serve_forever()

    except KeyboardInterrupt:

        print()
        print(
            "THETA shutting down..."
        )

    finally:

        server.server_close()


if __name__ == "__main__":
    main()
