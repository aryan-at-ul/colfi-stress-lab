from __future__ import annotations

import csv
import hashlib
import io
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, time as dt_time, timedelta, timezone
from typing import Callable

from .catalog import KNOWN_EVENTS
from .models import AssessmentRequest, EvidenceItem


USER_AGENT = "COLFI-Stress-Lab/2.0 research@example.invalid"

INSTRUMENTS = {
    "sp500": {"label": "S&P 500", "source": "yahoo", "symbol": "^GSPC", "unit": "index"},
    "nasdaq": {"label": "Nasdaq 100", "source": "yahoo", "symbol": "^NDX", "unit": "index"},
    "nikkei": {"label": "Nikkei 225", "source": "yahoo", "symbol": "^N225", "unit": "index"},
    "eurostoxx": {"label": "STOXX 50", "source": "yahoo", "symbol": "^STOXX50E", "unit": "index"},
    "us_banks": {"label": "State Street SPDR S&P Regional Banking ETF", "source": "yahoo", "symbol": "KRE", "unit": "USD"},
    "vix": {"label": "Cboe Volatility Index", "source": "cboe", "symbol": "VIX", "unit": "index"},
    "eurusd": {"label": "EUR/USD reference rate", "source": "ecb", "symbol": "USD", "unit": "USD per EUR"},
    "eurjpy": {"label": "EUR/JPY reference rate", "source": "ecb", "symbol": "JPY", "unit": "JPY per EUR"},
    "eurgbp": {"label": "EUR/GBP reference rate", "source": "ecb", "symbol": "GBP", "unit": "GBP per EUR"},
    "gbpusd": {"label": "GBP/USD derived ECB reference rate", "source": "ecb", "symbol": "GBPUSD", "unit": "USD per GBP"},
}

SOURCE_CATALOG = {
    "yahoo": {
        "label": "Yahoo Finance delayed charts",
        "kind": "market",
        "credential": "none",
        "note": "Key-free, unofficial chart endpoint; delayed data.", "default": True,
    },
    "cboe": {
        "label": "Cboe VIX daily history",
        "kind": "market",
        "credential": "none",
        "note": "Official Cboe daily VIX CSV.", "default": True,
    },
    "ecb": {
        "label": "ECB exchange-rate API",
        "kind": "market",
        "credential": "none",
        "note": "Official ECB SDMX REST API.", "default": True,
    },
    "google_reuters": {
        "label": "Reuters results from Google News RSS",
        "kind": "news",
        "credential": "none",
        "note": "Key-free Reuters-domain search used by WorldMonitor's finance feed.",
        "default": True,
    },
    "official_event": {
        "label": "Official event reference",
        "kind": "reference",
        "credential": "none",
        "note": "For known cases, fetch and hash the linked BIS, central-bank or resolution-authority source.",
        "default": True,
    },
}


class SourceError(RuntimeError):
    pass


