"""Versioned inputs used to construct real evaluation runs.

The catalog contains no model responses or assessment results. It is the
equivalent of an evaluation dataset: declared sandbox portfolios and known
historical event windows whose evidence is fetched again for every run.
"""
from __future__ import annotations


TAXONOMY = [
    "Bank run",
    "Equity market crisis",
    "FX/currency stress",
    "No material stress",
]


# Permitted synthetic hedge identifiers and the declared holding they hedge.
# Keeping this mapping explicit prevents free-form instruments from passing the
# portfolio-alignment gate merely because their names happen to share a token.
HEDGE_LINKS = {
    "sp500_put_options": "sp500",
    "nasdaq_put_options": "nasdaq",
    "nikkei_put_options": "nikkei",
    "eurostoxx_put_options": "eurostoxx",
    "us_banks_put_options": "us_banks",
}


INSTITUTIONS = {
    "INST-01": {
        "name": "Institution 1",
        "profile": "US broad-equity mandate",
        "institution_type": "Long-only equity manager",
        "base_currency": "USD",
        "portfolio": [{"asset_id": "sp500", "weight": 1.0}],
        "objective": "Protect portfolio value while avoiding unnecessary turnover.",
        "constraints": [
            "Remain fully invested unless the evidence supports de-risking.",
            "Explain any departure from the strategic S&P 500 mandate.",
        ],
    },
    "INST-02": {
        "name": "Institution 2",
        "profile": "US technology-equity mandate",
        "institution_type": "Growth equity manager",
        "base_currency": "USD",
        "portfolio": [{"asset_id": "nasdaq", "weight": 1.0}],
        "objective": "Limit drawdown while preserving long-horizon technology exposure.",
        "constraints": [
            "Avoid unnecessary crystallisation of short-lived volatility.",
            "Any sale must identify the Nasdaq exposure being reduced.",
        ],
    },
    "INST-03": {
        "name": "Institution 3",
        "profile": "Japan equity mandate",
        "institution_type": "Regional equity manager",
        "base_currency": "JPY",
        "portfolio": [{"asset_id": "nikkei", "weight": 1.0}],
        "objective": "Control drawdown while maintaining the Japanese equity mandate.",
        "constraints": [
            "Keep recommendations specific to the Nikkei exposure.",
            "Consider volatility and currency evidence before changing exposure.",
        ],
    },
    "INST-04": {
        "name": "Institution 4",
        "profile": "Euro-area equity mandate",
        "institution_type": "Regional equity manager",
        "base_currency": "EUR",
        "portfolio": [{"asset_id": "eurostoxx", "weight": 1.0}],
        "objective": "Preserve the strategic European allocation within risk limits.",
        "constraints": [
            "Keep recommendations specific to STOXX 50 exposure.",
            "Do not infer liquidity or FX conditions that are absent from evidence.",
        ],
    },
    "INST-05": {
        "name": "Institution 5",
        "profile": "Diversified US equity mandate",
        "institution_type": "Multi-index equity manager",
        "base_currency": "USD",
        "portfolio": [
            {"asset_id": "sp500", "weight": 0.5},
            {"asset_id": "nasdaq", "weight": 0.5},
        ],
        "objective": "Control portfolio drawdown while preserving diversification.",
        "constraints": [
            "Treat both holdings as one portfolio rather than isolated trades.",
            "Explain changes to the 50/50 strategic allocation.",
        ],
    },
    "INST-06": {
        "name": "Institution 6",
        "profile": "Diversified Japan/Europe mandate",
        "institution_type": "Cross-regional equity manager",
        "base_currency": "EUR",
        "portfolio": [
            {"asset_id": "nikkei", "weight": 0.5},
            {"asset_id": "eurostoxx", "weight": 0.5},
        ],
        "objective": "Limit cross-regional drawdown without abandoning diversification.",
        "constraints": [
            "Treat both regional holdings as a single 50/50 portfolio.",
            "Account for material regional differences visible in the evidence.",
        ],
    },
    "INST-07": {
        "name": "Institution 7",
        "profile": "Global four-index mandate",
        "institution_type": "Global equity allocator",
        "base_currency": "USD",
        "portfolio": [
            {"asset_id": "sp500", "weight": 0.25},
            {"asset_id": "nasdaq", "weight": 0.25},
            {"asset_id": "nikkei", "weight": 0.25},
            {"asset_id": "eurostoxx", "weight": 0.25},
        ],
        "objective": "Preserve global diversification while controlling portfolio-wide risk.",
        "constraints": [
            "Assess the four holdings together and avoid unsupported contagion claims.",
            "Explain any deviation from equal regional weights.",
        ],
    },
}

