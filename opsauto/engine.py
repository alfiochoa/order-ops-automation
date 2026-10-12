"""Enrichment (normalisation, customer lookup, risk score) and YAML rule evaluation."""
import re

from .models import REF, Config

COUNTRIES = {
    "usa": "US", "united states": "US", "u.s.": "US", "us": "US", "uk": "GB", "united kingdom": "GB",
    "england": "GB", "great britain": "GB", "españa": "ES", "spain": "ES", "germany": "DE",
    "deutschland": "DE", "france": "FR", "mexico": "MX", "méxico": "MX", "nigeria": "NG",
    "portugal": "PT", "italy": "IT", "italia": "IT", "netherlands": "NL", "canada": "CA",
}
FREE = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "proton.me", "aol.com"}


def norm_country(raw: str) -> str:
    s = raw.strip().lower().rstrip(".")
    if s in COUNTRIES:
        return COUNTRIES[s]
    return s.upper() if re.fullmatch(r"[a-z]{2}", s) else "ZZ"  # ZZ = unknown


def norm_address(a) -> dict:
    return {"line1": " ".join(a.line1.split()), "city": a.city.strip().title(),
            "postal": re.sub(r"\s+", "", a.postal).upper(), "country": norm_country(a.country)}


def enrich(kind: str, model, store, cfg: Config) -> dict:
    email = model.email.strip().lower()
    ctx = {"kind": kind, "ref": getattr(model, REF[kind]), "email": email,
           "free_email": email.split("@")[1] in FREE}
    if kind != "order":
        ctx["text"] = " ".join([getattr(model, "subject", ""), getattr(model, "body", ""),
                                getattr(model, "message", "")]).lower()
        return ctx
    bill, ship = norm_address(model.billing), norm_address(model.shipping)
    cust = store.customer(email) or {"orders": 0, "total_spent": 0, "flagged_orders": 0}
    total = model.total
    ctx.update(
        total=total, currency=model.currency.upper(), payment_method=model.payment_method,
        billing=bill, shipping=ship, country=ship["country"], cod=model.payment_method == "cod",
        country_mismatch=bill["country"] != ship["country"],
        high_value=total >= cfg.risk.high_value_threshold,
        new_customer=cust["orders"] == 0, customer_orders=cust["orders"],
        high_risk_country=ship["country"] in cfg.risk.high_risk_countries,
        prior_flagged=cust["flagged_orders"] > 0,
        vip=cust["orders"] >= cfg.vip.min_orders or cust["total_spent"] >= cfg.vip.min_spent)
    score = sum(w for name, w in cfg.risk.weights.items() if ctx.get(name))
    ctx["risk_score"] = max(0, min(100, score))
    return ctx


OPS = {
    "gt": lambda a, b: a > b, "gte": lambda a, b: a >= b, "lt": lambda a, b: a < b,
    "lte": lambda a, b: a <= b, "eq": lambda a, b: a == b, "ne": lambda a, b: a != b,
    "in": lambda a, b: a in b, "contains": lambda a, b: any(k in a for k in b),
}


def evaluate(cfg: Config, kind: str, ctx: dict):
    """Return the rules whose conditions all hold. Missing fields never match."""
    hits = []
    for r in cfg.rules:
        if kind in r.kinds and all(c.field in ctx and OPS[c.op](ctx[c.field], c.value) for c in r.all):
            hits.append(r)
    return hits
