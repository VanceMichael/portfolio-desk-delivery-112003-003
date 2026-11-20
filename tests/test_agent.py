"""代办端到端演练：只通过网页交互完成领取、交接与投递。"""
import hashlib
import tempfile
import unittest
from pathlib import Path
from threading import Thread

from portfolio_portal import create_source

ROOT = Path(__file__).resolve().parent.parent


class AgentFlowTest(unittest.TestCase):
    def setUp(self):
        self.server = create_source(root=ROOT)
        self.thread = Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.thread.join, 2)
        self.addCleanup(self.server.shutdown)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def run_agent(self):
        from agent import Agent
        workdir = Path(self.tmp.name) / "run"
        agent = Agent(
            self.base, workdir,
            ROOT / "data" / "dispatch.json",
            ROOT / "data" / "mailroom.json",
        )
        return agent.run()

    def test_full_flow_respects_handoff_gate_deadlines_and_budget(self):
        report = self.run_agent()

        # 第 5 轮全部到席，且没有任何失败/超时异常。
        self.assertEqual(report["final_round"], 5)
        self.assertEqual(report["exceptions"], [])

        credentials = {(c["permit"], c["work"]): c for c in report["credentials"]}
        # 匿名 W17A 两席均由种子投递签收，代办不重复领取；实际只领四件。
        self.assertEqual(
            set(credentials),
            {("P20", "W32N"), ("P15", "W17N"),
             ("P15", "W17Appendix"), ("P11", "W17Preview")},
        )
        # 临近截止的 J32 与新席正文在第 2 轮先取，附页与预览第 3 轮取。
        self.assertEqual(credentials[("P20", "W32N")]["round"], 2)
        self.assertEqual(credentials[("P15", "W17N")]["round"], 2)
        self.assertEqual(credentials[("P15", "W17Appendix")]["round"], 3)
        self.assertEqual(credentials[("P11", "W17Preview")]["round"], 3)
        # 每份下载凭据都与门户 TXT 原件逐字节一致。
        for (permit, work), item in credentials.items():
            original = (ROOT / "originals" / "works" / f"{work}.txt").read_bytes()
            self.assertEqual(
                hashlib.sha256(original).hexdigest(), item["sha256"],
                f"{permit}/{work} 摘要与门户原件不一致",
            )
            self.assertEqual(Path(item["path"]).read_bytes(), original)

        deliveries = {d["delivery"]: d for d in report["deliveries"]}
        tx2 = {m["name"]: m for m in deliveries["TX2"]["members"]}
        # 换席交接：留存的旧附页中转撤回，已到旧席的正文等席位清除回条。
        self.assertEqual(tx2["W17OldAppendix.txt"]["state"], "中转已撤")
        self.assertEqual(tx2["W17N.txt"]["state"], "席位已清除")

        handoff = report["handoffs"]["D102"]
        self.assertTrue(handoff["gate_open"])
        actions = {(t["delivery_id"], t["action"]) for t in report["tickets"]}
        self.assertIn(("TX2", "purge"), actions)
        self.assertIn(("TX2", "cancel"), actions)
        # 新席实名包第 3 轮才发出，晚于第 2 轮的撤回/清除手续。
        d102_ticket = next(t for t in report["tickets"] if t["delivery_id"] == "TX-D102")
        self.assertEqual(d102_ticket["submitted_round"], 3)
        # lost_reply：第 3 轮提交的回条第 4 轮才查得到，但成员已在投递中可查。
        self.assertEqual(d102_ticket["receipt_round"], 4)

        # 同一实名场次正文与附页齐件后才送，且最终都到新席。
        d102 = {m["name"]: m for m in deliveries["TX-D102"]["members"]}
        self.assertEqual(set(d102), {"W17N.txt", "W17Appendix.txt"})
        for member in d102.values():
            self.assertEqual(member["state"], "席位已收")
        self.assertEqual(
            d102["W17N.txt"]["sha256"], credentials[("P15", "W17N")]["sha256"])
        self.assertEqual(
            d102["W17Appendix.txt"]["sha256"],
            credentials[("P15", "W17Appendix")]["sha256"],
        )

        # 匿名包允许先交部分再追加：TX1 保留种子 W17A，只追加 W17Preview。
        tx1 = {m["name"]: m for m in deliveries["TX1"]["members"]}
        self.assertEqual(
            {name: m["state"] for name, m in tx1.items()},
            {"W17A.txt": "席位已收", "W17Preview.txt": "席位已收"},
        )
        # 已签收成员不补送：F4 的 TX3 维持单件，没有追加动作。
        tx3 = deliveries["TX3"]["members"]
        self.assertEqual([m["name"] for m in tx3], ["W17A.txt"])

        # 企业志愿者席位视图。
        view = report["desk_view"]
        e2 = {m["member"] for m in view["E2"]["可看"]}
        self.assertEqual(e2, {"W32N.txt", "W17N.txt", "W17Appendix.txt"})
        self.assertEqual(view["E2"]["待补"], [])
        self.assertEqual(
            [m["member"] for m in view["E1"]["已清除"]], ["W17N.txt"])
        self.assertEqual(view["E1"]["需人工联络"], [])

        # 全程没有把“暂缺/时限”误记成拒绝授权类异常。
        self.assertTrue(all(o["kind"] != "拒绝授权" for o in report["observations"]))

    def test_rerun_adopts_existing_deliveries_without_duplicate_actions(self):
        first = self.run_agent()
        self.assertEqual(first["final_round"], 5)

        # 同一服务上重启代办：固定投递号回查后应接管已有投递，
        # 不再产生任何领取凭据或中转手续，并在当前轮次立即收敛。
        second = self.run_agent()
        self.assertEqual(second["final_round"], first["final_round"])
        self.assertEqual(second["credentials"], [])
        self.assertEqual(second["tickets"], [])
        self.assertEqual(second["exceptions"], [])
        self.assertEqual(
            sorted(d["delivery"] for d in second["deliveries"]),
            sorted(d["delivery"] for d in first["deliveries"]),
        )
        # 已到新席的成员没有被重复投递。
        d102 = next(d for d in second["deliveries"] if d["delivery"] == "TX-D102")
        self.assertEqual(
            sorted(m["name"] for m in d102["members"]),
            ["W17Appendix.txt", "W17N.txt"],
        )
        for member in d102["members"]:
            self.assertEqual(member["state"], "席位已收")


if __name__ == "__main__":
    unittest.main()
