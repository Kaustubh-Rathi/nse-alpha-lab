"""Tests for the corporate-actions API identity-edge layer.

The fixtures are synthetic official-API rows. They prove the fail-closed
contract: a company-name match alone never creates an edge, an overlapping
company match is ambiguous, a reused symbol blocks linking, and no price-like
field can influence an edge. The last test runs the same rules over the real
official API file in the repository.
"""

import csv
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "api_edges", Path(__file__).resolve().parents[1] /
    "src/nse_pipeline/identity/api_edges.py")
api_edges = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(api_edges)

LINEAGE_SPEC = importlib.util.spec_from_file_location(
    "identity_lineage", Path(__file__).resolve().parents[1] /
    "src/nse_pipeline/identity/lineage.py")
lineage = importlib.util.module_from_spec(LINEAGE_SPEC)
LINEAGE_SPEC.loader.exec_module(lineage)

REPO = Path(__file__).resolve().parents[1]
REAL_API = REPO / "data" / "processed" / "older_actions" / "nse-api-actions.jsonl"

URL = "https://www.nseindia.com/api/corporates-corporateActions?index=equities"


def api_row(symbol, comp, subject, ex_date, isin, face_value="10",
            series="EQ", source_file="nse-api-2007.json", source_row=0,
            url=URL, **extra):
    """One official API row as the corporate-actions endpoint returns it."""
    row = {
        "symbol": symbol,
        "comp": comp,
        "subject": subject,
        "exDate": ex_date,
        "isin": isin,
        "faceVal": face_value,
        "series": series,
        "source_file": source_file,
        "source_row": source_row,
        "source_url": url,
    }
    row.update(extra)
    return row


def build(rows):
    """Parse rows through the real parser and derive the identity result."""
    return api_edges.build_api_identity(api_edges.parse_api_rows(_write(rows)))


def _write(rows):
    handle = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False,
                                         encoding="utf-8")
    with handle as stream:
        for row in rows:
            stream.write(json.dumps(row) + "\n")
    return Path(handle.name)


class SameIsinRenameTests(unittest.TestCase):
    def test_same_isin_rename_emits_an_edge(self):
        rows = [
            api_row("OLDSYM", "Acme Industries Limited", "Agm", "05-Mar-2005",
                    "INE000A01001", source_row=11),
            api_row("NEWSYM", "Acme Industries Limited", "Agm", "01-Apr-2008",
                    "INE000A01001", source_file="nse-api-2008.json", source_row=7),
        ]
        result = build(rows)
        self.assertEqual(len(result["edges"]), 1)
        edge = result["edges"][0]
        self.assertEqual(edge["old_symbol"], "OLDSYM")
        self.assertEqual(edge["new_symbol"], "NEWSYM")
        self.assertEqual(edge["evidence_class"], "same_isin")
        self.assertEqual(edge["effective_date"], "2008-04-01")
        self.assertEqual(edge["effective_date_basis"], "new_symbol_first_api_date")
        self.assertEqual(edge["shared_isins"], ["INE000A01001"])
        self.assertEqual(edge["handover"]["old_symbol_last_api_date"], "2005-03-05")
        self.assertEqual(edge["handover"]["new_symbol_first_api_date"], "2008-04-01")
        self.assertEqual(edge["citation"]["source_file"], "nse-api-2008.json")
        self.assertEqual(edge["citation"]["source_row"], 7)
        self.assertEqual(edge["citation"]["ex_date"], "2008-04-01")
        self.assertEqual(edge["citation"]["isin"], "INE000A01001")
        self.assertEqual(edge["citation"]["source_url"], URL)
        self.assertEqual(edge["derived_from"], "nse_corporate_actions_api")
        self.assertFalse(edge["from_symbolchange_csv"])
        self.assertEqual(result["unresolved_candidates"], [])

    def test_every_edge_carries_the_api_not_symbolchange_flag(self):
        rows = [
            api_row("OLDSYM", "Acme Industries Limited", "Agm", "05-Mar-2005",
                    "INE000A01001"),
            api_row("NEWSYM", "Acme Industries Limited", "Agm", "01-Apr-2008",
                    "INE000A01001"),
        ]
        for edge in build(rows)["edges"]:
            self.assertTrue(edge["derived_from"])
            self.assertFalse(edge["from_symbolchange_csv"])
            self.assertIn("NOT from symbolchange.csv", edge["note"])