def _fetch(url: str, accept: str = "application/json") -> tuple[bytes, str]:
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read(300).decode(errors="replace")
        raise SourceError(f"HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SourceError(f"{type(exc).__name__}: {exc}") from exc
    return body, hashlib.sha256(body).hexdigest()


def _iso(timestamp: float | int) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")


def _period(window_date: date) -> int:
    return int(datetime.combine(window_date, dt_time.min, timezone.utc).timestamp())


def _dated_observations(points: list[tuple[date, float, float | None, str]]) -> list[dict]:
    """Return the complete, ordered path with point-to-point changes.

    ``points`` must include the pre-window reference as its first member. The
    reference is retained so an auditor can reproduce every displayed return.
    """
    output: list[dict] = []
    prior: float | None = None
    for day, value, volume, observed_at in points:
        change = None if prior in {None, 0.0} else (value / prior - 1.0) * 100.0
        output.append({
            "session_date": day,
            "observed_at": observed_at,
            "value": value,
            "daily_change_pct": change,
            "volume": volume,
        })
        prior = value
    return output


def _path_stats(observations: list[dict]) -> tuple[float | None, float | None]:
    changes = [
        float(item["daily_change_pct"])
        for item in observations[1:]
        if item.get("daily_change_pct") is not None
    ]
    return (min(changes), max(changes)) if changes else (None, None)


def _yahoo(item_id: str, meta: dict, start: date, end: date) -> EvidenceItem:
    symbol = meta["symbol"]
    query = urllib.parse.urlencode({
        "period1": _period(start - timedelta(days=40)),
        "period2": _period(end + timedelta(days=2)),
        "interval": "1d",
        "events": "history",
    })
    encoded = urllib.parse.quote(symbol, safe="")
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded}?{query}"
    body, digest = _fetch(url)
    payload = json.loads(body)
    result = payload.get("chart", {}).get("result") or []
    if not result:
        raise SourceError(payload.get("chart", {}).get("error") or "empty Yahoo response")
    series = result[0]
    stamps = series.get("timestamp") or []
    quote = (series.get("indicators", {}).get("quote") or [{}])[0]
    closes = quote.get("close") or []
    volumes = quote.get("volume") or []
    if len(volumes) < len(stamps):
        volumes = list(volumes) + [None] * (len(stamps) - len(volumes))
    points = [
        (datetime.fromtimestamp(ts, timezone.utc).date(), ts, float(close), volume)
        for ts, close, volume in zip(stamps, closes, volumes)
        if close is not None
    ]
    points.sort(key=lambda point: point[0])
    selected = [point for point in points if start <= point[0] <= end]
    if not selected:
        raise SourceError(f"no {symbol} observation in selected window")
    current = selected[-1]
    earlier = [point for point in points if point[0] < selected[0][0]]
    if not earlier:
        raise SourceError(f"no prior {symbol} close available for change calculation")
    previous = earlier[-1]
    change = (current[2] / previous[2] - 1.0) * 100.0
    path = [previous] + selected
    observations = _dated_observations([
        (day, value, float(volume) if volume is not None else None, _iso(stamp))
        for day, stamp, value, volume in path
    ])
    minimum, maximum = _path_stats(observations)
    human_query = urllib.parse.urlencode({
        "period1": _period(start),
        "period2": _period(end + timedelta(days=1)),
        "interval": "1d",
        "filter": "history",
        "frequency": "1d",
    })
    return EvidenceItem(
        id=f"MKT-{item_id.upper()}", kind="market", source="Yahoo Finance",
        title=meta["label"], symbol=symbol, value=current[2],
        previous_value=previous[2], change_pct=change, window_change_pct=change,
        reference_date=previous[0], window_start_date=selected[0][0],
        window_end_date=current[0], min_daily_change_pct=minimum,
        max_daily_change_pct=maximum, observations=observations, unit=meta["unit"],
        observed_at=_iso(current[1]), fetched_at=_iso(time.time()),
        source_url=f"https://finance.yahoo.com/quote/{encoded}/history/?{human_query}",
        acquisition_url=url, raw_sha256=digest,
        summary=(
            f"Full selected path: {selected[0][0]} to {current[0]}; window return "
            f"{change:.4f}% from the pre-window close on {previous[0]}; "
            f"daily range {minimum:.4f}% to {maximum:.4f}%."
        ),
    )


