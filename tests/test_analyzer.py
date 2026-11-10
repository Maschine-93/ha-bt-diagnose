import sys, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "custom_components" / "bt_diagnose"))
from analyzer import AutoInfo, Point, TraceInfo, analyze  # noqa: E402
from describe import check_config  # noqa: E402

T0 = datetime(2026, 11, 8, 10, 0, 0, tzinfo=timezone.utc)
INFO = {"min_temp": 5.0, "hvac_modes": ["auto", "heat", "off"]}
OPT_ON = [{"trv": "t", "advanced": {"no_off_system_mode": True}}]
OPT_OFF = [{"trv": "t", "advanced": {"no_off_system_mode": False}}]


def p(sec, state, base=T0, **a):
    return Point(base + timedelta(seconds=sec), state, a)


def parking_case():
    bt = [p(-60, "heat", temperature=22.0, preset_mode="activity", current_temperature=22.9),
          p(-5, "heat", temperature=20.0, preset_mode="home", current_temperature=22.9),
          p(29, "off", temperature=20.0, preset_mode="home", current_temperature=22.9)]
    trv = [p(-60, "heat", temperature=21.5), p(-5, "heat", temperature=5.0)]
    return bt, trv


class AnalyzerTests(unittest.TestCase):
    def test_parking_with_option_on_is_actionable_with_solution(self):
        bt, trv = parking_case()
        f = analyze(bt, trv, OPT_ON, "WZ", "t", INFO)
        self.assertEqual(f[0].code, "off_parking_misread")
        self.assertTrue(f[0].action)
        self.assertIn("AUSSCHALTEN", f[0].recommendation)

    def test_parking_with_option_off_is_already_fixed(self):
        bt, trv = parking_case()
        f = analyze(bt, trv, OPT_OFF, "WZ", "t", INFO)
        self.assertEqual(f[0].code, "off_parking_misread")
        self.assertFalse(f[0].action)
        self.assertIn("behoben", f[0].title)

    def test_no_findings_when_stable(self):
        bt = [p(0, "heat", temperature=21.0), p(600, "heat", temperature=21.0)]
        self.assertEqual(analyze(bt, [p(0, "heat", temperature=21.0)], OPT_OFF, "WZ", "t", INFO), [])

    def test_option_on_alone_is_never_an_alarm(self):
        f = analyze([p(0, "heat")], [], OPT_ON, "SZ", "t", INFO)
        self.assertEqual([x.code for x in f], ["cfg_no_off_info"])
        self.assertFalse(f[0].action)

    def test_user_off_is_info(self):
        off = p(30, "off", temperature=21.0)
        off.has_context, off.user_id = True, "abc"
        f = analyze([p(0, "heat", temperature=21.0), off], [], OPT_OFF, "WZ", "t", INFO)
        self.assertEqual(f[0].code, "off_by_user")
        self.assertFalse(f[0].action)

    def test_window_off(self):
        f = analyze([p(0, "heat", temperature=21.0), p(30, "off", temperature=21.0, window_open=True)],
                    [], OPT_OFF, "WZ", "t", INFO)
        self.assertEqual(f[0].code, "off_window")

    def test_daily_same_time_off_is_schedule_not_alarm(self):
        bt = []
        for d in range(3):
            base = T0 + timedelta(days=d)
            bt += [p(0, "heat", base, temperature=21.0), p(30, "off", base, temperature=21.0)]
        f = analyze(bt, [], OPT_ON, "SZ", "t", INFO)
        self.assertIn("off_recurring", [x.code for x in f])
        self.assertFalse(any(x.action for x in f))

    def test_single_unexplained_off_is_hint_with_next_step(self):
        f = analyze([p(0, "heat", temperature=21.0), p(30, "off", temperature=21.0)],
                    [p(0, "heat", temperature=21.0)], OPT_OFF, "WZ", "t", INFO)
        self.assertEqual(f[0].code, "off_unexplained")
        self.assertEqual(f[0].certainty, "unklar")

    def test_trv_auto_while_bt_heat_is_actionable(self):
        f = analyze([p(0, "heat", temperature=21.0)], [p(0, "heat"), p(10, "auto"), p(400, "heat")],
                    OPT_OFF, "WZ", "t", INFO)
        self.assertEqual(f[0].code, "trv_auto_ignored")
        self.assertTrue(f[0].action)

    def test_single_unavailable_is_info_only(self):
        f = analyze([p(0, "heat"), p(10, "unavailable"), p(200, "heat")], [], OPT_OFF, "WZ", "t", INFO)
        self.assertEqual(f[0].code, "unavailable")
        self.assertFalse(f[0].action)

    def test_many_gaps_is_actionable(self):
        bt = [p(0, "heat")]
        for i in range(5):
            bt += [p(1000 * i + 10, "unavailable"), p(1000 * i + 400, "heat")]
        f = analyze(bt, [], OPT_OFF, "WZ", "t", INFO)
        self.assertTrue(f[0].action)