class SplitBridgedRenameTests(unittest.TestCase):
    def test_split_bridged_rename_emits_an_edge(self):
        rows = [
            api_row("REIAGRO", "Rei Agro Limited", "Agm", "18-Sep-2007",
                    "INE385B01015", face_value="10", source_row=1169),
            api_row("REIAGROLTD", "Rei Agro Limited", "Fv Split Rs.10/- To Re.1/",
                    "05-Feb-2009", "INE385B01031", face_value="1",
                    source_file="nse-api-2009.json", source_row=36),
        ]
        result = build(rows)
        self.assertEqual(len(result["edges"]), 1)
        edge = result["edges"][0]
        self.assertEqual(edge["evidence_class"], "split_bridged")
        self.assertEqual(edge["old_symbol"], "REIAGRO")
        self.assertEqual(edge["new_symbol"], "REIAGROLTD")
        self.assertEqual(edge["effective_date"], "2009-02-05")
        self.assertEqual(edge["isin_old"], "INE385B01015")
        self.assertEqual(edge["isin_new"], "INE385B01031")
        self.assertEqual(edge["split_bridge"]["face_value_from"], 10.0)
        self.assertEqual(edge["split_bridge"]["face_value_to"], 1.0)
        self.assertEqual(edge["split_bridge"]["ex_date"], "2009-02-05")
        self.assertEqual(edge["citation"]["source_file"], "nse-api-2009.json")
        self.assertEqual(edge["citation"]["source_row"], 36)
        self.assertEqual(edge["citation"]["source_url"], URL)
        self.assertEqual(edge["citation"]["face_value"], "1")
        self.assertFalse(edge["from_symbolchange_csv"])

    def test_split_outside_the_handover_window_is_not_a_bridge(self):
        # The split is dated after the new symbol's first API row, so it does
        # not bridge the ISIN change.
        rows = [
            api_row("OLDSYM", "Acme Industries Limited", "Agm", "18-Sep-2007",
                    "INE000A01001", face_value="10"),
            api_row("NEWSYM", "Acme Industries Limited", "Agm", "05-Feb-2009",
                    "INE000A01002", face_value="1"),
            api_row("NEWSYM", "Acme Industries Limited", "Fv Split Rs.10/- To Re.1/",
                    "05-Mar-2009", "INE000A01002", face_value="1",
                    source_file="nse-api-2009.json", source_row=99),
        ]
        result = build(rows)
        self.assertEqual(result["edges"], [])
        self.assertEqual(len(result["unresolved_candidates"]), 1)
        candidate = result["unresolved_candidates"][0]
        self.assertEqual(candidate["reason_code"],
                         api_edges.REASON_SPLIT_OUTSIDE_WINDOW)
        self.assertTrue(candidate["split_subject_rows"])


class CompanyNameOnlyTests(unittest.TestCase):
    def test_company_name_only_match_emits_no_edge(self):
        rows = [
            api_row("OLDSYM", "Acme Industries Limited", "Agm", "05-Mar-2005",
                    "INE000A01001"),
            api_row("NEWSYM", "Acme Industries Limited", "Agm", "01-Apr-2008",
                    "INE000A01002"),
        ]
        result = build(rows)
        self.assertEqual(result["edges"], [])
        self.assertEqual(len(result["unresolved_candidates"]), 1)
        candidate = result["unresolved_candidates"][0]
        self.assertEqual(candidate["reason_code"], api_edges.REASON_NO_LINKAGE)
        self.assertEqual(candidate["old_symbol"], "OLDSYM")
        self.assertEqual(candidate["new_symbol"], "NEWSYM")
        self.assertEqual(candidate["company_name"], "Acme Industries Limited")
        self.assertIn("never creates an edge", candidate["reason"])

    def test_similar_but_not_equal_company_names_never_group(self):
        rows = [
            api_row("OLDSYM", "Acme Industries Limited", "Agm", "05-Mar-2005",
                    "INE000A01001"),
            api_row("NEWSYM", "Acme Industries Ltd", "Agm", "01-Apr-2008",
                    "INE000A01001"),
            api_row("THIRD", "Acme Industries and Sons Limited", "Agm",
                    "02-May-2009", "INE000A01001"),
        ]
        result = build(rows)
        self.assertEqual(result["edges"], [])
        self.assertEqual(result["unresolved_candidates"], [])
        self.assertEqual(result["summary"]["symbol_pairs_evaluated"], 0)

    def test_company_key_normalises_only_whitespace_and_case(self):
        self.assertEqual(api_edges.normalise_company("  Acme   Industries Limited "),
                         "acme industries limited")
        self.assertNotEqual(api_edges.normalise_company("Acme Industries (India) Limited"),
                            api_edges.normalise_company("Acme Industries ( India ) Limited"))