def _cboe(start: date, end: date) -> EvidenceItem:
    url = "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"
    body, digest = _fetch(url, "text/csv")
    rows = list(csv.DictReader(io.StringIO(body.decode("utf-8-sig"))))
    points = []
    for row in rows:
        try:
            day = datetime.strptime(row["DATE"].strip(), "%m/%d/%Y").date()
            points.append((day, row))
        except (KeyError, ValueError):
            continue
    points.sort(key=lambda point: point[0])
    selected = [point for point in points if start <= point[0] <= end]
    if not selected:
        raise SourceError("no Cboe VIX observation in selected window")
    current_day, current = selected[-1]
    earlier = [point for point in points if point[0] < selected[0][0]]
    if not earlier:
        raise SourceError("no prior VIX close available")
    reference_day, reference = earlier[-1]
    previous = float(reference["CLOSE"])
    value = float(current["CLOSE"])
    change = (value / previous - 1.0) * 100.0
    path = [(reference_day, reference)] + selected
    observations = _dated_observations([
        (day, float(row["CLOSE"]), None, f"{day.isoformat()}T21:00:00Z")
        for day, row in path
    ])
    minimum, maximum = _path_stats(observations)
    return EvidenceItem(
        id="MKT-VIX", kind="market", source="Cboe Global Markets",
        title="Cboe Volatility Index", symbol="VIX", value=value,
        previous_value=previous, change_pct=change, window_change_pct=change,
        reference_date=reference_day, window_start_date=selected[0][0],
        window_end_date=current_day, min_daily_change_pct=minimum,
        max_daily_change_pct=maximum, observations=observations, unit="index",
        observed_at=f"{current_day.isoformat()}T21:00:00Z",
        fetched_at=_iso(time.time()), source_url=url, acquisition_url=url,
        raw_sha256=digest,
        summary=(
            f"Full selected path: {selected[0][0]} to {current_day}; window change "
            f"{change:.4f}% from the pre-window close on {reference_day}; "
            f"final-session high {float(current['HIGH']):.4f}."
        ),
    )


def _ecb(selected_ids: list[str], start: date, end: date) -> list[EvidenceItem]:
    currencies = list(dict.fromkeys(
        currency
        for item in selected_ids
        for currency in (
            ["USD", "GBP"] if item == "gbpusd"
            else [INSTRUMENTS[item]["symbol"]]
        )
    ))
    key = "+".join(currencies)
    params = urllib.parse.urlencode({
        "startPeriod": (start - timedelta(days=40)).isoformat(),
        "endPeriod": end.isoformat(), "format": "csvdata", "detail": "dataonly",
    })
    url = f"https://data-api.ecb.europa.eu/service/data/EXR/D.{key}.EUR.SP00.A?{params}"
    body, digest = _fetch(url, "text/csv")
    rows = list(csv.DictReader(io.StringIO(body.decode("utf-8-sig"))))
    by_currency: dict[str, list[tuple[date, float]]] = {
        currency: [] for currency in currencies
    }
    for row in rows:
        row_currency = row.get("CURRENCY") or row.get("KEY", "").split(".")[1:2]
        if isinstance(row_currency, list):
            row_currency = row_currency[0] if row_currency else ""
        if row_currency not in by_currency:
            continue
        try:
            by_currency[row_currency].append((
                date.fromisoformat(row["TIME_PERIOD"]), float(row["OBS_VALUE"])
            ))
        except (KeyError, ValueError, TypeError):
            continue
    for values in by_currency.values():
        values.sort()

    output = []
    for item_id in selected_ids:
        meta = INSTRUMENTS[item_id]
        if item_id == "gbpusd":
            usd = dict(by_currency.get("USD", []))
            gbp = dict(by_currency.get("GBP", []))
            currency_rows = [
                (day, usd[day] / gbp[day])
                for day in sorted(set(usd) & set(gbp))
                if gbp[day] != 0
            ]
            evidence_symbol = meta["symbol"]
            source_series = "USD and GBP EUR reference rates"
        else:
            currency = meta["symbol"]
            currency_rows = by_currency.get(currency, [])
            evidence_symbol = f"EUR{currency}"
            source_series = f"{currency} EUR reference rate"
        in_window = [point for point in currency_rows if start <= point[0] <= end]
        if not in_window:
            raise SourceError(f"no ECB {source_series} observation in selected window")
        current = in_window[-1]
        earlier = [point for point in currency_rows if point[0] < in_window[0][0]]
        if not earlier:
            raise SourceError(f"no prior ECB {currency} rate available")
        previous = earlier[-1]
        path = [previous] + in_window
        observations = _dated_observations([
            (day, value, None, f"{day.isoformat()}T15:00:00Z")
            for day, value in path
        ])
        minimum, maximum = _path_stats(observations)
        source_params = urllib.parse.urlencode({
            "startPeriod": start.isoformat(),
            "endPeriod": end.isoformat(),
        })
        change = (current[1] / previous[1] - 1) * 100
        output.append(EvidenceItem(
            id=f"MKT-{item_id.upper()}", kind="market", source="European Central Bank",
            title=meta["label"], symbol=evidence_symbol, value=current[1],
            previous_value=previous[1], change_pct=change,
            window_change_pct=change,
            reference_date=previous[0], window_start_date=in_window[0][0],
            window_end_date=current[0], min_daily_change_pct=minimum,
            max_daily_change_pct=maximum, observations=observations,
            unit=meta["unit"], observed_at=f"{current[0].isoformat()}T15:00:00Z",
            fetched_at=_iso(time.time()),
            source_url=(
                "https://data.ecb.europa.eu/data/datasets/EXR/"
                f"EXR.D.{'+'.join(['USD', 'GBP'] if item_id == 'gbpusd' else [meta['symbol']])}.EUR.SP00.A?{source_params}"
            ),
            acquisition_url=url, raw_sha256=digest,
            summary=(
                f"Full selected path: {in_window[0][0]} to {current[0]}; window change "
                f"{change:.4f}% from the pre-window reference on {previous[0]}."
                + (
                    " USD per GBP is derived as the matched-date ECB USD-per-EUR "
                    "rate divided by the ECB GBP-per-EUR rate."
                    if item_id == "gbpusd" else ""
                )
            ),
        ))
    return output


