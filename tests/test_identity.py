import hashlib
import importlib.util
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

import duckdb

SPEC = importlib.util.spec_from_file_location("identity", Path(__file__).resolve().parents[1] / "src/nse_pipeline/identity/lineage.py")
identity = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(identity)


def obs(symbols=None, intervals=None, price=None, raw_intervals=None):
    return {"symbols": symbols or {}, "isin_intervals_by_symbol": intervals or {},
            "isin_to_symbols": {}, "source": "test", "read_only": True,
            "isin_intervals": raw_intervals or [],
            "last_eqbe_price_by_symbol": price or {}}


def iv(symbol, isin, first, last):
    return {"symbol": symbol, "isin": isin, "first_observed": first,
            "last_observed": last, "sessions": 10}


def change(old, new, eff, company="Co", row=0):
    return {"old_symbol": old, "new_symbol": new, "company": company,
            "effective_date": eff, "source_row": row}


class IdentityTests(unittest.TestCase):
    def test_parse_dates(self):
        self.assertEqual(str(identity.parse_date("04-Jan-2000")), "2000-01-04")
        self.assertEqual(str(identity.parse_date("15-Apr-02")), "2002-04-15")
        self.assertIsNone(identity.parse_date("-"))
        with self.assertRaises(ValueError):
            identity.parse_date("not-a-date")

    def test_parse_symbolchange_strips_and_validates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nse-symbolchange.csv"
            path.write_bytes(b"NAME OF THE COMPANY,OLD SYMBOL,NEW SYMBOL,EFFECTIVE DATE\n"
                             b" Co Ltd , OLD , NEW ,01-Jan-2005\n")
            rows = identity.parse_symbolchange(path)
            self.assertEqual(rows[0]["old_symbol"], "OLD")
            self.assertEqual(rows[0]["effective_date"], "2005-01-01")
            path.write_bytes(b"NAME OF THE COMPANY,OLD SYMBOL,NEW SYMBOL,EFFECTIVE DATE\n"
                             b"Co,OLD,NEW,not-a-date\n")
            with self.assertRaises(ValueError):
                identity.parse_symbolchange(path)

    def test_edges_only_from_official_rows_no_name_similarity(self):
        # Similar company names must never create an edge on their own.
        rows = [change("AAA", "BBB", "2005-01-01", "Alpha Limited", 0),
                change("CCC", "DDD", "2006-01-01", "Alpha Limited", 1)]
        edges, excluded = identity.build_edges(rows, obs())
        self.assertEqual(len(edges), 2)
        self.assertEqual(excluded, [])
        self.assertNotIn(("AAA", "DDD"), {(e["old_symbol"], e["new_symbol"]) for e in edges})
        for edge in edges:
            self.assertEqual(edge["verification"], "official_symbolchange_row")
            self.assertIn("symbolchange.csv", edge["source"])

    def test_old_equals_new_is_excluded_not_an_edge(self):
        rows = [change("SAME", "SAME", "2026-06-08", "Co", 0),
                change("A", "B", "2020-01-01", "Co", 1)]
        edges, excluded = identity.build_edges(rows, obs())
        self.assertEqual([e["new_symbol"] for e in edges], ["B"])
        self.assertEqual(len(excluded), 1)
        unresolved = identity.build_unresolved(edges, excluded, obs(), [])
        self.assertEqual(unresolved[0]["kind"], "old_equals_new_symbolchange_row")

    def test_pre2011_isin_not_backfilled_from_later_observation(self):
        # Bars ISINs start 2011; a 2005 edge must keep null ISINs even
        # though the symbols later have exactly one ISIN each.
        intervals = {"OLD": [iv("OLD", "INEOLD", "2011-06-22", "2012-12-31")],
                     "NEW": [iv("NEW", "INENEW", "2011-06-22", "2012-12-31")]}
        edges, _ = identity.build_edges([change("OLD", "NEW", "2005-01-01")], obs(intervals=intervals))
        self.assertIsNone(edges[0]["isin_old"])
        self.assertIsNone(edges[0]["isin_new"])
        self.assertIsNone(edges[0]["isin_change"])

    def test_post2011_isin_brackets_effective_date_and_detects_change(self):
        intervals = {"OLD": [iv("OLD", "INE1", "2011-06-22", "2020-03-31")],
                     "NEW": [iv("NEW", "INE2", "2020-04-01", "2026-01-01")]}
        edges, _ = identity.build_edges([change("OLD", "NEW", "2020-04-01")], obs(intervals=intervals))
        self.assertEqual(edges[0]["isin_old"], "INE1")
        self.assertEqual(edges[0]["isin_new"], "INE2")
        self.assertTrue(edges[0]["isin_change"])
        same = {"OLD": [iv("OLD", "INE1", "2011-06-22", "2020-03-31")],
                "NEW": [iv("NEW", "INE1", "2020-04-01", "2026-01-01")]}
        edges, _ = identity.build_edges([change("OLD", "NEW", "2020-04-01")], obs(intervals=same))
        self.assertFalse(edges[0]["isin_change"])

    def test_ambiguous_isin_tie_gives_null(self):
        intervals = {"OLD": [iv("OLD", "INE1", "2011-06-22", "2020-03-31"),
                             iv("OLD", "INE2", "2015-01-01", "2020-03-31")],
                     "NEW": [iv("NEW", "INE3", "2020-04-01", "2026-01-01")]}
        result = identity.assign_isin(intervals["OLD"], intervals["NEW"], date(2020, 4, 1))
        self.assertIsNone(result["isin_old"])
        self.assertTrue(result["isin_old_ambiguous"])
        self.assertEqual(result["isin_new"], "INE3")

    def test_gfl_godrejfood_never_becomes_an_edge(self):
        edges, _ = identity.build_edges([change("GFLLIMITED", "X", "2019-01-01")], obs())
        self.assertFalse(any(e["old_symbol"] == "GFL" or e["new_symbol"] == "GODREJFOOD"
                             for e in edges))
        delisted = [{"symbol": "GFL", "company": "Godrej Foods Limited",
                     "delisted_date": "2005-04-19", "type": "Voluntary"}]
        unresolved = identity.build_unresolved(edges, [], obs(), delisted)
        entry = next(u for u in unresolved if u.get("symbol") == "GFL")
        self.assertEqual(entry["kind"], "pre2011_delisted_without_symbolchange_edge")
        self.assertIn("unverified", entry["reason"])

    def test_sequential_multi_isin_is_resolved_observation_not_edge(self):
        # Strictly sequential ISIN intervals under one symbol: dated
        # observation-only resolution, never a promoted symbol edge.
        intervals = {"MULTI": [iv("MULTI", "INE1", "2011-06-22", "2020-01-01"),
                               iv("MULTI", "INE2", "2020-01-02", "2026-01-01")]}
        o = obs(intervals=intervals)
        resolved = identity.build_resolved_observations([], o)
        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0]["kind"], "isin_transition_same_symbol")
        self.assertEqual(resolved[0]["steps"][0]["from"], "INE1")
        self.assertEqual(resolved[0]["steps"][0]["to"], "INE2")
        self.assertFalse(resolved[0]["promoted_to_symbol_edge"])
        self.assertEqual(identity.build_unresolved([], [], o, []), [])

    def test_overlapping_multi_isin_stays_unresolved(self):
        intervals = {"MULTI": [iv("MULTI", "INE1", "2011-06-22", "2020-06-01"),
                               iv("MULTI", "INE2", "2020-01-02", "2026-01-01")]}
        o = obs(intervals=intervals)
        self.assertEqual(identity.build_resolved_observations([], o), [])
        unresolved = identity.build_unresolved([], [], o, [])
        self.assertEqual([u["kind"] for u in unresolved],
                         ["bars_multi_isin_symbol_without_symbolchange_edge"])

    def test_isin_symbol_sequence_and_official_edge(self):
        intervals = {"AAA": [iv("AAA", "INE9", "2011-06-22", "2019-12-31")],
                     "BBB": [iv("BBB", "INE9", "2020-01-01", "2026-01-01")]}
        o = obs(intervals=intervals)
        o["isin_intervals"] = [intervals["AAA"][0], intervals["BBB"][0]]
        o["isin_to_symbols"] = {"INE9": ["AAA", "BBB"]}
        resolved = identity.build_resolved_observations([], o)
        self.assertEqual([r["kind"] for r in resolved], ["isin_symbol_sequence"])
        self.assertFalse(resolved[0]["promoted_to_symbol_edge"])
        self.assertEqual(identity.build_unresolved([], [], o, []), [])
        # An official edge for the pair removes even the resolved lead.
        edges, _ = identity.build_edges([change("AAA", "BBB", "2020-01-01")], o)
        self.assertEqual(identity.build_resolved_observations(edges, o), [])
        self.assertNotIn("bars_isin_maps_to_multiple_symbols_without_edge",
                         {u["kind"] for u in identity.build_unresolved(edges, [], o, [])})

    def test_isin_missing_records_external_evidence_gap(self):
        edges, _ = identity.build_edges([change("OLD", "NEW", "2005-01-01")], obs())
        unresolved = identity.build_unresolved(edges, [], obs(), [])
        entry = next(u for u in unresolved if u["kind"] == "edge_isin_missing")
        self.assertIn("external_evidence_gap", entry)
        codes = {r["code"] for r in entry["isin_missing_reasons"]}
        self.assertEqual(codes, {"no_eqbe_isin_interval_for_symbol"})

    def test_dated_isin_mapping_is_interval_bounded(self):
        intervals = {"AAA": [iv("AAA", "INE9", "2011-06-22", "2019-12-31")]}
        o = obs(intervals=intervals, raw_intervals=intervals["AAA"])
        mapping = identity.build_dated_isin_mapping(o)
        self.assertEqual(len(mapping), 1)
        self.assertEqual(mapping[0]["valid_from"], "2011-06-22")
        self.assertEqual(mapping[0]["valid_to"], "2019-12-31")
        self.assertEqual(mapping[0]["verification"], "eqbe_bars_interval_observation_only")
        self.assertEqual(identity.build_dated_isin_mapping(None), [])

    def test_sequential_isin_missing_audit_is_observation_only(self):
        # Single ISIN per side, strictly sequential, effective date in the
        # gap, but old side outside the 365d window: audit, never assign.
        intervals = {"OLD": [iv("OLD", "INE1", "2011-06-22", "2019-07-10")],
                     "NEW": [iv("NEW", "INE2", "2025-06-19", "2026-11-06")]}
        o = obs(intervals=intervals)
        edges, _ = identity.build_edges([change("OLD", "NEW", "2025-06-17")], o)
        self.assertIsNone(edges[0]["isin_old"])  # outside bracket window
        audits = identity.build_isin_missing_audits(edges, o)
        self.assertEqual(len(audits), 1)
        self.assertEqual(audits[0]["kind"], "sequential_different_isin_observation_only")
        self.assertFalse(audits[0]["candidate_isin_same"])
        self.assertFalse(audits[0]["assigned_to_edge"])
        self.assertIn("external_evidence_gap", audits[0])
        # Edge stays unresolved, annotated with the audit.
        unresolved = identity.build_unresolved(edges, [], o, [], isin_audits=audits)
        entry = next(u for u in unresolved if u["kind"] == "edge_isin_missing")
        self.assertEqual(entry["isin_missing_observation_audit"]["audit_id"], edges[0]["edge_id"])
        self.assertIsNone(edges[0]["isin_old"])  # still not assigned

    def test_sequential_audit_excludes_overlap_multi_and_outside_gap(self):
        # Overlapping intervals: no audit.
        overlap = {"OLD": [iv("OLD", "INE1", "2011-06-22", "2020-06-01")],
                   "NEW": [iv("NEW", "INE2", "2020-01-01", "2026-01-01")]}
        edges, _ = identity.build_edges([change("OLD", "NEW", "2020-04-01")], obs(intervals=overlap))
        self.assertEqual(identity.build_isin_missing_audits(edges, obs(intervals=overlap)), [])
        # Multi-ISIN on one side: no audit.
        multi = {"OLD": [iv("OLD", "INE1", "2011-06-22", "2019-01-01"),
                         iv("OLD", "INE2", "2019-01-02", "2020-01-01")],
                 "NEW": [iv("NEW", "INE3", "2020-04-01", "2026-01-01")]}
        edges, _ = identity.build_edges([change("OLD", "NEW", "2020-04-01")], obs(intervals=multi))
        self.assertEqual(identity.build_isin_missing_audits(edges, obs(intervals=multi)), [])
        # Sequential but effective date outside the gap: no audit.
        outside = {"OLD": [iv("OLD", "INE1", "2011-06-22", "2012-01-01")],
                   "NEW": [iv("NEW", "INE1", "2013-01-01", "2026-01-01")]}
        edges, _ = identity.build_edges([change("OLD", "NEW", "2005-01-01")], obs(intervals=outside))
        self.assertEqual(identity.build_isin_missing_audits(edges, obs(intervals=outside)), [])

    def test_terminal_proceeds_reports_verified_observation_not_exit_price(self):
        symbols = {"OLD": {"symbol": "OLD", "first_observed": "2000-01-03",
                           "last_observed": "2005-04-18", "sessions": 100,
                           "series_count": 1, "eqbe_first_observed": "2000-01-03",
                           "eqbe_last_observed": "2005-04-18", "eqbe_sessions": 100}}
        price = {"OLD": {"symbol": "OLD", "price_date": "2005-04-18", "close": 95.0,
                         "series": "EQ", "isin": None, "source": "bhav.csv",
                         "verification": "eqbe_bars_last_observed_close_observation_only"}}
        o = obs(symbols=symbols, price=price)
        delisted = [{"symbol": "OLD", "company": "Co", "delisted_date": "2005-04-19",
                     "type": "Voluntary Delisting"}]
        audit = identity.terminal_proceeds_audit(delisted, o, [])
        row = audit["rows"][0]
        self.assertEqual(row["last_observed_price_observation"]["close"], 95.0)
        self.assertEqual(row["last_observed_price_observation"]["price_date"], "2005-04-18")
        self.assertIsNone(row["exit_price"])
        self.assertIsNone(row["proceeds_per_share"])
        self.assertEqual(audit["summary"]["with_verified_observed_price"], 1)
        self.assertEqual(audit["summary"]["proceeds_resolved"], 0)
        # Explicit verified consideration is the only way to resolve proceeds.
        resolved = identity.terminal_proceeds_audit(
            delisted, o, [], verified_consideration={"OLD": {"cash_per_share": 100.0, "citation": "scheme.pdf"}})
        self.assertEqual(resolved["rows"][0]["exit_price"], 100.0)
        self.assertEqual(resolved["summary"]["proceeds_resolved"], 1)

    def test_terminal_proceeds_audit_never_invents_exit_price(self):
        symbols = {"OLD": {"symbol": "OLD", "first_observed": "2000-01-03",
                           "last_observed": "2005-04-18", "sessions": 100,
                           "series_count": 1, "eqbe_first_observed": "2000-01-03",
                           "eqbe_last_observed": "2005-04-18", "eqbe_sessions": 100}}
        o = obs(symbols=symbols)
        delisted = [{"symbol": "OLD", "company": "Co", "delisted_date": "2005-04-19",
                     "type": "Voluntary Delisting"},
                    {"symbol": "GHOST", "company": "Ghost", "delisted_date": "2010-01-01",
                     "type": "Compulsory Delisting"}]
        audit = identity.terminal_proceeds_audit(delisted, o, [])
        self.assertEqual(audit["summary"]["exit_prices_invented"], 0)
        self.assertEqual(audit["summary"]["proceeds_resolved"], 0)
        row = audit["rows"][0]
        self.assertTrue(row["bars_observed"])
        self.assertEqual(row["last_observed_timing_vs_delisting"], "within_30_days_of_delisting")
        self.assertIsNone(row["exit_price"])
        self.assertIsNone(row["proceeds_per_share"])
        self.assertEqual(row["proceeds_status"], "unresolved_no_verified_exit_price")
        self.assertIn("external_evidence_gap", row)

    def test_reuse_and_branch_flags_and_components(self):
        rows = [change("A", "B", "2010-01-01", row=0), change("B", "C", "2015-01-01", row=1),
                change("A", "D", "2012-01-01", row=2)]
        edges, _ = identity.build_edges(rows, obs())
        by_id = {(e["old_symbol"], e["new_symbol"]): e for e in edges}
        self.assertTrue(by_id[("A", "B")]["reuse_flags"]["new_symbol_is_also_an_old_symbol"])
        self.assertTrue(by_id[("A", "B")]["reuse_flags"]["old_symbol_has_multiple_successors"])
        self.assertTrue(by_id[("B", "C")]["reuse_flags"]["old_symbol_is_also_a_new_symbol"])
        self.assertEqual(identity.lineage_components(edges), [["A", "B", "C", "D"]])

    def test_bars_order_violation_flagged(self):
        symbols = {"OLD": {"symbol": "OLD", "first_observed": "2020-01-01",
                           "last_observed": "2020-12-31", "sessions": 5, "series_count": 1,
                           "eqbe_first_observed": "2020-01-01", "eqbe_last_observed": "2020-12-31",
                           "eqbe_sessions": 5},
                   "NEW": {"symbol": "NEW", "first_observed": "2010-01-01",
                           "last_observed": "2010-12-31", "sessions": 5, "series_count": 1,
                           "eqbe_first_observed": "2010-01-01", "eqbe_last_observed": "2010-12-31",
                           "eqbe_sessions": 5}}
        edges, _ = identity.build_edges([change("OLD", "NEW", "2015-01-01")], obs(symbols=symbols))
        self.assertTrue(edges[0]["bars_order_violation"])

    def test_build_readonly_duckdb_and_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "bars.db"
            con = duckdb.connect(str(db))
            con.execute("CREATE TABLE bars(date DATE, symbol VARCHAR, series VARCHAR, isin VARCHAR, close DOUBLE)")
            con.execute("INSERT INTO bars VALUES "
                        "(DATE '2019-01-02','OLD','EQ','INE1',10),"
                        "(DATE '2019-06-03','OLD','EQ','INE1',11),"
                        "(DATE '2020-04-02','NEW','EQ','INE2',20),"
                        "(DATE '2020-06-01','NEW','EQ','INE2',21),"
                        "(DATE '2020-04-02','DEBT','GS','INEDEBT',100)")
            con.execute("CREATE VIEW bars_v AS SELECT * FROM bars")
            con.close()
            before = hashlib.sha256(db.read_bytes()).hexdigest()
            # bars_observations uses table/view named bars; use a view-only db.
            con = duckdb.connect(str(root / "bars2.db"))
            con.execute("CREATE TABLE t(date DATE, symbol VARCHAR, series VARCHAR, isin VARCHAR, close DOUBLE)")
            con.execute("CREATE VIEW bars AS SELECT * FROM t")
            con.execute("INSERT INTO t VALUES "
                        "(DATE '2019-01-02','OLD','EQ','INE1',10),"
                        "(DATE '2020-03-02','OLD','EQ','INE1',11),"
                        "(DATE '2020-04-02','NEW','EQ','INE2',20)")
            con.close()
            before2 = hashlib.sha256((root / "bars2.db").read_bytes()).hexdigest()
            sym = root / "nse-symbolchange.csv"
            sym.write_bytes(b"NAME OF THE COMPANY,OLD SYMBOL,NEW SYMBOL,EFFECTIVE DATE\n"
                            b"Co,OLD,NEW,01-Apr-2020\n")
            summary = identity.build(sym, None, root / "bars2.db", root / "out", root / "out/coverage.json")
            self.assertEqual(summary["edges_total"], 1)
            self.assertEqual(summary["isin"]["both_present_and_changed"], 1)
            self.assertEqual(summary["bars_continuity"]["bars_symbols_gaining_continuity_eqbe"], 2)
            self.assertEqual(hashlib.sha256((root / "bars2.db").read_bytes()).hexdigest(), before2)
            self.assertEqual(hashlib.sha256(db.read_bytes()).hexdigest(), before)
            edge = json.loads((root / "out/identity_edges.jsonl").read_text())
            self.assertEqual(edge["isin_old"], "INE1")
            self.assertEqual(edge["isin_new"], "INE2")


