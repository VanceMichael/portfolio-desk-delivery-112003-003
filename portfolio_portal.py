"""双选会授权与领取页面。"""
from argparse import ArgumentParser
from html import escape
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


ROOT = Path(__file__).resolve().parent
LABELS = {"anonymous": "匿名初筛", "named": "实名面谈"}


def choice(name, label, values):
    options = "".join(f'<option value="{escape(v)}">{escape(LABELS.get(v, v))}</option>' for v in values)
    return f'<label>{label}<select name="{name}">{options}</select></label>'


def create_source(port=0, root=ROOT):
    entries = json.loads((root / "data/pages.json").read_text(encoding="utf-8"))
    permits = {entry["id"]: entry for entry in entries}

    class Portal(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def finish_response(self, body, kind, status=200, filename=""):
            self.send_response(status)
            self.send_header("Content-Type", kind)
            if filename:
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def page(self, title, content, status=200):
            document = f"""<!doctype html><html lang="zh"><meta charset="utf-8"><title>{title}</title>
            <style>body{{font:16px system-ui;margin:2rem;max-width:60rem}}label{{display:block;margin:.75rem 0}}
            dt{{font-weight:600}}dd{{margin:0 0 .75rem}}select,button{{margin-left:1rem;padding:.3rem}}</style>
            <main><h1>{title}</h1><nav><a href="/">席位选择</a></nav>{content}</main></html>"""
            self.finish_response(document.encode(), "text/html; charset=utf-8", status)

        def context(self, permit):
            values = [("授权编号", permit["id"]), ("席位", permit["desk"]), ("场次", permit["slot"]),
                      ("用途", LABELS[permit["use"]]), ("修订", str(permit["rev"]))]
            return "<dl>" + "".join(f"<dt>{key}</dt><dd>{escape(value)}</dd>" for key, value in values) + "</dl>"

        def do_GET(self):
            url = urlsplit(self.path)
            args = {key: value[0] for key, value in parse_qs(url.query).items()}
            if url.path == "/":
                fields = choice("desk", "企业席位", ["E1", "E2", "F4"])
                fields += choice("slot", "面谈场次", ["PM1", "PM2"])
                fields += choice("use", "作品用途", list(LABELS))
                fields += choice("release", "授权公告轮次", ["1", "2"])
                self.page("作品领取", f'<form action="/permission">{fields}<button>查看授权</button></form>')
                return
            if url.path == "/permission":
                matches = [p for p in entries if all(p[k] == args.get(k) for k in ("desk", "slot", "use"))
                           and str(p["rev"]) <= args.get("release", "1")]
                if matches:
                    permit = max(matches, key=lambda p: p["rev"])
                    items = "".join(f"<li>{escape(item)}</li>" for item in permit["allowed"])
                    pickup = permit["previous"] if permit["id"] == "P15" and not args.get("refresh") else permit["id"]
                    refresh = "&amp;".join(f"{key}={escape(args[key])}" for key in ("desk", "slot", "use", "release"))
                    self.page("席位授权", self.context(permit) + f'<ul aria-label="已授权作品">{items}</ul>'
                              f'<a href="/pickup/{pickup}" target="_blank">打开领取页</a> '
                              f'<a href="/permission?{refresh}&amp;refresh=1">刷新授权与领取入口</a>')
                    return
            _, _, path_id = url.path.partition("/pickup/")
            if url.path.startswith("/pickup/") and path_id in permits:
                permit = permits[path_id]
                links = "".join(f'<li><a href="/file/{path_id}/{item}">{item}</a></li>' for item in permit["served"])
                self.page("领取清单", self.context(permit) + f'<ul aria-label="可下载作品">{links}</ul>')
                return
            parts = url.path.split("/")
            if len(parts) == 4 and parts[1] == "file" and parts[2] in permits:
                item = parts[3]
                if item in permits[parts[2]]["served"]:
                    path = root / "originals/works" / (item + ".txt")
                    self.finish_response(path.read_bytes(), "text/plain; charset=utf-8", filename=path.name)
                    return
            self.page("当前无资料", "", 404)

    return HTTPServer(("127.0.0.1", port), Portal)


if __name__ == "__main__":
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8040)
    with create_source(parser.parse_args().port) as server:
        print(f"作品门户在 http://127.0.0.1:{server.server_port}", flush=True)
        server.serve_forever()