def _google_reuters(query_text: str, start: date, end: date) -> list[EvidenceItem]:
    # Google News treats ``before`` as exclusive, so advance it one day to
    # include the selected end date.
    query = (f"({query_text}) site:reuters.com after:{start.isoformat()} "
             f"before:{(end + timedelta(days=1)).isoformat()}")
    params = urllib.parse.urlencode({"q": query, "hl": "en-US", "gl": "US",
                                     "ceid": "US:en"})
    url = f"https://news.google.com/rss/search?{params}"
    body, digest = _fetch(url, "application/rss+xml")
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise SourceError("Google News returned invalid RSS") from exc
    output = []
    for index, node in enumerate(root.findall(".//item"), 1):
        title = (node.findtext("title") or "").strip()
        link = (node.findtext("link") or "").strip()
        published = (node.findtext("pubDate") or "").strip()
        if not title or not link:
            continue
        try:
            from email.utils import parsedate_to_datetime
            stamp = parsedate_to_datetime(published).astimezone(timezone.utc)
            if not start <= stamp.date() <= end:
                continue
            observed = stamp.isoformat().replace("+00:00", "Z")
        except (TypeError, ValueError):
            observed = f"{end.isoformat()}T23:59:59Z"
        output.append(EvidenceItem(
            id=f"GNEWS-{index:03d}", kind="news",
            source="Reuters result via Google News RSS", title=title,
            observed_at=observed, source_url=link, acquisition_url=url,
            fetched_at=_iso(time.time()), raw_sha256=digest,
            summary="Reuters-domain search result returned by Google News RSS.",
        ))
    if not output:
        raise SourceError("Google News returned no Reuters results for the selected query and window")
    return output


def _official_event(event_id: str, end: date) -> EvidenceItem:
    event = KNOWN_EVENTS.get(event_id)
    if not event:
        raise SourceError("official event evidence is available only for a known event")
    reference = event["reference"]
    body, digest = _fetch(reference["url"], "text/html,application/pdf")
    return EvidenceItem(
        id=f"REF-{event_id.upper().replace('_', '-')}",
        kind="official_reference",
        source=reference["publisher"],
        title=reference["title"],
        observed_at=f"{end.isoformat()}T23:59:59Z",
        source_url=reference["url"],
        acquisition_url=reference["url"],
        fetched_at=_iso(time.time()),
        raw_sha256=digest,
        summary=reference["summary"],
    )


