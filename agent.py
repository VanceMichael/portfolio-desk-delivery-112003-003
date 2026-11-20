"""双选会作品领取与会务投递代办。

本代办只通过合成站点实际呈现的网页与交互完成工作：

* 从“会务交付单”页读取当前轮次可见的交付单；
* 从“席位授权 / 领取弹窗”核对授权编号、修订、用途与可领清单，
  按弹窗公示的 SHA-256 摘要校验下载到的 TXT 原件；
* 通过“场馆中转台”逐成员投递、中转撤回、联系席位清除，并按投递号、
  办理号回查每个成员实际到了哪一站；
* 每轮先处理换席交接（旧单成员未终结前，新席同一学生的实名包不发送），
  再按截止轮次与每轮领取名额安排领取与投递，最后生成会务/企业两侧视图。

用法::

    python3 agent.py --base http://127.0.0.1:8040 \
        --workdir runs/demo \
        --dispatch data/dispatch.json \
        --mailroom data/mailroom.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

# 中转台成员状态（网页原文）。
HOLD = "中转留存"
SEAT_RECEIVED = "席位已收"
CANCELLED = "中转已撤"
PURGE_PENDING = "待席位处理"
SEAT_CLEARED = "席位已清除"
TERMINAL_STATES = {CANCELLED, SEAT_CLEARED}
SEAT_VISIBLE_STATES = {SEAT_RECEIVED}


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass
class Member:
    """中转台某投递号下的一个成员文件。"""

    name: str
    state: str
    digest: str = ""
    observed_round: int = 0


@dataclass
class Delivery:
    """中转台投递号及其成员的最近一次回查结果。"""

    delivery_id: str
    request_id: str = ""
    permit_id: str = ""
    members: dict[str, Member] = field(default_factory=dict)


@dataclass
class Ticket:
    """一次中转台表单办理的办理号与回条。"""

    ticket: str
    delivery_id: str
    action: str
    summary: str
    submitted_round: int
    receipt_round: int | None = None


class PortalUI:
    """封装门户网页上的全部交互，页面结构均以可见角色/标签定位。"""

    def __init__(self, base: str, workdir: Path, timeout_ms: int = 10000):
        self.base = base.rstrip("/")
        self.workdir = workdir
        self.download_dir = workdir / "downloads"
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.timeout_ms = timeout_ms
        self._playwright = None
        self._browser = None
        self.page = None

    def __enter__(self) -> "PortalUI":
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch()
        self.page = self._browser.new_page(accept_downloads=True)
        self.page.set_default_timeout(self.timeout_ms)
        return self

    def __exit__(self, *exc) -> None:
        if self._browser:
            self._browser.close()
        if self._playwright:
            self._playwright.stop()

    # ---------- 通用 ----------

    def open_home(self) -> None:
        # 关闭任何仍开在上一轮授权上的领取弹窗，避免 expect_popup 抓到旧弹窗。
        for other in self.page.context.pages:
            if other is not self.page:
                try:
                    other.close()
                except Exception:
                    pass
        self.page.goto(self.base + "/")
        self.page.get_by_role("heading", name="作品领取").wait_for()

    def home_round_and_budget(self) -> tuple[int, int]:
        """读取主页公示的当前公告轮次与本轮剩余领取次数。"""
        self.open_home()
        text = self.page.locator("body").inner_text()
        round_no = int(re.search(r"当前公告轮次\s*(\d+)", text).group(1))
        budget = int(re.search(r"剩余领取次数\s*(\d+)", text).group(1))
        return round_no, budget

    def advance_clock(self, target_round: int) -> None:
        self.page.get_by_role("link", name="场馆时钟").click()
        self.page.get_by_label("公告轮次").fill(str(target_round))
        self.page.get_by_role("button", name="推进公告").click()
        self.open_home()

    def heading_text(self) -> str:
        return self.page.get_by_role("heading").first.inner_text()

    # ---------- 会务交付单 ----------

    def read_schedule(self) -> list[dict]:
        self.page.get_by_role("link", name="会务交付单").click()
        table = self.page.get_by_role("table", name="交付单")
        headers = [th.inner_text() for th in table.locator("thead th").all()]
        rows = []
        for tr in table.locator("tbody tr").all():
            cells = [td.inner_text() for td in tr.locator("td").all()]
            row = dict(zip(headers, cells))
            rows.append(
                {
                    "request": row["交付单"],
                    "student": row["学生"],
                    "desk": row["席位"],
                    "slot": row["场次"],
                    "use": row["用途"],
                    "deadline": int(row["截止轮次"]),
                    "replaces": row["替代单"] or None,
                    "required": [item for item in row["必需件"].split(",") if item],
                }
            )
        # 网页只展示当前轮次已生效的交付单；起始轮次由代办按交付单文件补齐。
        return rows
        return rows

    # ---------- 授权页与领取弹窗 ----------

    def open_permission(self, student: str, desk: str, slot: str, use: str,
                        release: int) -> dict | None:
        """按学生/席位/场次/用途/公告轮次打开授权页，返回授权快照。

        弹窗可能停留在上一轮授权上，因此授权页与领取弹窗分别核对，
        弹窗授权编号不一致时使用“刷新授权与领取入口”后重开。
        """
        self.open_home()
        self.page.get_by_label("学生编号").select_option(student)
        self.page.get_by_label("企业席位").select_option(desk)
        self.page.get_by_label("面谈场次").select_option(slot)
        self.page.get_by_label("作品用途").select_option(use)
        self.page.get_by_label("授权公告轮次").select_option(str(release))
        self.page.get_by_role("button", name="查看授权").click()
        if self.heading_text() == "当前无资料":
            return None
        context = self._read_dl()
        allowed = []
        for li in self.page.get_by_role("list", name="已授权作品").locator("li").all():
            match = re.fullmatch(r"(\S+)\s+可领至\s+(\d+)", li.inner_text())
            allowed.append({"work": match.group(1), "until": int(match.group(2))})
        return {
            "permit": context["授权编号"],
            "desk": context["席位"],
            "slot": context["场次"],
            "use": use,
            "rev": int(context["修订"]),
            "student": context["学生"],
            "allowed": allowed,
            "release": release,
        }

    def _read_dl(self) -> dict[str, str]:
        keys = [dt.inner_text() for dt in self.page.locator("dt").all()]
        values = [dd.inner_text() for dd in self.page.locator("dd").all()]
        return dict(zip(keys, values))

    def open_pickup(self, permit_snapshot: dict) -> dict:
        """在授权页打开领取弹窗并核对授权编号。

        弹窗可能仍开在上一轮授权上；授权编号不一致时关闭弹窗，
        使用“刷新授权与领取入口”后重开。中转台等导航会带走主页，
        因此先按授权快照重新打开授权页。
        """
        expected_permit = permit_snapshot["permit"]
        if self.heading_text() != "席位授权" or self._read_dl()["授权编号"] != expected_permit:
            self.open_permission(
                permit_snapshot["student"], permit_snapshot["desk"],
                permit_snapshot["slot"], permit_snapshot["use"], permit_snapshot["release"],
            )
        for _ in range(3):
            with self.page.expect_popup() as event:
                self.page.get_by_role("link", name="打开领取页").click()
            popup = event.value
            popup.wait_for_load_state()
            context = self._read_dl_on(popup)
            if context["授权编号"] == expected_permit:
                items = {}
                for li in popup.get_by_role("list", name="可下载作品").locator("li").all():
                    work = li.get_by_role("link").inner_text()
                    digest = re.search(r"原件摘要\s+([0-9a-f]{64})", li.inner_text()).group(1)
                    items[work] = digest
                return {"popup": popup, "permit": expected_permit, "items": items}
            popup.close()
            # 弹窗仍是上一轮授权：刷新入口后再开（主页仍是授权页）。
            self.page.get_by_role("link", name="刷新授权与领取入口").click()
        raise RuntimeError(f"领取弹窗授权始终不是 {expected_permit}")

    @staticmethod
    def _read_dl_on(page) -> dict[str, str]:
        keys = [dt.inner_text() for dt in page.locator("dt").all()]
        values = [dd.inner_text() for dd in page.locator("dd").all()]
        return dict(zip(keys, values))

    def download_work(self, pickup: dict, work: str) -> tuple[Path, str]:
        """在领取弹窗下载作品，保存为 作品代号.txt 并核对网页公示摘要。"""
        popup = pickup["popup"]
        target_dir = self.download_dir / pickup["permit"]
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{work}.txt"
        try:
            with popup.expect_download(timeout=self.timeout_ms) as event:
                popup.get_by_role("link", name=work).click()
            download = event.value
        except PlaywrightTimeoutError:
            # 名额用尽等情况下站点返回提示页而不是下载。
            raise DownloadRefused(work, self._heading_on(popup))
        download.save_as(target)
        if download.suggested_filename != f"{work}.txt":
            raise DownloadRefused(work, f"下载文件名异常：{download.suggested_filename}")
        digest = sha256_file(target)
        if digest != pickup["items"][work]:
            raise DownloadRefused(work, "下载摘要与领取页公示摘要不一致")
        return target, digest

    def close_pickup(self, pickup: dict) -> None:
        try:
            pickup["popup"].close()
        except Exception:
            pass

    @staticmethod
    def _heading_on(page) -> str:
        try:
            return page.get_by_role("heading").first.inner_text()
        except Exception:
            return page.title()

    # ---------- 场馆中转台 ----------

    def counter_lookup(self, delivery_id: str, ticket: str = "") -> dict:
        """按投递号（可带办理号）回查，返回办理回条与成员状态表。"""
        self.page.get_by_role("link", name="场馆中转台").click()
        self.page.get_by_label("查询投递号").fill(delivery_id)
        if ticket:
            self.page.get_by_label("查询办理号").fill(ticket)
        self.page.get_by_role("button", name="查询", exact=True).click()
        receipt = None
        for paragraph in self.page.locator("main p").all():
            text = paragraph.inner_text()
            if text.startswith("办理号 ") or text in {"办理回条未到", "未查到投递"}:
                receipt = text
        members = []
        table = self.page.get_by_role("table", name="投递成员")
        if table.count():
            for tr in table.locator("tbody tr").all():
                cells = [td.inner_text() for td in tr.locator("td").all()]
                members.append(Member(name=cells[0], state=cells[1], digest=cells[2]))
        context = ""
        paragraphs = [p.inner_text() for p in self.page.locator("main p").all()]
        for text in paragraphs:
            if re.fullmatch(r"TX\S*\s+D\S+\s+P\S+", text):
                context = text
        return {"receipt": receipt, "members": members, "context": context}

    def counter_submit(self, action: str, ticket: str, delivery_id: str,
                       request_id: str = "", permit_id: str = "",
                       member: str = "", files: list[Path] | None = None) -> str:
        """向中转台提交一张表单，返回落地页标题（回条可能下一轮才可见）。"""
        self.page.get_by_role("link", name="场馆中转台").click()
        self.page.get_by_label("手续").select_option(action)
        self.page.get_by_label("办理号", exact=True).fill(ticket)
        self.page.get_by_label("投递号", exact=True).fill(delivery_id)
        if request_id:
            self.page.get_by_label("交付单", exact=True).fill(request_id)
        if permit_id:
            self.page.get_by_label("授权编号", exact=True).fill(permit_id)
        if member:
            self.page.get_by_label("需处理的成员", exact=True).fill(member)
        if files:
            self.page.get_by_label("作品文件").set_input_files([str(p) for p in files])
        self.page.get_by_role("button", name="交中转台").click()
        self.page.wait_for_load_state()
        return self.heading_text()


class DownloadRefused(RuntimeError):
    """下载未取得原件（名额、时限或页面提示），不等于学生拒绝授权。"""


@dataclass
class Credential:
    """一份实际下载凭据：作品、授权上下文、文件与摘要。"""

    work: str
    permit: str
    desk: str
    slot: str
    use: str
    rev: int
    path: str
    sha256: str
    round: int


class Agent:
    """按轮次推进的领取/投递代办。"""

    def __init__(self, base: str, workdir: Path, dispatch_path: Path,
                 mailroom_path: Path, max_round: int = 8):
        self.ui = PortalUI(base, workdir)
        self.workdir = workdir
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.dispatch_path = dispatch_path
        self.mailroom_path = mailroom_path
        self.max_round = max_round

        self.orders: list[dict] = []
        self.orders_by_id: dict[str, dict] = {}
        self.deliveries: dict[str, Delivery] = {}
        self.permits: dict[tuple, dict] = {}
        self.credentials: list[Credential] = []
        self.tickets: list[Ticket] = []
        self.exceptions: list[dict] = []
        self.observations: list[dict] = []
        self.handoffs: dict[str, dict] = {}
        self._ticket_seq = 0
        self._missing_logged: set[tuple[str, str]] = set()

    # ---------- 基础数据 ----------

    def load_seed(self) -> None:
        """交付单与种子投递来自排班组文件；授权与清单只认网页。"""
        self.orders = json.loads(self.dispatch_path.read_text(encoding="utf-8"))
        self.orders_by_id = {order["request"]: order for order in self.orders}
        for item in json.loads(self.mailroom_path.read_text(encoding="utf-8")):
            delivery = Delivery(
                delivery_id=item["id"], request_id=item["request"], permit_id=item["permit"],
                members={
                    m["name"]: Member(name=m["name"], state=m["state"])
                    for m in item["members"]
                },
            )
            self.deliveries[item["id"]] = delivery

    def new_ticket(self, round_no: int, action: str, delivery_id: str, summary: str) -> str:
        self._ticket_seq += 1
        return f"H{round_no:02d}{self._ticket_seq:02d}"

    def record_exception(self, round_no: int, category: str, subject: str, detail: str) -> None:
        self.exceptions.append(
            {"round": round_no, "category": category, "subject": subject, "detail": detail}
        )

    def delivery_for_order(self, request_id: str) -> Delivery | None:
        for delivery in self.deliveries.values():
            if delivery.request_id == request_id:
                return delivery
        return None

    def refresh_delivery(self, delivery_id: str, round_no: int) -> Delivery:
        result = self.ui.counter_lookup(delivery_id)
        delivery = self.deliveries.setdefault(
            delivery_id, Delivery(delivery_id=delivery_id)
        )
        context_parts = result["context"].split()
        if len(context_parts) == 3:
            _, request_id, permit_id = context_parts
            delivery.request_id, delivery.permit_id = request_id, permit_id
        for member in result["members"]:
            member.observed_round = round_no
            delivery.members[member.name] = member
        return delivery

    @staticmethod
    def delivery_id_for(request_id: str) -> str:
        # 代办为每张无既有投递的交付单使用固定投递号，便于重复运行时回查接管。
        return f"TX-{request_id}"

    def discover_deliveries(self, schedule: list[dict], round_no: int) -> None:
        """按固定投递号回查中转台，接管之前运行已办理但本进程未知的投递。

        中转台只按投递号回查；固定命名使重启代办后仍能发现自己创建的
        投递，避免对已收件成员重复投递。
        """
        for order in schedule:
            if self.delivery_for_order(order["request"]) is not None:
                continue
            delivery_id = self.delivery_id_for(order["request"])
            result = self.ui.counter_lookup(delivery_id)
            if result["members"] or result["context"]:
                self.refresh_delivery(delivery_id, round_no)

    def refresh_all(self, round_no: int) -> None:
        for delivery_id in list(self.deliveries):
            self.refresh_delivery(delivery_id, round_no)

    def permit_for(self, order: dict, round_no: int) -> dict | None:
        """取该席位上下文在指定公告轮次下的授权（按轮次缓存）。

        授权公告独立于领取弹窗更新：同一席位在不同轮次可能是不同授权
        编号与修订（如 E1 实名第 1 轮 P12、第 2 轮起 P14），必须按
        学生/席位/场次/用途/公告轮次分别核对。
        """
        key = (order["student"], order["desk"], order["slot"], order["use"], round_no)
        if key not in self.permits:
            permit = self.ui.open_permission(
                order["student"], order["desk"], order["slot"], order["use"], round_no)
            if permit is None:
                self.record_exception(round_no, "授权缺失", order["request"],
                                      f"网页查不到 {key[:4]} 在第 {round_no} 轮的授权公告")
                self.permits[key] = {}
                return None
            self.permits[key] = permit
        return self.permits[key] or None

    # ---------- 第一步：换席交接 ----------

    def handle_handoffs(self, schedule: list[dict], round_no: int) -> None:
        """替代单出现后，先终结旧实名单在中转台的全部成员。"""
        for order in schedule:
            replaces = order.get("replaces")
            if not replaces:
                continue
            handoff = self.handoffs.setdefault(
                order["request"],
                {"replaces": replaces, "new_request": order["request"],
                 "tickets": [], "gate_open": False},
            )
            old_order = self.orders_by_id[replaces]
            old_deliveries = [
                d for d in self.deliveries.values() if d.request_id == replaces
            ]
            for delivery in old_deliveries:
                self.refresh_delivery(delivery.delivery_id, round_no)
                for member_name, member in delivery.members.items():
                    if member.state in TERMINAL_STATES:
                        continue
                    if member.state == HOLD:
                        ticket = self.new_ticket(round_no, "cancel", delivery.delivery_id,
                                                 f"中转撤回 {member_name}")
                        heading = self.ui.counter_submit(
                            "cancel", ticket, delivery.delivery_id,
                            member=member_name,
                        )
                        self.tickets.append(Ticket(
                            ticket, delivery.delivery_id, "cancel", heading, round_no))
                        handoff["tickets"].append(ticket)
                    elif member.state == SEAT_RECEIVED:
                        ticket = self.new_ticket(round_no, "purge", delivery.delivery_id,
                                                 f"联系席位清除 {member_name}")
                        heading = self.ui.counter_submit(
                            "purge", ticket, delivery.delivery_id,
                            member=member_name,
                        )
                        self.tickets.append(Ticket(
                            ticket, delivery.delivery_id, "purge", heading, round_no))
                        handoff["tickets"].append(ticket)
                    elif member.state == PURGE_PENDING:
                        # 已联系，等席位下一轮回报。
                        pass
                    else:
                        self.record_exception(
                            round_no, "交接状态不明", delivery.delivery_id,
                            f"{member_name} 状态 {member.state}",
                        )
            # 全部成员终结（中转已撤/席位已清除）后，新席实名包才可发送。
            # 刷新网页状态，使同轮到达的席位清除回条立刻生效。
            for delivery in old_deliveries:
                self.refresh_delivery(delivery.delivery_id, round_no)
            terminal = all(
                member.state in TERMINAL_STATES
                for delivery in old_deliveries
                for member in delivery.members.values()
            )
            if terminal and old_deliveries:
                handoff["gate_open"] = True
                handoff["gate_evidence"] = [
                    {"delivery": delivery.delivery_id,
                     "members": [
                         {"name": member.name, "state": member.state,
                          "observed_round": member.observed_round}
                         for member in delivery.members.values()]}
                    for delivery in old_deliveries
                ]
            handoff["old_order"] = {
                "request": replaces, "desk": old_order["desk"], "slot": old_order["slot"],
                "student": old_order["student"],
            }

    # ---------- 第二步：领取 ----------

    def plan_candidates(self, schedule: list[dict], round_no: int) -> list[dict]:
        """构造本轮可领候选，按最早有效截止轮次排序，含为后续轮次预领。

        * 被替代的旧单不再领取（旧件只走撤回/清除交接，不重领重投）；
        * 同一授权上下文中的同一作品只领一次，已在投递中或已下载的跳过；
        * 已签收成员不补送；匿名包可先交允许的部分，后续再追加。
        """
        replaced = {order["replaces"] for order in schedule if order.get("replaces")}
        candidates = []
        seen: set[tuple[str, str]] = set()
        # 同一授权上下文（授权编号 + 作品）只领一次；不同授权上下文即使
        # 作品同名也是各自独立的领取凭据，不能互相替代。
        downloaded = {(c.permit, c.work) for c in self.credentials}
        for order in schedule:
            if order["request"] in replaced:
                continue
            permit = self.permit_for(order, round_no)
            if not permit:
                continue
            pickup_meta = self.ui.open_pickup(permit)
            self.ui.close_pickup(pickup_meta)
            until_map = {item["work"]: item["until"] for item in permit["allowed"]}
            delivery = self.delivery_for_order(order["request"])
            # 网页成员文件名带 .txt，领取清单作品代号不带，比较时统一去扩展名。
            already = {name[:-4] for name in delivery.members} if delivery else set()
            for work, digest in pickup_meta["items"].items():
                if work in already or (permit["permit"], work) in downloaded:
                    continue
                key = (permit["permit"], work)
                if key in seen:
                    continue
                seen.add(key)
                effective_deadline = min(order["deadline"], until_map.get(work, order["deadline"]))
                candidates.append({
                    "order": order, "permit": permit, "work": work, "digest": digest,
                    "required": work in order["required"], "deadline": effective_deadline,
                })
            for work in until_map:
                if work not in pickup_meta["items"]:
                    log_key = (permit["permit"], work)
                    if log_key in self._missing_logged:
                        continue
                    self._missing_logged.add(log_key)
                    until = until_map[work]
                    if round_no >= until:
                        kind, detail = (
                            "已过领取时限",
                            f"公示可领至第 {until} 轮（不含），第 {round_no} 轮起停止提供，"
                            "属临时不在清单而非学生拒绝授权",
                        )
                    else:
                        kind, detail = (
                            "暂缺",
                            "授权允许但领取清单本轮暂缺（发布时刻/修订差异），不是拒绝授权",
                        )
                    self.observations.append({
                        "round": round_no, "kind": kind, "request": order["request"],
                        "permit": permit["permit"], "work": work, "detail": detail,
                    })
        # 必需件优先，再按有效截止轮次，临近截止且仍能交到对应席位者先领。
        candidates.sort(key=lambda c: (not c["required"], c["deadline"], c["work"]))
        return candidates

    def download_round(self, schedule: list[dict], round_no: int, budget: int) -> None:
        candidates = self.plan_candidates(schedule, round_no)
        open_popups: dict[str, dict] = {}
        used = 0
        try:
            for candidate in candidates:
                if used >= budget:
                    self.record_exception(
                        round_no, "本轮名额不足", candidate["work"],
                        f"交付单 {candidate['order']['request']} 起仍有候选未取得，下一轮继续",
                    )
                    break
                permit = candidate["permit"]
                pickup = open_popups.get(permit["permit"])
                if pickup is None:
                    pickup = self.ui.open_pickup(permit)
                    open_popups[permit["permit"]] = pickup
                try:
                    path, digest = self.ui.download_work(pickup, candidate["work"])
                except DownloadRefused as exc:
                    self.record_exception(
                        round_no, "领取未成功", candidate["work"], str(exc))
                    if "名额" in str(exc):
                        # 站点公示本轮名额已用完，后续链接同样取不了，直接停。
                        break
                    continue
                used += 1
                self.credentials.append(Credential(
                    work=candidate["work"], permit=permit["permit"], desk=permit["desk"],
                    slot=permit["slot"], use=permit["use"], rev=permit["rev"],
                    path=str(path), sha256=digest, round=round_no,
                ))
        finally:
            for pickup in open_popups.values():
                self.ui.close_pickup(pickup)

    # ---------- 第三步：投递 ----------

    def dispatch_round(self, schedule: list[dict], round_no: int) -> None:
        replaced = {order["replaces"] for order in schedule if order.get("replaces")}
        for order in schedule:
            if order["request"] in replaced:
                # 被替代的旧实名单不再发件，成员只经撤回/清除交接终结。
                continue
            permit = self.permit_for(order, round_no)
            if not permit:
                continue
            delivery = self.delivery_for_order(order["request"])
            existing_members = set(delivery.members) if delivery else set()
            paths = [
                Path(c.path) for c in self.credentials
                if c.permit == permit["permit"]
                and Path(c.path).name not in existing_members
            ]
            if not paths:
                continue
            if not (order["from"] <= round_no < order["deadline"]):
                # 窗口关闭后即使还有未投凭据也不能补发；只在确有新件时才记录。
                self.record_exception(
                    round_no, "接收窗已关闭", order["request"],
                    f"{[p.name for p in paths]} 未能在截止轮次 {order['deadline']} 前发出",
                )
                continue
            if order["use"] == "named":
                required_names = {f"{w}.txt" for w in order["required"]}
                have = {p.name for p in paths} | existing_members
                if not required_names.issubset(have):
                    # 同一实名场次的必要正文和附页都齐才可送。
                    self.observations.append({
                        "round": round_no, "kind": "实名齐件等待",
                        "request": order["request"],
                        "detail": f"尚缺 {sorted(required_names - have)}，本轮不发送",
                    })
                    continue
                handoff = self.handoffs.get(order["request"])
                if handoff and not handoff["gate_open"]:
                    self.observations.append({
                        "round": round_no, "kind": "等待旧席清除",
                        "request": order["request"],
                        "detail": "旧实名包成员未全部撤回/清除，新席实名包暂停发送",
                    })
                    continue
            delivery_id = delivery.delivery_id if delivery else self.delivery_id_for(order["request"])
            ticket = self.new_ticket(round_no, "dispatch", delivery_id,
                                     f"投递 {order['request']}")
            heading = self.ui.counter_submit(
                "dispatch", ticket, delivery_id,
                request_id=order["request"], permit_id=permit["permit"], files=paths,
            )
            self.tickets.append(Ticket(
                ticket, delivery_id, "dispatch", heading, round_no))
            if delivery is None:
                self.deliveries[delivery_id] = Delivery(
                    delivery_id=delivery_id, request_id=order["request"],
                    permit_id=permit["permit"],
                )
            self.refresh_delivery(delivery_id, round_no)

    # ---------- 回条跟进 ----------

    def follow_tickets(self, round_no: int) -> None:
        for ticket in self.tickets:
            if ticket.receipt_round is not None:
                continue
            result = self.ui.counter_lookup(ticket.delivery_id, ticket.ticket)
            if result["receipt"] and result["receipt"] != "办理回条未到":
                ticket.receipt_round = round_no
                ticket.summary = result["receipt"]

    # ---------- 完成判断 ----------

    def is_complete(self, schedule: list[dict], round_no: int) -> bool:
        replaced = {
            order["replaces"]
            for order in schedule if order.get("replaces")
        }
        for order in schedule:
            if order["request"] in replaced:
                # 被替代的旧单以交接终结为准，不再要求到席。
                continue
            delivery = self.delivery_for_order(order["request"])
            members = delivery.members if delivery else {}
            for work in order["required"]:
                member = members.get(f"{work}.txt")
                if not member or member.state not in SEAT_VISIBLE_STATES:
                    return False
        for handoff in self.handoffs.values():
            if not handoff["gate_open"]:
                return False
        # 中转台仍有在途成员（留存待转到席、待席位回报清除）时继续等。
        for delivery in self.deliveries.values():
            for member in delivery.members.values():
                if member.state in {HOLD, PURGE_PENDING}:
                    return False
        return True

    def read_schedule(self) -> list[dict]:
        """从网页读当前可见交付单，并用排班组文件补齐网页未展示的起始轮次。"""
        rows = self.ui.read_schedule()
        for row in rows:
            row["from"] = self.orders_by_id[row["request"]]["from"]
        return rows

    # ---------- 主流程 ----------

    def run(self) -> dict:
        with self.ui:
            self.load_seed()
            round_no, _ = self.ui.home_round_and_budget()
            schedule = self.read_schedule()
            while round_no <= self.max_round:
                self.refresh_all(round_no)
                schedule = self.read_schedule()
                self.discover_deliveries(schedule, round_no)
                self.handle_handoffs(schedule, round_no)
                _, budget = self.ui.home_round_and_budget()
                self.download_round(schedule, round_no, budget)
                self.dispatch_round(schedule, round_no)
                self.refresh_all(round_no)
                self.follow_tickets(round_no)
                if self.is_complete(schedule, round_no):
                    break
                round_no += 1
                self.ui.advance_clock(round_no)
            final_round, _ = self.ui.home_round_and_budget()
            self.refresh_all(final_round)
            self.discover_deliveries(schedule, final_round)
            self.follow_tickets(final_round)
            report = self.build_report(schedule, final_round)
            report_path = self.workdir / "report.json"
            report_path.write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return report

    # ---------- 报告 ----------

    def build_report(self, schedule: list[dict], round_no: int) -> dict:
        deliveries = []
        for delivery in sorted(self.deliveries.values(), key=lambda d: d.delivery_id):
            deliveries.append({
                "delivery": delivery.delivery_id,
                "request": delivery.request_id,
                "permit": delivery.permit_id,
                "members": [
                    {"name": m.name, "state": m.state, "sha256": m.digest,
                     "observed_round": m.observed_round}
                    for m in sorted(delivery.members.values(), key=lambda m: m.name)
                ],
            })
        desk_view = self._desk_view(schedule)
        return {
            "final_round": round_no,
            "permits": [self.permits[key] for key in sorted(self.permits) if self.permits[key]],
            "schedule": schedule,
            "deliveries": deliveries,
            "tickets": [vars(t) for t in self.tickets],
            "handoffs": self.handoffs,
            "credentials": [vars(c) for c in self.credentials],
            "desk_view": desk_view,
            "exceptions": self.exceptions,
            "observations": self.observations,
        }

    def _desk_view(self, schedule: list[dict]) -> dict:
        """企业志愿者按席位看到的可看/待补/待转入/需人工联络四类。

        “席位已清除”是旧席已经回报解决的回条，不再占用人工联络队列，
        单列为已清除记录；需人工联络只保留仍在“待席位处理”的成员。
        """
        view: dict[str, dict] = {}
        order_by_request = {o["request"]: o for o in schedule}
        for delivery in self.deliveries.values():
            order = order_by_request.get(delivery.request_id)
            if not order:
                continue
            desk = view.setdefault(order["desk"], {
                "可看": [], "待补": [], "待转入": [], "需人工联络": [], "已清除": []})
            for member in delivery.members.values():
                entry = {"delivery": delivery.delivery_id, "member": member.name,
                         "request": delivery.request_id, "state": member.state}
                if member.state == SEAT_RECEIVED:
                    desk["可看"].append(entry)
                elif member.state == HOLD:
                    desk["待转入"].append(entry)
                elif member.state == PURGE_PENDING:
                    desk["需人工联络"].append(entry)
                elif member.state == SEAT_CLEARED:
                    desk["已清除"].append(entry)
            for work in order["required"]:
                name = f"{work}.txt"
                if name not in delivery.members:
                    desk["待补"].append(
                        {"request": order["request"], "member": name,
                         "slot": order["slot"], "use": order["use"]})
        return view


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8040")
    parser.add_argument("--workdir", type=Path, default=Path("runs/demo"))
    parser.add_argument("--dispatch", type=Path, default=Path("data/dispatch.json"))
    parser.add_argument("--mailroom", type=Path, default=Path("data/mailroom.json"))
    parser.add_argument("--max-round", type=int, default=8)
    args = parser.parse_args()
    agent = Agent(args.base, args.workdir, args.dispatch, args.mailroom, args.max_round)
    report = agent.run()
    print(json.dumps({
        "final_round": report["final_round"],
        "deliveries": report["deliveries"],
        "exceptions": report["exceptions"],
        "report": str(args.workdir / "report.json"),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