class AutomationTests(unittest.TestCase):
    def _off_at(self, hh, mm):
        base = datetime(2026, 11, 8, hh, mm, 0).astimezone()
        return [Point(base - timedelta(minutes=5), "heat", {"temperature": 21.0}),
                Point(base, "off", {"temperature": 21.0})], base

    def test_off_matched_to_automation_time_trigger(self):
        bt, _ = self._off_at(20, 0)
        a = AutoInfo("automation.nacht", "Nachtabschaltung", trigger_times=["20:00:00"],
                     touches_mode=True, actions_text=["setzt Modus „off“"])
        f = analyze(bt, [], OPT_ON, "SZ", "t", INFO, [a])
        codes = [x.code for x in f]
        self.assertIn("off_by_automation", codes)
        self.assertNotIn("off_unexplained", codes)
        self.assertFalse(any(x.action for x in f))

    def test_off_matched_to_recent_trace(self):
        bt, base = self._off_at(13, 17)
        a = AutoInfo("automation.x", "Test", touches_mode=True, actions_text=["schaltet aus"],
                     traces=[TraceInfo(base - timedelta(seconds=10), "finished")])
        f = analyze(bt, [], OPT_OFF, "SZ", "t", INFO, [a])
        self.assertEqual(f[0].code, "off_by_automation")

    def test_automation_that_only_turns_on_is_not_blamed_for_off(self):
        bt, _ = self._off_at(5, 0)
        a = AutoInfo("automation.presets", "Presets", trigger_times=["05:00:00"], touches_mode=True,
                     actions_text=["setzt Modus „heat“"])
        f = analyze(bt, [], OPT_OFF, "BAD", "t", INFO, [a])
        self.assertNotIn("off_by_automation", [x.code for x in f])

    def test_automation_with_errors_is_actionable(self):
        a = AutoInfo("automation.x", "Heizung X", traces=[
            TraceInfo(T0, "error", "Entity climate.foo not found", "action/0"), TraceInfo(T0, "finished")])
        f = analyze([p(0, "heat")], [], OPT_OFF, "WZ", "t", INFO, [a])
        self.assertEqual([x.code for x in f if x.code == "auto_info"], ["auto_info"])
        e = [x for x in f if x.code == "auto_errors"][0]
        self.assertTrue(e.action)
        self.assertIn("Entity climate.foo not found", e.explanation)
        self.assertIn("Verläufe", e.recommendation)
        self.assertTrue(e.change.startswith("Ja"))

    def test_repair_entry_becomes_problem(self):
        a = AutoInfo("automation.x", "Test", repairs=[{"key": "service_not_found", "detail": "climate.foo"}])
        f = analyze([p(0, "heat")], [], OPT_OFF, "WZ", "t", INFO, [a])
        r = [x for x in f if x.code == "auto_repair"][0]
        self.assertTrue(r.action)
        self.assertIn("Reparaturen", r.recommendation)
        self.assertIn("climate.foo", r.explanation)

    def test_unavailable_automation_is_actionable(self):
        a = AutoInfo("automation.x", "Kaputt", state="unavailable")
        f = analyze([p(0, "heat")], [], OPT_OFF, "WZ", "t", INFO, [a])
        self.assertTrue([x for x in f if x.code == "auto_unavailable"][0].action)

    def test_healthy_automation_listed_as_info(self):
        a = AutoInfo("automation.x", "Presets", touches_mode=True, actions_text=["setzt Preset"],
                     traces=[TraceInfo(T0, "finished")])
        f = analyze([p(0, "heat")], [], OPT_OFF, "WZ", "t", INFO, [a])
        i = [x for x in f if x.code == "auto_info"][0]
        self.assertFalse(i.action)
        self.assertEqual(i.title, "Presets")
        self.assertEqual(i.explanation, "")
        self.assertEqual(i.subject, "Presets")