def collect(request: AssessmentRequest,
            emit: Callable[[str, str, dict], None]) -> tuple[list[dict], list[dict]]:
    evidence: list[EvidenceItem] = []
    failures: list[dict] = []

    def attempt(source: str, label: str, operation):
        emit("source_started", f"Fetching {label}", {"source": source})
        try:
            items = operation()
            if isinstance(items, EvidenceItem):
                items = [items]
            evidence.extend(items)
            emit("source_complete", f"Captured {len(items)} item(s) from {label}",
                 {"source": source, "evidence_ids": [item.id for item in items]})
        except Exception as exc:  # isolate connector failures; never invent replacement data
            failure = {"source": source, "error": f"{type(exc).__name__}: {exc}"}
            failures.append(failure)
            emit("source_failed", f"{label} failed: {exc}", failure)

    selected = set(request.instruments)
    if "yahoo" in request.sources:
        for item_id in request.instruments:
            meta = INSTRUMENTS.get(item_id)
            if meta and meta["source"] == "yahoo":
                attempt("yahoo", meta["label"],
                        lambda item_id=item_id, meta=meta: _yahoo(
                            item_id, meta, request.start_date, request.end_date))
    if "cboe" in request.sources and "vix" in selected:
        attempt("cboe", "Cboe VIX history",
                lambda: _cboe(request.start_date, request.end_date))
    if "ecb" in request.sources:
        fx = [item for item in request.instruments
              if item in INSTRUMENTS and INSTRUMENTS[item]["source"] == "ecb"]
        if fx:
            attempt("ecb", "ECB exchange rates",
                    lambda: _ecb(fx, request.start_date, request.end_date))
    if "google_reuters" in request.sources:
        attempt("google_reuters", "Reuters news through Google News RSS",
                lambda: _google_reuters(request.news_query, request.start_date,
                                        request.end_date))
    if "official_event" in request.sources and request.event_id != "custom":
        attempt("official_event", "official event reference",
                lambda: _official_event(request.event_id, request.end_date))
    # The collector boundary is a persisted/API contract. Return JSON-native
    # values immediately so direct execution and replay behave exactly like a
    # database round trip.
    return [item.model_dump(mode="json") for item in evidence], failures


def verify_evidence_snapshot(request: AssessmentRequest,
                             evidence: list[dict]) -> dict:
    """Apply release-blocking structural checks to captured evidence."""
    checks: list[dict] = []

    def check(name: str, passed: bool, detail: str):
        checks.append({"name": name, "passed": bool(passed), "detail": detail})

    by_id = {item["id"]: item for item in evidence}
    required_market = {
        f"MKT-{item_id.upper()}" for item_id in request.instruments
        if INSTRUMENTS.get(item_id, {}).get("source") in {"yahoo", "cboe", "ecb"}
    }
    missing = sorted(required_market - set(by_id))
    check("required_market_coverage", not missing,
          "All required market series captured." if not missing
          else f"Missing required series: {missing}")

    market = [item for item in evidence if item.get("kind") == "market"]
    malformed = []
    for item in market:
        observations = item.get("observations") or []
        dates = [date.fromisoformat(str(point["session_date"])) for point in observations]
        reference = item.get("reference_date")
        window_points = [day for day in dates if request.start_date <= day <= request.end_date]
        if (
            len(observations) < 2
            or not reference
            or date.fromisoformat(str(reference)) >= request.start_date
            or not window_points
            or dates != sorted(dates)
        ):
            malformed.append(item["id"])
    check("dated_path_integrity", not malformed,
          "Every market item contains an ordered pre-window reference and full in-window path."
          if not malformed else f"Invalid dated paths: {malformed}")

    if "official_event" in request.sources and request.event_id != "custom":
        official = any(item.get("kind") == "official_reference" for item in evidence)
        check("official_reference", official,
              "Known-event first-party reference captured." if official
              else "Known-event first-party reference missing.")

    return {
        "passed": all(item["passed"] for item in checks),
        "checks": checks,
        "contract_version": "dated-evidence-v1",
    }
