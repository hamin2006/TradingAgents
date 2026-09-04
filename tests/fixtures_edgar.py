"""Shared EDGAR test fixtures (real API shapes: companyfacts, submissions,
Form 4 XML modeled on the Guarini/McCourt 2026-09-03 filings)."""

from __future__ import annotations

import json


def fact_row(start, end, val, filed, form, fy, fp, frame=None, accn="ACC-1"):
    row = {"start": start, "end": end, "val": val, "accn": accn,
           "fy": fy, "fp": fp, "form": form, "filed": filed}
    if frame:
        row["frame"] = frame
    return row


def fact_tag(rows, unit="USD"):
    return {"label": "x", "description": "x", "units": {unit: rows}}


def companyfacts(ticker="REGN", cik="0000872589"):
    """Minimal companyfacts JSON with quarterly + annual + instant facts.

    Values are RAW dollars (as the SEC serves them); the 10-K full-year row
    shares the Revenue tag with the 10-Q quarters (as in real payloads) so
    the quarter/TTM logic must exclude it.
    """
    us_gaap = {
        # Quarterly duration rows (frame = CYyyyyQq), filed a day after end.
        "Revenues": fact_tag([
            fact_row("2025-07-01", "2025-09-30", 3_800_000_000, "2025-10-23", "10-Q", 2025, "Q3", "CY2025Q3"),
            fact_row("2025-10-01", "2025-12-31", 3_900_000_000, "2026-02-12", "10-Q", 2025, "Q4", "CY2025Q4"),
            fact_row("2026-01-01", "2026-03-31", 4_000_000_000, "2026-04-24", "10-Q", 2026, "Q1", "CY2026Q1"),
            fact_row("2026-04-01", "2026-06-30", 4_290_000_000, "2026-07-24", "10-Q", 2026, "Q2", "CY2026Q2"),
            # 10-K full-year row on the same tag: end Dec-31, ~4x the quarters.
            fact_row("2025-01-01", "2025-12-31", 15_200_000_000, "2026-02-12", "10-K", 2025, "FY", "CY2025"),
        ]),
        "GrossProfit": fact_tag([
            fact_row("2026-01-01", "2026-03-31", 3_300_000_000, "2026-04-24", "10-Q", 2026, "Q1", "CY2026Q1"),
            fact_row("2026-04-01", "2026-06-30", 3_540_000_000, "2026-07-24", "10-Q", 2026, "Q2", "CY2026Q2"),
        ]),
        "OperatingIncomeLoss": fact_tag([
            fact_row("2026-01-01", "2026-03-31", 900_000_000, "2026-04-24", "10-Q", 2026, "Q1", "CY2026Q1"),
            fact_row("2026-04-01", "2026-06-30", 1_180_000_000, "2026-07-24", "10-Q", 2026, "Q2", "CY2026Q2"),
        ]),
        "NetIncomeLoss": fact_tag([
            fact_row("2026-01-01", "2026-03-31", 700_000_000, "2026-04-24", "10-Q", 2026, "Q1", "CY2026Q1"),
            fact_row("2026-04-01", "2026-06-30", 900_000_000, "2026-07-24", "10-Q", 2026, "Q2", "CY2026Q2"),
        ]),
        "NetCashProvidedByUsedInOperatingActivities": fact_tag([
            fact_row("2026-01-01", "2026-03-31", 1_100_000_000, "2026-04-24", "10-Q", 2026, "Q1", "CY2026Q1"),
            fact_row("2026-04-01", "2026-06-30", 1_250_000_000, "2026-07-24", "10-Q", 2026, "Q2", "CY2026Q2"),
        ]),
        "PaymentsToAcquirePropertyPlantAndEquipment": fact_tag([
            fact_row("2026-01-01", "2026-03-31", 150_000_000, "2026-04-24", "10-Q", 2026, "Q1", "CY2026Q1"),
            fact_row("2026-04-01", "2026-06-30", 160_000_000, "2026-07-24", "10-Q", 2026, "Q2", "CY2026Q2"),
        ]),
        "PaymentsForRepurchaseOfCommonStock": fact_tag([
            fact_row("2026-01-01", "2026-03-31", 500_000_000, "2026-04-24", "10-Q", 2026, "Q1", "CY2026Q1"),
            fact_row("2026-04-01", "2026-06-30", 550_000_000, "2026-07-24", "10-Q", 2026, "Q2", "CY2026Q2"),
        ]),
        "PaymentsOfDividends": fact_tag([
            fact_row("2026-01-01", "2026-03-31", 90_000_000, "2026-04-24", "10-Q", 2026, "Q1", "CY2026Q1"),
            fact_row("2026-04-01", "2026-06-30", 95_000_000, "2026-07-24", "10-Q", 2026, "Q2", "CY2026Q2"),
        ]),
        "DepreciationDepletionAndAmortization": fact_tag([
            fact_row("2026-01-01", "2026-03-31", 120_000_000, "2026-04-24", "10-Q", 2026, "Q1", "CY2026Q1"),
            fact_row("2026-04-01", "2026-06-30", 125_000_000, "2026-07-24", "10-Q", 2026, "Q2", "CY2026Q2"),
        ]),
        # Balance-sheet instants (quarter-end frames CYyyyyQqI)
        "Assets": fact_tag([
            fact_row(None, "2026-03-31", 32_000_000_000, "2026-04-24", "10-Q", 2026, "Q1", "CY2026Q1I"),
            fact_row(None, "2026-06-30", 33_000_000_000, "2026-07-24", "10-Q", 2026, "Q2", "CY2026Q2I"),
        ]),
        "Liabilities": fact_tag([
            fact_row(None, "2026-03-31", 10_000_000_000, "2026-04-24", "10-Q", 2026, "Q1", "CY2026Q1I"),
            fact_row(None, "2026-06-30", 10_100_000_000, "2026-07-24", "10-Q", 2026, "Q2", "CY2026Q2I"),
        ]),
        "StockholdersEquity": fact_tag([
            fact_row(None, "2026-03-31", 22_000_000_000, "2026-04-24", "10-Q", 2026, "Q1", "CY2026Q1I"),
            fact_row(None, "2026-06-30", 22_900_000_000, "2026-07-24", "10-Q", 2026, "Q2", "CY2026Q2I"),
        ]),
        "CashAndCashEquivalentsAtCarryingValue": fact_tag([
            fact_row(None, "2026-03-31", 5_000_000_000, "2026-04-24", "10-Q", 2026, "Q1", "CY2026Q1I"),
            fact_row(None, "2026-06-30", 5_200_000_000, "2026-07-24", "10-Q", 2026, "Q2", "CY2026Q2I"),
        ]),
        "LongTermDebtNoncurrent": fact_tag([
            fact_row(None, "2026-06-30", 6_000_000_000, "2026-07-24", "10-Q", 2026, "Q2", "CY2026Q2I"),
        ]),
    }
    dei = {
        "EntityCommonStockSharesOutstanding": fact_tag([
            fact_row(None, "2026-07-24", 103100000, "2026-07-24", "10-Q", 2026, "Q2", None),
        ], unit="shares"),
    }
    return {"cik": cik.lstrip("0") or "0", "entityName": "REGENERON PHARMACEUTICALS",
            "facts": {"us-gaap": us_gaap, "dei": dei}}