BT = "climate.bt_sz"
CTX = {"exists": lambda e: e != "light.gibts_nicht",
       "bt": {BT: {"hvac_modes": ["heat", "off"], "preset_modes": ["none", "eco", "home"],
                   "min_temp": 5.0, "max_temp": 30.0}}}


def cfg(*actions, triggers=None):
    return {"triggers": triggers or [], "actions": list(actions)}


def call(svc, entity, **data):
    return {"action": svc, "target": {"entity_id": entity}, "data": data}


class ConfigCheckTests(unittest.TestCase):
    def codes(self, c):
        return [i["code"] for i in check_config(c, {BT}, {"climate.trv_sz"}, CTX)]

    def test_correct_automation_has_no_issues(self):
        c = cfg(call("climate.set_hvac_mode", BT, hvac_mode="off"),
                call("climate.set_preset_mode", BT, preset_mode="eco"),
                call("climate.set_temperature", BT, temperature=21))
        self.assertEqual(self.codes(c), [])

    def test_auto_mode_is_rejected_with_hint(self):
        c = cfg(call("climate.set_hvac_mode", BT, hvac_mode="auto"))
        issues = check_config(c, {BT}, set(), CTX)
        self.assertEqual(issues[0]["code"], "cfg_bad_mode")
        self.assertIn("auto", issues[0]["explanation"])

    def test_unknown_preset(self):
        self.assertEqual(self.codes(cfg(call("climate.set_preset_mode", BT, preset_mode="komfort"))),
                         ["cfg_bad_preset"])

    def test_temperature_out_of_range(self):
        self.assertEqual(self.codes(cfg(call("climate.set_temperature", BT, temperature=35))), ["cfg_bad_temp"])

    def test_missing_entity(self):
        c = cfg(call("light.turn_off", "light.gibts_nicht"))
        self.assertEqual(self.codes(c), ["cfg_missing_entity"])

    def test_direct_trv_is_only_a_recommendation(self):
        issues = check_config(cfg(call("climate.set_temperature", "climate.trv_sz", temperature=20)),
                              {BT}, {"climate.trv_sz"}, CTX)
        self.assertEqual(issues[0]["code"], "cfg_direct_trv")
        self.assertFalse(issues[0]["action"])

    def test_templated_values_are_not_judged(self):
        c = cfg(call("climate.set_preset_mode", BT, preset_mode="{{ preset }}"),
                call("climate.set_temperature", BT, temperature="{{ t }}"))
        self.assertEqual(self.codes(c), [])

    def test_issues_flow_into_findings(self):
        a = AutoInfo("automation.x", "Nacht", issues=check_config(
            cfg(call("climate.set_hvac_mode", BT, hvac_mode="auto")), {BT}, set(), CTX))
        f = analyze([p(0, "heat")], [], OPT_OFF, "SZ", "t", INFO, [a])
        bad = [x for x in f if x.code == "cfg_bad_mode"][0]
        self.assertTrue(bad.action)
        self.assertEqual(bad.subject, "Nacht")


if __name__ == "__main__":
    unittest.main()
