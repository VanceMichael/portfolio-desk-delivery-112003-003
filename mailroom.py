"""独立的场馆中转台，展示逐成员的送达、撤回和清除回条。"""
from email import policy
from email.parser import BytesParser
from hashlib import sha256
from html import escape
from urllib.parse import urlencode


def unpack(headers, body):
    tag = headers.get("Content-Type", "")
    message = BytesParser(policy=policy.default).parsebytes(
        f"Content-Type: {tag}\r\nMIME-Version: 1.0\r\n\r\n".encode() + body
    )
    values, files = {}, []
    for part in message.iter_parts():
        key = part.get_param("name", header="Content-Disposition")
        if key == "files":
            name = part.get_filename()
            if name:
                files.append({"name": name, "sha256": sha256(part.get_payload(decode=True)).hexdigest()})
        else:
            values[key] = part.get_payload(decode=True).decode()
    return values, files


def create_mailroom(dispatches, seed, current):
    requests = {item["request"]: item for item in dispatches}
    deliveries = {item["id"]: {**item, "members": {m["name"]: dict(m) for m in item["members"]}}
                  for item in seed}
    tickets = {}

    class Room:
        def form(self):
            inputs = [("ticket", "办理号"), ("delivery", "投递号"), ("request", "交付单"),
                      ("permit", "授权编号"), ("member", "需处理的成员")]
            fields = "".join(f'<label>{label}<input name="{name}"></label>' for name, label in inputs)
            return '<form method="post" action="/counter" enctype="multipart/form-data">' + fields + (
                '<label>手续<select name="action"><option value="dispatch">投递</option>'
                '<option value="cancel">中转撤回</option><option value="purge">联系席位清除</option></select></label>'
                '<label>作品文件<input name="files" type="file" multiple></label><button>交中转台</button></form>'
            )

        def page(self, handler, query):
            turn = current()
            ticket = query.get("ticket", "")
            status = []
            if ticket:
                record = tickets.get(ticket)
                if record and record["visible"] <= turn:
                    status.append("<p>办理号 " + escape(ticket) + " " + escape(record["result"]) + "</p>")
                else:
                    status.append("<p>办理回条未到</p>")
            delivery = query.get("delivery", "")
            if delivery:
                record = deliveries.get(delivery)
                if record:
                    status.extend(self.details(record, turn))
                else:
                    status.append("<p>未查到投递</p>")
            query_form = ('<form action="/counter"><label>查询办理号<input name="ticket"></label>'
                          '<label>查询投递号<input name="delivery"></label><button>查询</button></form>')
            handler.page("场馆中转台", query_form + "".join(status) + self.form())

        def details(self, record, turn):
            context = "<p>" + escape(" ".join(record[key] for key in ("id", "request", "permit"))) + "</p>"
            rows = []
            for member in record["members"].values():
                state = member["state"]
                if state == "中转留存" and member["seat_at"] <= turn:
                    state = "席位已收"
                if state == "待席位处理" and member["purge_at"] <= turn:
                    state = "席位已清除"
                rows.append(f'<tr><td>{escape(member["name"])}</td><td>{state}</td>'
                            f'<td>{escape(member.get("sha256", ""))}</td></tr>')
            return [context, '<table aria-label="投递成员"><thead><tr><th>成员</th><th>状态</th><th>摘要</th></tr></thead>'
                    + "<tbody>" + "".join(rows) + "</tbody></table>"]

        def submit(self, handler, values, files):
            ticket = values.get("ticket", "")
            identifier = values.get("delivery", "")
            if not all((ticket, identifier)):
                return handler.page("单据字段不足", "", 422)
            turn = current()
            saved = tickets.get(ticket)
            if saved and saved["payload"] != [values, files]:
                return handler.page("办理号内容有冲突", "", 409)
            if saved:
                return handler.move("/counter?" + urlencode({"ticket": ticket, "delivery": identifier}))
            if values.get("action") == "dispatch":
                result, lost = self.dispatch(values, files, turn)
            else:
                result, lost = self.withdraw(values, turn)
            tickets[ticket] = {"visible": turn + int(lost), "result": result, "payload": [values, files]}
            return handler.move("/" if lost else "/counter?" + urlencode({"ticket": ticket, "delivery": identifier}))

        def dispatch(self, values, files, turn):
            identifier, request_id, permit = (values[key] for key in ("delivery", "request", "permit"))
            request = requests.get(request_id)
            if not request or not request["from"] <= turn < request["deadline"]:
                return "场次接收窗已关闭", False
            delivered = deliveries.get(identifier)
            if delivered and (delivered["request"], delivered["permit"]) != (request_id, permit):
                return "投递上下文不符", False
            if not files:
                return "没有作品文件", False
            if delivered is None:
                delivered = {"id": identifier, "request": request_id, "permit": permit, "members": {}}
                deliveries[identifier] = delivered
            for file in files:
                if file["name"] in delivered["members"]:
                    continue
                backlog = request["delay"].get(file["name"], 0)
                delivered["members"][file["name"]] = {
                    **file, "seat_at": turn + backlog, "state": "中转留存" if backlog else "席位已收"
                }
            return "已进入中转", request.get("lost_reply", False)

        def withdraw(self, values, turn):
            delivered = deliveries.get(values["delivery"])
            member = delivered["members"].get(values.get("member")) if delivered else None
            if not member:
                return "未查到成员", False
            action, state = values["action"], member["state"]
            if state == "中转留存" and member["seat_at"] <= turn:
                state = "席位已收"
            if action == "cancel" and state == "中转留存":
                member["state"] = "中转已撤"
                return "中转已撤", False
            if action == "purge" and state == "席位已收":
                member.update(state="待席位处理", purge_at=turn + 1)
                return "席位等待清除", False
            return "该状态不能办此手续", False

    return Room()