def submissions(ticker="REGN", cik="0000872589", extra_forms=None):
    """Recent-filings list with two Form 4s + one 8-K (today)."""
    forms = extra_forms or ["4", "4", "8-K", "10-Q"]
    accessions = ["000166375826000002", "000199235226000007",
                  "000166375826000009", "000166375826000010"]
    dates = ["2026-09-03", "2026-09-03", "2026-09-02", "2026-07-24"]
    docs = ["edgardoc.xml", "edgardoc.xml", "regeneron-8k.htm", "regeneron-10q.htm"]
    return {
        "cik": cik.lstrip("0") or "0",
        "name": "REGENERON PHARMACEUTICALS",
        "tickers": [ticker],
        "filings": {"recent": {
            "accessionNumber": accessions[:len(forms)],
            "form": forms,
            "filingDate": dates[:len(forms)],
            "primaryDocument": docs[:len(forms)],
        }},
    }


FORM4_XML = """<?xml version="1.0"?>
<ownershipDocument>
  <schemaVersion>X0609</schemaVersion>
  <documentType>4</documentType>
  <periodOfReport>2026-09-02</periodOfReport>
  <issuer>
    <issuerCik>0000872589</issuerCik>
    <issuerName>REGENERON PHARMACEUTICALS, INC.</issuerName>
    <issuerTradingSymbol>REGN</issuerTradingSymbol>
  </issuer>
  <reportingOwner>
    <reportingOwnerId><rptOwnerCik>0001992352</rptOwnerCik></reportingOwnerId>
    <reportingOwnerName>
      <rptOwnerName>Guarini Kathryn</rptOwnerName>
    </reportingOwnerName>
    <reportingOwnerRelationship>
      <isDirector>1</isDirector>
      <rptOwnerTitle></rptOwnerTitle>
    </reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeHolding>
      <securityTitle><value>Common Stock</value></securityTitle>
      <postTransactionAmounts><sharesOwnedFollowingTransaction><value>22488</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
    </nonDerivativeHolding>
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>2026-09-02</value></transactionDate>
      <transactionCoding>
        <transactionFormType>4</transactionFormType>
        <transactionCode>S</transactionCode>
        <transactionEquitySwapInd>0</transactionEquitySwapInd>
      </transactionCoding>
      <transactionAmounts>
        <transactionShares><value>400</value></transactionShares>
        <transactionPricePerShare><value>850.0</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
      <postTransactionAmounts><sharesOwnedFollowingTransaction><value>22488</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
  <derivativeTable>
    <derivativeTransaction>
      <securityTitle><value>Option (right to buy)</value></securityTitle>
      <transactionDate><value>2026-09-02</value></transactionDate>
      <transactionCoding>
        <transactionFormType>4</transactionFormType>
        <transactionCode>M</transactionCode>
      </transactionCoding>
      <transactionAmounts>
        <transactionShares><value>400</value></transactionShares>
        <transactionPricePerShare><value>719.37</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
    </derivativeTransaction>
  </derivativeTable>
</ownershipDocument>
"""


def json_bytes(payload) -> bytes:
    return json.dumps(payload).encode()