class CombinedIdentityEdgeTests(unittest.TestCase):
    """The combined edge set: official rows plus API edges, provenance-tagged."""

    def _api_edge(self, old="REIAGRO", new="REIAGROLTD", date="2009-02-05"):
        return {
            "edge_id": f"API|{old}->{new}|{date}",
            "old_symbol": old,
            "new_symbol": new,
            "company": "Rei Agro Limited",
            "effective_date": date,
            "evidence_class": "split_bridged",
            "isin_old": "INE385B01015",
            "isin_new": "INE385B01031",
            "derived_from": "nse_corporate_actions_api",
            "from_symbolchange_csv": False,
            "verification": "official_nse_corporate_actions_api",
            "note": "Derived from the corporate-actions API, NOT from symbolchange.csv.",
        }

    def _official_edge(self, old="OLD", new="NEW"):
        edges, _ = identity.build_edges([change(old, new, "2005-01-01")], obs())
        return edges[0]

    def test_combined_contains_every_official_edge_unchanged_plus_api_edges(self):
        official = [self._official_edge("A", "B"), self._official_edge("C", "D")]
        api = [self._api_edge()]
        combined = identity.build_combined_edges(official, api)
        self.assertEqual(len(combined), 3)
        # Every original edge is present, with every one of its own fields
        # unchanged; the only addition is the provenance tag.
        for edge in official:
            match = next(e for e in combined
                         if (e["old_symbol"], e["new_symbol"]) ==
                         (edge["old_symbol"], edge["new_symbol"]))
            for key, value in edge.items():
                self.assertEqual(match[key], value, key)
            self.assertEqual(match["edge_source"], "official_symbolchange_row")
            self.assertEqual(match["verification"], "official_symbolchange_row")
        # The API edges are appended with their own verification value.
        api_edge = combined[-1]
        self.assertEqual(api_edge["edge_source"], "official_corporate_actions_api")
        self.assertEqual(api_edge["verification"], "official_corporate_actions_api_rename")
        self.assertEqual(api_edge["evidence_class"], "split_bridged")
        self.assertEqual(api_edge["edge_id"], "API|REIAGRO->REIAGROLTD|2009-02-05")

    def test_official_edges_file_is_not_modified_by_the_combined_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            processed = Path(tmp)
            official_path = processed / "identity_edges.jsonl"
            official_path.write_text(
                json.dumps(self._official_edge("A", "B")) + "\n", encoding="utf-8")
            (processed / "api_identity_edges.jsonl").write_text(
                json.dumps(self._api_edge()) + "\n", encoding="utf-8")
            before = hashlib.sha256(official_path.read_bytes()).hexdigest()
            summary = identity.write_combined_identity_edges(processed)
            after = hashlib.sha256(official_path.read_bytes()).hexdigest()
            self.assertEqual(before, after,
                             "the official identity_edges.jsonl must stay byte-identical")
            self.assertFalse(summary["official_edges_file_modified"])
            combined = [json.loads(line) for line in
                        (processed / "identity_edges_combined.jsonl")
                        .read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertEqual(len(combined), 2)
            self.assertEqual(summary["combined_edges_total"], 2)

    def test_api_and_symbolchange_edge_for_the_same_pair_are_distinguishable(self):
        official = [self._official_edge("SAME", "PAIR")]
        api = [self._api_edge("SAME", "PAIR", "2005-01-01")]
        combined = identity.build_combined_edges(official, api)
        self.assertEqual(len(combined), 2)
        by_source = {edge["edge_source"]: edge for edge in combined}
        self.assertEqual(set(by_source),
                         {"official_symbolchange_row", "official_corporate_actions_api"})
        official_edge = by_source["official_symbolchange_row"]
        api_edge = by_source["official_corporate_actions_api"]
        self.assertEqual((official_edge["old_symbol"], official_edge["new_symbol"]),
                         (api_edge["old_symbol"], api_edge["new_symbol"]))
        self.assertNotEqual(official_edge["verification"], api_edge["verification"])
        self.assertEqual(official_edge["verification"], "official_symbolchange_row")
        self.assertEqual(api_edge["verification"], "official_corporate_actions_api_rename")
        self.assertNotEqual(official_edge.get("evidence_class"),
                            api_edge.get("evidence_class"))
        # The official edge keeps its own source label; the API edge never
        # claims to come from the symbol-change file.
        self.assertIn("symbolchange.csv", official_edge["source"])
        self.assertFalse(api_edge["from_symbolchange_csv"])

    def test_build_writes_the_combined_file_and_leaves_the_official_one_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sym = root / "nse-symbolchange.csv"
            sym.write_bytes(b"NAME OF THE COMPANY,OLD SYMBOL,NEW SYMBOL,EFFECTIVE DATE\n"
                            b"Co,OLD,NEW,01-Apr-2020\n")
            out = root / "out"
            identity.build(sym, None, None, out, out / "coverage.json")
            official_path = out / "identity_edges.jsonl"
            combined_path = out / "identity_edges_combined.jsonl"
            self.assertTrue(combined_path.exists())
            official = official_path.read_text(encoding="utf-8")
            combined = [json.loads(line) for line in
                        combined_path.read_text(encoding="utf-8").splitlines()
                        if line.strip()]
            self.assertEqual(len(combined), 1)
            self.assertEqual(combined[0]["edge_source"], "official_symbolchange_row")
            # The official file is not rewritten with the provenance tag.
            self.assertNotIn("edge_source", official)

    def test_real_repository_combined_file_holds_official_plus_api_edges(self):
        processed = Path(__file__).resolve().parents[1] / "data" / "processed" / "identity"
        official_path = processed / "identity_edges.jsonl"
        api_path = processed / "api_identity_edges.jsonl"
        if not official_path.exists() or not api_path.exists():
            self.skipTest("identity layers not built in this repository")
        before = hashlib.sha256(official_path.read_bytes()).hexdigest()
        official = identity.read_jsonl(official_path)
        api = identity.load_api_identity_edges(api_path)
        combined = identity.build_combined_edges(official, api)
        self.assertEqual(len(combined), len(official) + len(api))
        self.assertEqual(hashlib.sha256(official_path.read_bytes()).hexdigest(), before)
        sources = {edge["edge_source"] for edge in combined}
        self.assertEqual(sources,
                         {"official_symbolchange_row", "official_corporate_actions_api"})
        api_edges = [e for e in combined
                     if e["edge_source"] == "official_corporate_actions_api"]
        self.assertEqual(len(api_edges), len(api))
        for edge in api_edges:
            self.assertEqual(edge["verification"], "official_corporate_actions_api_rename")

    def test_strict_merge_view_refuses_an_edge_without_a_documented_ratio(self):
        official = [self._official_edge("A", "B")]
        api = [self._api_edge("OLD", "NEW", "2009-02-05")]
        merged = identity.merge_identity_edges(official, api)
        self.assertEqual(merged["summary"]["edges_total"], 2)
        self.assertEqual(merged["summary"]["official_edges"], 1)
        self.assertEqual(merged["summary"]["api_edges"], 1)
        refused = merged["refused"]
        self.assertEqual(len(refused), 1)
        self.assertIn(identity.REFUSAL_EXCHANGE_RATIO,
                      refused[0]["refusal_codes"])
        # The official edge is untouched by the merge.
        official_edge = next(e for e in merged["edges"]
                             if e["edge_source"] == "official_symbolchange_row")
        self.assertEqual(official_edge["old_symbol"], "A")
        self.assertEqual(official_edge["new_symbol"], "B")

    def test_strict_merge_view_refuses_a_documented_ratio_it_may_not_carry(self):
        # Two different ISINs prove the same company, not the same share
        # count: the exchange ratio at the handover is not documented, so the
        # edge may never imply a 1:1 carry-over. The edge is reported with the
        # reason and never applied.
        official = [self._official_edge("A", "B")]
        api = [self._api_edge("REIAGRO", "REIAGROLTD", "2009-02-05")]
        api[0]["split_bridge"] = {"ex_date": "2009-02-05", "face_value_from": 10.0,
                                  "face_value_to": 1.0}
        merged = identity.merge_identity_edges(official, api)
        self.assertEqual(len(merged["refused"]), 1)
        refusal = merged["refused"][0]
        self.assertIn(identity.REFUSAL_EXCHANGE_RATIO, refusal["refusal_codes"])
        self.assertIn("exchange ratio", refusal["reason"])
        api_edge = next(e for e in merged["edges"]
                        if e["edge_source"] == "official_corporate_actions_api")
        self.assertFalse(api_edge["remap_admissible"])
        self.assertNotIn("share_count_multiplier", api_edge)
        # The official edge is untouched by the merge.
        official_edge = next(e for e in merged["edges"]
                             if e["edge_source"] == "official_symbolchange_row")
        self.assertEqual(official_edge["old_symbol"], "A")
        self.assertEqual(official_edge["new_symbol"], "B")


class MergedIdentityLoaderTests(unittest.TestCase):
    """The merged loader the runners consume: provenance plus a fail-closed gate."""

    def _official_edge(self, old="OLD", new="NEW", effective="2005-01-01"):
        edges, _ = identity.build_edges([change(old, new, effective)], obs())
        return edges[0]

    def _api_edge(self, old="OLDSYM", new="NEWSYM", effective="2008-04-01", **extra):
        edge = {
            "edge_id": f"API|{old}->{new}|{effective}",
            "old_symbol": old,
            "new_symbol": new,
            "company_name": "Acme Industries Limited",
            "company_name_normalised": "acme industries limited",
            "effective_date": effective,
            "evidence_class": "same_isin",
            "isin_old": "INE000A01001",
            "isin_new": "INE000A01001",
            "derived_from": "nse_corporate_actions_api",
            "from_symbolchange_csv": False,
            "verification": "official_nse_corporate_actions_api",
        }
        edge.update(extra)
        return edge

    def test_merged_loader_returns_official_and_api_edges_with_distinguishable_source(self):
        official = [self._official_edge("A", "B")]
        api = [self._api_edge("OLDSYM", "NEWSYM")]
        merged = identity.merge_identity_edges(official, api)
        self.assertEqual([edge["edge_source"] for edge in merged["edges"]],
                         ["official_symbolchange_row", "official_corporate_actions_api"])
        # The official edge keeps every one of its own fields.
        for key, value in official[0].items():
            self.assertEqual(merged["edges"][0][key], value, key)
        # The API edge keeps its own evidence and its own citation fields.
        api_edge = merged["edges"][1]
        self.assertEqual(api_edge["evidence_class"], "same_isin")
        self.assertEqual(api_edge["isin_old"], "INE000A01001")
        self.assertEqual(api_edge["isin_new"], "INE000A01001")
        self.assertFalse(api_edge["from_symbolchange_csv"])
        self.assertEqual(merged["summary"]["edges_by_source"],
                         {"official_symbolchange_row": 1,
                          "official_corporate_actions_api": 1})

    def test_api_edge_that_would_create_a_branch_ambiguity_is_refused(self):
        # The predecessor already has another successor: the API step is not a
        # single unambiguous successor step, so it may not remap a key.
        official = [self._official_edge("OLD", "OTHER")]
        api = [self._api_edge("OLD", "NEW")]
        merged = identity.merge_identity_edges(official, api)
        self.assertEqual(len(merged["refused"]), 1)
        self.assertIn(identity.REFUSAL_BRANCH, merged["refused"][0]["refusal_codes"])
        api_edge = merged["edges"][-1]
        self.assertFalse(api_edge["remap_admissible"])
        self.assertNotIn(api_edge, merged["remap_edges"])

    def test_api_edge_that_would_create_a_merge_ambiguity_is_refused(self):
        official = [self._official_edge("OTHER", "NEW")]
        api = [self._api_edge("OLD", "NEW")]
        merged = identity.merge_identity_edges(official, api)
        self.assertEqual(len(merged["refused"]), 1)
        self.assertIn(identity.REFUSAL_MERGE, merged["refused"][0]["refusal_codes"])
        self.assertFalse(merged["edges"][-1]["remap_admissible"])
        self.assertNotIn(merged["edges"][-1], merged["remap_edges"])

    def test_api_edge_across_a_symbol_reuse_is_refused(self):
        api = [self._api_edge("REUSE", "NEWSYM"),
               self._api_edge("REUSE", "OTHERSYM",
                              company_name="Other Company Limited",
                              company_name_normalised="other company limited",
                              isin_old="INE000A01009", isin_new="INE000A01009")]
        merged = identity.merge_identity_edges([], api)
        codes = {code for entry in merged["refused"] for code in entry["refusal_codes"]}
        self.assertIn(identity.REFUSAL_REUSE, codes)

    def test_undated_api_edge_is_refused(self):
        api = [self._api_edge("OLDSYM", "NEWSYM", effective=None)]
        merged = identity.merge_identity_edges([], api)
        self.assertEqual(len(merged["refused"]), 1)
        self.assertIn(identity.REFUSAL_UNDATED, merged["refused"][0]["refusal_codes"])

    def test_real_rei_agro_edge_is_refused_because_the_handover_ratio_is_undocumented(self):
        repo = Path(__file__).resolve().parents[1]
        if not (repo / "data/processed/identity/api_identity_edges.jsonl").exists():
            self.skipTest("the API identity layer is not built in this repository")
        merged = identity.load_merged_identity_edges(repo / "data")
        edges = [edge for edge in merged["edges"]
                 if (edge.get("old_symbol"), edge.get("new_symbol"))
                 == ("REIAGRO", "REIAGROLTD")]
        self.assertEqual(len(edges), 1)
        edge = edges[0]
        self.assertEqual(edge["edge_source"], "official_corporate_actions_api")
        self.assertFalse(edge["remap_admissible"])
        self.assertIn(identity.REFUSAL_EXCHANGE_RATIO, edge["refusal_codes"])
        # The documented 10:1 split is dated 2009-02-05, which is NOT the
        # observed trading handover (2008-11-25): it is a later, separate
        # event and it says nothing about the handover's share count.
        self.assertEqual(edge["split_bridge"]["ex_date"], "2009-02-05")
        self.assertEqual(edge["remap_effective_date"], "2008-11-25")
        self.assertNotIn("share_count_multiplier", edge)
        self.assertNotIn(edge, merged["remap_edges"])

    def test_real_rei_agro_split_is_already_applied_by_the_action_audit(self):
        # The only documented split for this company is the 2009-02-05 10:1
        # face-value split, and the applied action audit already carries it.
        # It is dated on the successor's side of the handover, so it is not
        # evidence for the handover's exchange ratio.
        repo = Path(__file__).resolve().parents[1]
        audit = repo / "data/processed/adjusted/action_audit.parquet"
        if not audit.exists():
            self.skipTest("the applied action audit is not built in this repository")
        quoted = str(audit.resolve()).replace("\\", "/")
        con = duckdb.connect()
        try:
            rows = con.execute(
                "SELECT symbol, series, CAST(ex_date AS DATE), share_multiplier, decision "
                f"FROM read_parquet('{quoted}') WHERE symbol IN ('REIAGRO','REIAGROLTD') "
                "AND decision = 'applied'").fetchall()
        finally:
            con.close()
        self.assertEqual(rows, [("REIAGROLTD", "EQ", date(2009, 2, 5), 10.0, "applied")])


if __name__ == "__main__":
    unittest.main()