class OverlappingWindowTests(unittest.TestCase):
    def test_overlapping_company_match_emits_no_edge(self):
        rows = [
            api_row("OLDSYM", "Acme Industries Limited", "Agm", "05-Mar-2005",
                    "INE000A01001"),
            api_row("OLDSYM", "Acme Industries Limited", "Agm", "05-Mar-2008",
                    "INE000A01001", source_row=2),
            api_row("NEWSYM", "Acme Industries Limited", "Agm", "05-Mar-2006",
                    "INE000A01001", source_row=3),
            api_row("NEWSYM", "Acme Industries Limited", "Agm", "05-Mar-2009",
                    "INE000A01001", source_row=4),
        ]
        result = build(rows)
        self.assertEqual(result["edges"], [])
        self.assertEqual(len(result["unresolved_candidates"]), 1)
        candidate = result["unresolved_candidates"][0]
        self.assertEqual(candidate["reason_code"], api_edges.REASON_OVERLAP)
        self.assertIn("different instruments", candidate["reason"])

    def test_overlap_is_checked_before_the_shared_isin_rule(self):
        # A shared ISIN does not rescue an overlapping pair.
        rows = [
            api_row("OLDSYM", "Acme Industries Limited", "Agm", "05-Mar-2005",
                    "INE000A01001"),
            api_row("OLDSYM", "Acme Industries Limited", "Agm", "05-Mar-2008",
                    "INE000A01001", source_row=2),
            api_row("NEWSYM", "Acme Industries Limited", "Agm", "05-Mar-2006",
                    "INE000A01001", source_row=3),
        ]
        result = build(rows)
        self.assertEqual(result["edges"], [])
        self.assertEqual(result["unresolved_candidates"][0]["reason_code"],
                         api_edges.REASON_OVERLAP)


class SymbolReuseTests(unittest.TestCase):
    def _rows(self, reuse=True):
        rows = [
            api_row("REUSE", "Alpha Limited", "Agm", "10-Jan-2003",
                    "INE000A01001"),
            api_row("OTHER", "Alpha Limited", "Agm", "10-Jan-2005",
                    "INE000A01001", source_row=2),
        ]
        if reuse:
            rows.append(api_row("REUSE", "Beta Limited", "Agm", "10-Jun-2004",
                                "INE000A01009", source_row=3))
        return rows

    def test_symbol_reuse_blocks_linking(self):
        result = build(self._rows(reuse=True))
        self.assertEqual(result["edges"], [])
        self.assertEqual(len(result["unresolved_candidates"]), 1)
        candidate = result["unresolved_candidates"][0]
        self.assertEqual(candidate["reason_code"], api_edges.REASON_REUSE)
        self.assertTrue(candidate["reuse_evidence"])
        self.assertEqual(candidate["reuse_evidence"][0]["other_company"],
                         "Beta Limited")
        self.assertEqual(result["summary"]["symbol_reuse_total"], 1)
        self.assertEqual(result["symbol_reuse"][0]["symbol"], "REUSE")
        self.assertEqual(len(result["symbol_reuse"][0]["companies"]), 2)

    def test_without_the_reuse_the_same_pair_links(self):
        result = build(self._rows(reuse=False))
        self.assertEqual(len(result["edges"]), 1)
        self.assertEqual(result["edges"][0]["evidence_class"], "same_isin")
        self.assertEqual(result["summary"]["symbol_reuse_total"], 0)


