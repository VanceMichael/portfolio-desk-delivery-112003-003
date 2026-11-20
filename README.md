# 双选会作品领取门户

本地演练资料使用虚构的实物、机构和记录编号。经营单据存放于 `data/`，包括 `dispatch.json`、`mailroom.json`、`pages.json`。来源原件在提供时由资料端送出。

## 资料约定

只有匿名用途可见匿名作品，实名是单独原件。作品编号跨席位共享，领取号组合场次、席位、用途与修订。旧版下载页能通过旧地址继续打开，页面凭据用于区分。收条区分收到和已收回，发送记录仅表示办会端交出了资料。

用浏览器访问本地端口。请求、台账和原件均为合成资料。重启服务后恢复到演练开始时的页面和登记状态。

## 运行资料端

浏览器检查使用 Playwright 1.56.0 附带的 Chromium。已有安装可直接运行；新环境先执行 `python3 -m pip install -r requirements.txt` 和 `python3 -m playwright install --with-deps chromium`。

```sh
python3 portfolio_portal.py --port 8040
```

以下命令核对本地资料服务和交互。

```sh
python3 -m unittest discover -s tests -v
```