for _institution in INSTITUTIONS.values():
    _institution["portfolio_source"] = "User-supplied COLFI hackathon specification"
    _institution["data_class"] = "declared sandbox input — not actual firm data"


KNOWN_EVENTS = {
    "august_2024_turmoil": {
        "label": "5 Aug 2024 — global equity turmoil",
        "description": "Japan-led global equity sell-off and yen carry-trade unwind.",
        "start_date": "2024-08-02",
        "end_date": "2024-08-05",
        "news_query": "August 5 2024 global equity selloff yen carry trade",
        "instruments": ["sp500", "nasdaq", "nikkei", "eurostoxx", "vix", "eurjpy"],
        "expected_taxonomy": "Equity market crisis",
        "reference": {
            "publisher": "Bank for International Settlements",
            "title": "The market turbulence and carry trade unwind of August 2024",
            "url": "https://www.bis.org/publications/bulletin-90-market-turbulence-and-carry-trade-unwind-august-2024",
            "summary": "BIS records that stress peaked on 5 August, with a 12% TOPIX fall, a VIX spike and leveraged carry-trade deleveraging.",
        },
    },
    "svb_2023_run": {
        "label": "10 Mar 2023 — Silicon Valley Bank run",
        "description": "Rapid deposit outflows, funding shortfall and bank closure.",
        "start_date": "2023-03-08",
        "end_date": "2023-03-13",
        "news_query": "Silicon Valley Bank deposit run funding shortfall March 2023",
        "instruments": ["sp500", "us_banks", "vix"],
        "portfolio_overrides": {
            "INST-01": [
                {"asset_id": "sp500", "weight_pct": 50.0},
                {"asset_id": "us_banks", "weight_pct": 50.0},
            ],
            "INST-05": [
                {"asset_id": "sp500", "weight_pct": 25.0},
                {"asset_id": "nasdaq", "weight_pct": 25.0},
                {"asset_id": "us_banks", "weight_pct": 50.0},
            ],
        },
        "expected_taxonomy": "Bank run",
        "reference": {
            "publisher": "Federal Deposit Insurance Corporation",
            "title": "Failed Bank Information for Silicon Valley Bank",
            "url": "https://www.fdic.gov/resources/resolutions/bank-failures/failed-bank-list/silicon-valley.html",
            "summary": "The FDIC records Silicon Valley Bank's closure on 10 March 2023 and its transfer into an FDIC bridge bank.",
        },
    },
    "uk_2022_sterling": {
        "label": "23–28 Sep 2022 — sterling and gilt stress",
        "description": "Sterling depreciation and severe dysfunction in long-dated UK gilts.",
        "start_date": "2022-09-23",
        "end_date": "2022-09-28",
        "news_query": "UK sterling gilt market stress September 2022",
        "instruments": ["sp500", "eurostoxx", "vix", "eurgbp", "gbpusd"],
        "expected_taxonomy": "FX/currency stress",
        "reference": {
            "publisher": "Bank of England",
            "title": "Financial Policy Summary and Record — October 2022",
            "url": "https://www.bankofengland.co.uk/-/media/boe/files/financial-policy-summary-and-record/2022/fpc-summary-and-record-october-2022.pdf",
            "summary": "The Bank of England records a further 5% sterling depreciation and severe gilt-market dysfunction after 23 September.",
        },
    },
    "covid_2020_selloff": {
        "label": "12–16 Mar 2020 — pandemic equity sell-off",
        "description": "Broad equity falls, record volatility and strained market liquidity.",
        "start_date": "2020-03-12",
        "end_date": "2020-03-16",
        "news_query": "March 2020 pandemic global equity selloff market liquidity",
        "instruments": ["sp500", "nasdaq", "nikkei", "eurostoxx", "vix"],
        "expected_taxonomy": "Equity market crisis",
        "reference": {
            "publisher": "Board of Governors of the Federal Reserve System",
            "title": "Monetary Policy Report — June 2020",
            "url": "https://www.federalreserve.gov/monetarypolicy/2020-06-mpr-part1.htm",
            "summary": "The Federal Reserve records broad equity prices falling as much as 34% peak-to-trough and VIX levels last seen during the financial crisis.",
        },
    },
}