class PriceIndependenceTests(unittest.TestCase):
    def _rows(self, **price_fields):
        rows = [
            api_row("OLDSYM", "Acme Industries Limited", "Agm", "05-Mar-2005",
                    "INE000A01001", **price_fields),
            api_row("NEWSYM", "Acme Industries Limited", "Agm", "01-Apr-2008",
                    "INE000A01001", **price_fields),
        ]
        return rows

    def test_price_like_field_never_influences_an_edge(self):
        without = build(self._rows())["edges"]
        with_prices = build(self._rows(close=999.99, price=1.23, open=2.5,
                                        high=3.5, low=0.5, prev_close=10.0,
                                        volume=123456))["edges"]
        self.assertEqual(len(without), 1)
        self.assertEqual(len(with_prices), 1)
        self.assertEqual(without, with_prices)

    def test_price_like_fields_are_not_even_read(self):
        parsed = api_edges.parse_api_rows(
            _write(self._rows(close=999.99, price=1.23, prev_close=10.0)))
        for row in parsed:
            for key in ("close", "price", "open", "high", "low", "prev_close",
                        "volume", "previous_close"):
                self.assertNotIn(key, row)

    def test_a_different_price_value_changes_nothing(self):
        low = build(self._rows(close=1.0))["edges"]
        high = build(self._rows(close=100000.0))["edges"]
        self.assertEqual(low, high)


class FaceValueParsingTests(unittest.TestCase):
    def test_split_face_value_parsing(self):
        self.assertEqual(api_edges.split_face_value("Fv Split Rs.10/- To Re.1/"),
                         (10.0, 1.0))
        self.assertEqual(api_edges.split_face_value("Fv Split-Rs10/-To Rs5/-"),
                         (10.0, 5.0))
        self.assertEqual(api_edges.split_face_value("Face Value Split From Rs.10/- To Rs.2/-"),
                         (10.0, 2.0))
        self.assertEqual(api_edges.split_face_value("Agm/Fv Split Rs10 To Rs2"),
                         (10.0, 2.0))
        self.assertEqual(api_edges.split_face_value("Agm/Fv Split Rs.2 To Re.1"),
                         (2.0, 1.0))
        self.assertIsNone(api_edges.split_face_value("Split Us 64 Into 2 Parts"))
        self.assertIsNone(api_edges.split_face_value("Agm/Dividend-10%"))
        self.assertIsNone(api_edges.split_face_value("Capital Red./Demerger"))


