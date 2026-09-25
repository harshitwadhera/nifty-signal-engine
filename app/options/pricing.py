"""European Black-76 / Black-Scholes(q=0). ACT/365, theta/day, vega/1% vol."""
from math import erf, exp, isfinite, log, pi, sqrt


def cdf(x):
    return (1 + erf(x / sqrt(2))) / 2


def price_greeks(underlying, strike, years, rate, sigma, kind, model="black76"):
    if kind not in ("CE", "PE") or model not in ("black76", "black_scholes_q0"):
        raise ValueError("Unsupported pricing inputs")
    if not all(isfinite(x) for x in (underlying, strike, years, rate, sigma)) or min(underlying, strike, years, sigma) <= 0:
        raise ValueError("Invalid pricing inputs")
    forward = underlying if model == "black76" else underlying * exp(rate * years)
    discount = exp(-rate * years)
    d1 = (log(forward / strike) + sigma*sigma*years/2) / (sigma*sqrt(years))
    d2 = d1 - sigma*sqrt(years)
    density = exp(-d1*d1/2) / sqrt(2*pi)
    sign = 1 if kind == "CE" else -1
    price = discount * sign * (forward*cdf(sign*d1) - strike*cdf(sign*d2))
    if model == "black76":
        delta = discount * sign * cdf(sign*d1)
        gamma = discount * density / (forward*sigma*sqrt(years))
        theta = rate*price - discount*forward*density*sigma/(2*sqrt(years))
    else:
        delta = sign*cdf(sign*d1)
        gamma = density/(underlying*sigma*sqrt(years))
        theta = -underlying*density*sigma/(2*sqrt(years)) - sign*rate*strike*discount*cdf(sign*d2)
    vega = discount*forward*density*sqrt(years)
    return {"price": price, "delta": delta, "gamma": gamma, "theta": theta/365, "vega": vega/100}


def implied_greeks(price, underlying, strike, years, rate, kind, model):
    empty = {"iv": None, "delta": None, "gamma": None, "theta": None, "vega": None}
    try:
        if not all(isinstance(x, (int, float)) and not isinstance(x, bool) and isfinite(x) for x in (price, underlying, strike, years, rate)) or price <= 0:
            return empty
        low, high = 0.0001, 5.0
        lower = price_greeks(underlying, strike, years, rate, low, kind, model)["price"]
        upper = price_greeks(underlying, strike, years, rate, high, kind, model)["price"]
        if not lower < price < upper:
            return empty
        for _ in range(100):
            mid = (low+high)/2
            value = price_greeks(underlying, strike, years, rate, mid, kind, model)["price"]
            if value < price:
                low = mid
            else:
                high = mid
        sigma = (low+high)/2
        result = price_greeks(underlying, strike, years, rate, sigma, kind, model)
        return {"iv": sigma, **{k: result[k] for k in empty if k != "iv"}}
    except (ValueError, OverflowError, ZeroDivisionError):
        return empty
