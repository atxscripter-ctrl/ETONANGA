"""Playwright driver for mtacc.mobilelegends.com. Memory-safe."""
from __future__ import annotations
import asyncio
import hashlib
import json
import random
import time
from typing import Optional

from playwright.async_api import async_playwright, Browser, BrowserContext, Page

HOOK_JS = r"""
(function(){
  if(window.__acsHook)return; window.__acsHook=1;
  function isApi(u){u=String(u||'');return u.indexOf('accountmtapi')>=0;}
  function got(id,t,st,u,m){
    try{window.ACSBridge && window.ACSBridge.capture(JSON.stringify({
      id:id,url:u,method:m,status:st,len:(t||'').length,text:(t||'')
    }));}catch(e){}
  }
  var of=window.fetch;
  window.fetch=function(u,o){
    try{
      return of.apply(this,arguments).then(function(r){
        if(isApi(r.url||u)){
          var r2=(r.clone&&r.clone())||r;
          if(r2.text)r2.text().then(function(t){
            got(o&&o._id||'',t,r.status,r.url||u,'fetch');
          });
        }
        return r;
      });
    }catch(e){return of.apply(this,arguments);}
  };
  var OX=XMLHttpRequest.prototype,oo=OX.open,os=OX.send;
  OX.open=function(m,u){this._m=m;this._u=u;return oo.apply(this,arguments);};
  OX.send=function(b){
    try{
      var self=this;
      this.addEventListener('load',function(){
        if(isApi(self._u)){
          try{got('',self.responseText,self.status,self._u,self._m);}catch(e){}
        }
      },false);
    }catch(e){}
    return os.apply(this,arguments);
  };
})();
"""

LOGIN_URL = "https://mtacc.mobilelegends.com/"
API_BASE  = "https://accountmtapi.mobilelegends.com/"
UA = ("Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/155.0.0.0 Mobile Safari/537.36")


def _md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def make_sign(account: str, md5pwd: str) -> str:
    pairs = sorted([("account", account), ("country", ""), ("md5pwd", md5pwd)])
    joined = "&".join(f"{k}={v}" for k, v in pairs)
    return _md5(joined + "&op=new_login_pwd")


def _js_str(s: str) -> str:
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


class WebLogin:
    def __init__(self, accounts_per_recycle: int = 25):
        self._pw = None
        self._browser: Optional[Browser] = None
        self._ctx: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._captures: dict[str, dict] = {}
        self._accounts_since_recycle = 0
        self._accounts_per_recycle = accounts_per_recycle
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        self._pw = await async_playwright().start()
        await self._spawn_browser()
        self._started = True

    async def _spawn_browser(self) -> None:
        await self._kill_browser()
        self._browser = await self._pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--no-zygote",
                "--disable-accelerated-2d-canvas",
                "--disable-background-timer-throttling",
                "--disable-renderer-backgrounding",
                "--disable-backgrounding-occluded-windows",
                "--js-flags=--max-old-space-size=256",
            ],
        )
        self._ctx = await self._browser.new_context(
            user_agent=UA, locale="en-US",
            viewport={"width": 412, "height": 915},
            device_scale_factor=2.0, is_mobile=True, has_touch=True,
        )
        await self._ctx.add_init_script(HOOK_JS)
        await self._ctx.expose_binding("__acsCapture", self._on_capture)
        await self._ctx.add_init_script(
            "window.ACSBridge = {capture: function(s){ window.__acsCapture(s); }};"
        )
        self._page = await self._ctx.new_page()
        self._page.set_default_timeout(30000)
        self._page.set_default_navigation_timeout(30000)
        self._accounts_since_recycle = 0

    async def _kill_browser(self) -> None:
        for obj in (self._page, self._ctx, self._browser):
            if obj is not None:
                try:
                    await obj.close()
                except Exception:
                    pass
        self._page = None
        self._ctx = None
        self._browser = None

    async def _on_capture(self, source, payload: str) -> None:
        try:
            obj = json.loads(payload)
        except Exception:
            return
        rid = obj.get("id") or ""
        if "accountmtapi" not in (obj.get("url") or ""):
            return
        if rid:
            self._captures[rid] = obj
        self._captures[""] = obj
        if len(self._captures) > 50:
            keys = list(self._captures.keys())
            for k in keys[:-30]:
                self._captures.pop(k, None)

    async def _wait_cookie(self, timeout_s: float = 12.0) -> bool:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                ck = await self._ctx.cookies(LOGIN_URL)
                if len("; ".join(f"{c['name']}={c['value']}" for c in ck)) > 50:
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.25)
        return False

    async def _ensure_on_login(self) -> None:
        try:
            if self._page.url != LOGIN_URL:
                await self._page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
                await self._page.wait_for_timeout(2000)
                await self._wait_cookie()
        except Exception:
            # try one more time with a fresh page
            try:
                self._page = await self._ctx.new_page()
                await self._page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
                await self._page.wait_for_timeout(2000)
                await self._wait_cookie()
            except Exception:
                pass

    async def try_login(self, account: str, password: str) -> Optional[str]:
        # recycle browser every N accounts to avoid memory creep
        if self._accounts_since_recycle >= self._accounts_per_recycle:
            try:
                await self._spawn_browser()
            except Exception:
                pass
        self._accounts_since_recycle += 1

        try:
            await self._ensure_on_login()
        except Exception:
            return None

        md5pwd = _md5(password)
        sign = make_sign(account, md5pwd)
        payload = json.dumps({
            "op": "new_login_pwd",
            "sign": sign,
            "params": {"account": account, "md5pwd": md5pwd, "country": ""},
            "lang": "en",
        }, separators=(",", ":"), ensure_ascii=False)

        req_id = f"req-{int(time.time()*1000)}-{random.randint(0,9999)}"
        self._captures.pop(req_id, None)
        self._captures.pop("", None)

        url = API_BASE
        try:
            ck = await self._ctx.cookies(LOGIN_URL)
            jar = {c["name"]: c["value"] for c in ck}
            if "ssxmod_itna" in jar:
                url += ("?" if "?" not in url else "&") + "ssxmod_itna=" + jar["ssxmod_itna"]
        except Exception:
            pass

        js = (
            "(function(){try{"
            "var opts={method:'POST',credentials:'include',headers:{"
            "'Content-Type':'application/json',"
            "'Accept':'application/json, text/plain, */*',"
            "'Accept-Language':'en-GB,en-US;q=0.9,en;q=0.8',"
            "'Origin':'https://mtacc.mobilelegends.com',"
            "'Referer':'https://mtacc.mobilelegends.com/',"
            "'Sec-Fetch-Site':'same-site','Sec-Fetch-Mode':'cors','Sec-Fetch-Dest':'empty'"
            "},body:" + _js_str(payload) + ",_id:" + _js_str(req_id) + "};"
            f"fetch({_js_str(url)},opts).catch(function(e){{}});"
            "}catch(e){}})();"
        )
        try:
            await self._page.evaluate(js)
        except Exception:
            return None

        for _ in range(8):
            await asyncio.sleep(0.5)
            cap = self._captures.get(req_id) or self._captures.get("")
            if cap and cap.get("text"):
                return cap["text"]
        return None

    async def stop(self) -> None:
        await self._kill_browser()
        try:
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass
        self._pw = None
        self._started = False