class FreezeImpactTests(unittest.TestCase):
    def test_unpriced_position_days_count_per_symbol(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "equity_marks.csv"
            with path.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(["variant_id", "date", "unpriced_positions",
                                 "unpriced_symbols"])
                writer.writerow(["v1", "2020-01-01", 2, "AAA;BBB"])
                writer.writerow(["v1", "2020-01-02", 1, "AAA"])
                writer.writerow(["v1", "2020-01-03", 0, ""])
            result = api_edges.unpriced_position_days(path)
        self.assertEqual(result["total"], 3)
        self.assertEqual(result["symbols"], {"AAA": 2, "BBB": 1})
        self.assertEqual(result["distinct_symbols"], 2)


class CompanyNameOnlyNeverAnEdgeTests(unittest.TestCase):
    """A company-name match alone never reaches the layer the runners read."""

    def test_company_name_only_candidate_never_becomes_an_edge(self):
        rows = [
            api_row("OLDSYM", "Acme Industries Limited", "Agm", "05-Mar-2005",
                    "INE000A01001"),
            api_row("NEWSYM", "Acme Industries Limited", "Agm", "01-Apr-2008",
                    "INE000A01002"),
        ]
        result = build(rows)
        self.assertEqual(result["edges"], [])
        candidate = result["unresolved_candidates"][0]
        self.assertEqual(candidate["reason_code"], api_edges.REASON_NO_LINKAGE)
        self.assertIn("never creates an edge", candidate["reason"])

    def test_company_name_only_candidate_never_reaches_the_merged_layer(self):
        rows = [
            api_row("OLDSYM", "Acme Industries Limited", "Agm", "05-Mar-2005",
                    "INE000A01001"),
            api_row("NEWSYM", "Acme Industries Limited", "Agm", "01-Apr-2008",
                    "INE000A01002"),
        ]
        result = build(rows)
        # The merged layer the runners consume is built from the EDGES only:
        # a candidate that never became an edge can never remap a key.
        merged = lineage.merge_identity_edges([], result["edges"])
        self.assertEqual(merged["edges"], [])
        self.assertEqual(merged["remap_edges"], [])
        self.assertEqual(merged["refused"], [])

    def test_the_real_api_layer_has_no_company_name_only_edge(self):
        if not REAL_API.exists():
            self.skipTest("official corporate-actions API file not present")
        result = api_edges.build_api_identity(api_edges.parse_api_rows(REAL_API))
        for edge in result["edges"]:
            self.assertIn(edge["evidence_class"],
                          (api_edges.EVIDENCE_SAME_ISIN, api_edges.EVIDENCE_SPLIT_BRIDGED))
            self.assertTrue(edge["shared_isins"] or edge.get("split_bridge"))
        name_only = [candidate for candidate in result["unresolved_candidates"]
                     if candidate["reason_code"] == api_edges.REASON_NO_LINKAGE]
        self.assertTrue(name_only)
        pairs = {(candidate["old_symbol"], candidate["new_symbol"])
                 for candidate in name_only}
        edge_pairs = {(edge["old_symbol"], edge["new_symbol"]) for edge in result["edges"]}
        self.assertFalse(pairs & edge_pairs)


@unittest.skipUnless(REAL_API.exists(), "official corporate-actions API file not present")
class RealApiFileTests(unittest.TestCase):
    """The rules run over the real official API file in the repository."""

    @classmethod
    def setUpClass(cls):
        cls.result = api_edges.build_api_identity(
            api_edges.parse_api_rows(REAL_API))

    def test_real_file_yields_eight_same_isin_and_one_split_bridged_edge(self):
        edges = self.result["edges"]
        self.assertEqual(len(edges), 9)
        by_class = {}
        for edge in edges:
            by_class.setdefault(edge["evidence_class"], []).append(edge)
        self.assertEqual(len(by_class["same_isin"]), 8)
        self.assertEqual(len(by_class["split_bridged"]), 1)

    def test_real_split_bridged_edge_is_rei_agro(self):
        edge = next(e for e in self.result["edges"]
                    if e["evidence_class"] == "split_bridged")
        self.assertEqual(edge["old_symbol"], "REIAGRO")
        self.assertEqual(edge["new_symbol"], "REIAGROLTD")
        self.assertEqual(edge["effective_date"], "2009-02-05")
        self.assertEqual(edge["citation"]["source_file"], "nse-api-2009.json")
        self.assertEqual(edge["citation"]["source_row"], 36)
        self.assertEqual(edge["citation"]["isin"], "INE385B01031")
        self.assertEqual(edge["split_bridge"]["face_value_from"], 10.0)
        self.assertEqual(edge["split_bridge"]["face_value_to"], 1.0)
        self.assertFalse(edge["from_symbolchange_csv"])

    def test_real_unresolved_candidates_all_carry_a_reason(self):
        candidates = self.result["unresolved_candidates"]
        self.assertEqual(len(candidates), 65)
        for candidate in candidates:
            self.assertTrue(candidate["reason_code"])
            self.assertTrue(candidate["reason"])
            self.assertNotIn(candidate["reason_code"], ("", None))

    def test_real_overlapping_pairs_are_flagged_ambiguous(self):
        overlapping = [c for c in self.result["unresolved_candidates"]
                       if c["reason_code"] == api_edges.REASON_OVERLAP]
        self.assertEqual(len(overlapping), 4)
        for candidate in overlapping:
            self.assertIn("different instruments", candidate["reason"])
        self.assertEqual(
            self.result["summary"]["overlapping_window_pairs_flagged_ambiguous"], 4)

    def test_real_file_has_no_symbol_reuse(self):
        self.assertEqual(self.result["summary"]["symbol_reuse_total"], 0)

    def test_real_file_evaluates_one_outer_handover_per_company(self):
        summary = self.result["summary"]
        self.assertEqual(summary["companies_with_more_than_one_symbol"], 74)
        self.assertEqual(summary["symbol_pairs_evaluated"], 74)
        self.assertEqual(summary["edges_total"] + summary["unresolved_candidates_total"],
                         summary["symbol_pairs_evaluated"])
        # The inner links of a rename chain are recorded, never evaluated.
        self.assertEqual(summary["rename_chain_inner_links_recorded"], 6)
        for link in self.result["rename_chain_inner_links"]:
            self.assertEqual(link["reason_code"], api_edges.REASON_CHAIN_INNER_LINK)
            self.assertTrue(link["reason"])


if __name__ == "__main__":
    unittest.main()
