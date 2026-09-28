import unittest
from evaluate_acceptance import evaluate

class AcceptanceTests(unittest.TestCase):
    def cohort(self):
        return {**{k:"test" for k in ("run_id","commit","phone","browser","network","model_manifest")},
            "source":"real-phone-real-api",
            "turns":[{"id":i,"outcome":"pass","clock":"phone-monotonic",
                      "last_speech_sample_ms":0,"first_audible_video_ms":9000,
                      "audible_evidence":"fixture-only"} for i in range(20)]}
    def test_p95_is_nineteenth_and_not_max(self):
        data=self.cohort();data["turns"][-1]["first_audible_video_ms"]=11000
        result=evaluate(data)
        self.assertEqual(result["status"],"PASS")
        self.assertEqual(result["p95_seconds"],9)
        self.assertEqual(result["max_seconds"],11)
    def test_missing_failed_or_fake_evidence_cannot_pass(self):
        for mutation in (
            lambda x:x["turns"].pop(),
            lambda x:x["turns"][0].update(outcome="failed"),
            lambda x:x.update(source="desktop-emulation"),
            lambda x:x["turns"][0].pop("audible_evidence")
        ):
            data=self.cohort();mutation(data)
            self.assertEqual(evaluate(data)["status"],"FAIL")
if __name__=="__main__": unittest.main()
