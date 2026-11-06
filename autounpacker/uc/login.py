# -*- coding: utf-8 -*-
"""UC 登录流程的浏览器内脚本（纯字符串构造，无 Qt 依赖）。

设计原则：
- **只做确定性动作**：填手机号、点「获取验证码」、填验证码、点「登录」、点「全部下载」。
- **滑块一律留给用户手动拖**（阿里 NC 带轨迹风控，程序硬拖既不可靠也容易被判风险）。
- 每个动作都返回 JSON：`{"ok": bool, "msg": str, ...}`，失败不抛异常，
  这样 Python 侧可以降级为「请你在窗口里手动操作」。

DOM 选择器以用户提供的真实页面 DOM 为准，并带多重回退：
- 登录面板：`.login-view` / `.login-box`
- 手机号输入：scope 内 `input[type=tel]`（排除 `#sms_code`）/ placeholder 含「手机号」
- 获取验证码：`#send_sms_code` / 文案「获取短信验证码」「获取验证码」
- 验证码输入：`#sms_code` / placeholder 含「验证码」
- 登录按钮：`#submit_btn` / `input[value=登录]` / 文案「登录」
- 全部下载：`.ant-btn.task-footer-down` / 文案「全部下载」
"""
import json

_PRELUDE = r"""
  function q(s){try{return document.querySelector(s)}catch(e){return null}}
  function qa(s){try{return Array.prototype.slice.call(document.querySelectorAll(s))}catch(e){return []}}
  function norm(e){return ((e&&(e.innerText||e.textContent))||'').replace(/\s+/g,'')}
  function hasText(e,w){return norm(e).indexOf(w)>=0}
  function clickIt(e){ if(!e) return false;
    try{ e.click(); return true }catch(x){
      try{ e.dispatchEvent(new MouseEvent('click',{bubbles:true})); return true }catch(y){ return false } } }
  function firstByText(sel, words){
    var els=qa(sel);
    for(var i=0;i<els.length;i++){ for(var j=0;j<words.length;j++){ if(hasText(els[i],words[j])) return els[i] } }
    return null }
  /* 取「文本恰好等于 word 且可见」的最内层元素。
     真机坑：firstByText 用 indexOf 会先命中整个页面容器（div.index-bg 的
     innerText 也含「登录UC网盘」），点了等于没点；必须找最内层那一个。 */
  function exact(sel, word){
    var els=qa(sel), best=null;
    for(var i=0;i<els.length;i++){
      var e=els[i];
      if(norm(e)!==word) continue;
      try{ if(!(e.getClientRects().length)) continue }catch(x){ continue }
      var inner=false, kids=e.children||[];
      for(var k=0;k<kids.length;k++){ if(norm(kids[k])===word){ inner=true; break } }
      if(!inner) return e;
      if(!best) best=e;
    }
    return best }
  function frameOf(){ var fs=qa('iframe');
    for(var i=0;i<fs.length;i++){ var s=fs[i].src||''; if(s.indexOf('api.open.uc.cn')>=0) return fs[i] }
    return null }
  function setVal(e,v){ if(!e) return false;
    try{ var d=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value');
         d.set.call(e,v);
         e.dispatchEvent(new Event('input',{bubbles:true}));
         e.dispatchEvent(new Event('change',{bubbles:true}));
         return true }catch(x){ try{ e.value=v; return true }catch(y){ return false } } }
  function loginScope(){ return q('.login-view') || q('.login-box') || q('.phone-login-box') || document }
  function phoneInput(){ var s=loginScope();
    var els=qa.call(null,'input').filter(function(i){ return s===document || (s.contains && s.contains(i)) });
    for(var i=0;i<els.length;i++){ var p=(els[i].placeholder||''); if(p.indexOf('手机号')>=0) return els[i] }
    for(var i=0;i<els.length;i++){ var e=els[i]; if(e.type==='tel' && e.id!=='sms_code') return e }
    return null }
  function codeInput(){ var c=q('#sms_code'); if(c) return c; var s=loginScope();
    var els=qa.call(null,'input').filter(function(i){ return s===document || (s.contains && s.contains(i)) });
    for(var i=0;i<els.length;i++){ if((els[i].placeholder||'').indexOf('验证码')>=0) return els[i] }
    return null }
"""


def _wrap(body):
    return ("(function(){" + _PRELUDE +
            "try{" + body + "}catch(e){return JSON.stringify({ok:false,msg:'JS异常: '+e})}})()")


# UC 账号中心（CAS）登录页本体。
#
# 真机事实：分享页点登录后弹出的面板里，表单在**跨域 iframe**
# （api.open.uc.cn/cas/custom/login）内，主文档 runJavaScript 拿不到；QWebEngine
# 也没法在指定子框架里执行脚本。而同一条 URL 单独打开时，`#login_name` /
# `#sms_code` / `#send_sms_code` / `#submit_btn` 全部在主文档、可直接驱动
# （已用真实 Edge 验证）。所以登录助手改成：**先把整页导航到这个 URL**，
# 登录完再回到分享页——cookie 落在 .uc.cn 域，分享页随之变成已登录态。
LOGIN_URL = ("https://api.open.uc.cn/cas/custom/login"
             "?custom_login_type=mobile&client_id=519&display=pc")


def is_login_url(url):
    """当前地址是不是 UC 账号中心登录页。"""
    return "api.open.uc.cn/cas/" in str(url or "")


def js_state():
    """探测当前页面状态（只读，不点任何东西）。"""
    return _wrap(r"""
      var dl = q('.ant-btn.task-footer-down') || firstByText('button,a', ['全部下载']);
      var s = loginScope();
      var box = q('.login-iframe-view');
      var phoneTab = firstByText('.login-item,div,span,button', ['手机登录']);
      var smsBtn = q('#send_sms_code') ||
                   firstByText('a,button,span', ['获取短信验证码','获取验证码']);
      var submit = q('#submit_btn') || firstByText('button,input[type=button],input[type=submit]', ['登录']);
      var navLogin = exact('div,span,button,a', '登录UC网盘') ||
                     exact('div,span,button,a', '登录/注册') ||
                     exact('div,span,button,a', '立即登录');
      return JSON.stringify({
        ok: true,
        url: location.href,
        title: document.title,
        login_panel: (s !== document) || !!(box && box.getClientRects().length),
        iframe: !!frameOf(),
        nav_login: !!navLogin,
        download_btn: !!dl,
        phone_tab: !!phoneTab,
        phone_input: !!phoneInput(),
        sms_btn: !!smsBtn,
        code_input: !!codeInput(),
        submit_btn: !!submit
      });
    """)


def js_open_login():
    """叫出登录面板。

    真机 DOM：右上角是 `div.nav-item.login-btn`，文本「登录UC网盘」。必须用
    exact（最内层）点击——父容器 innerText 也含这串字，点到容器等于没点。
    找不到入口时兜底点「全部下载」：未登录状态下它一定会拉起登录抽屉。
    """
    return _wrap(r"""
      var el = exact('div,span,button,a', '登录UC网盘')
            || exact('div,span,button,a', '登录/注册')
            || exact('div,span,button,a', '立即登录')
            || exact('div,span,button,a', '登录');
      if(el){ clickIt(el); return JSON.stringify({ok:true,msg:'已点击登录入口'}) }
      var dl = q('.ant-btn.task-footer-down');
      if(dl){ clickIt(dl); return JSON.stringify({ok:true,msg:'无登录入口，已点「全部下载」拉起登录'}) }
      return JSON.stringify({ok:false,msg:'页面上没找到登录入口'});
    """)


def js_pick_phone_tab():
    """切到「手机登录」标签（标签在**主文档**里，真正的表单在跨域 iframe 内）。"""
    return _wrap(r"""
      var el = exact('.login-item', '手机登录')
            || exact('div,span,li', '手机登录');
      if(!el) return JSON.stringify({ok:false,msg:'没找到「手机登录」标签（可能面板未展开）'});
      clickIt(el);
      return JSON.stringify({ok:true,msg:'已切到手机登录'});
    """)


def js_fill_phone(phone):
    """把手机号写进输入框（触发 input/change，让前端框架感知）。"""
    lit = json.dumps(str(phone or ""), ensure_ascii=False)
    return _wrap("""
      var el = phoneInput();
      if(!el) return JSON.stringify({ok:false,msg:'没找到手机号输入框'});
      if(!setVal(el, %s)) return JSON.stringify({ok:false,msg:'写入手机号失败'});
      el.focus();
      return JSON.stringify({ok:true,msg:'已填入手机号'});
    """ % lit)


def js_send_sms():
    """点「获取短信验证码」。

    真机坑：按钮点完后会变成「获取短信验证码 (59s)」倒计时。倒计时期间再点
    不会发新码，但**每请求一次都会让上一条码失效** —— 用户拿到第一条码去提交，
    却因为中途又请求过而判「无效或过期」。所以倒计时中直接拒绝，不点。
    """
    return _wrap(r"""
      var el = q('#send_sms_code') || q('.smsCode-btn') ||
               firstByText('a,button,span', ['获取短信验证码','获取验证码']);
      if(!el) return JSON.stringify({ok:false,msg:'没找到「获取验证码」按钮'});
      var txt = norm(el);
      var m = txt.match(/(\d+)\s*[sS]/);
      if (m) return JSON.stringify({ok:false,
        msg:'验证码已发送，请等倒计时结束（约 ' + m[1] + ' 秒）再重发；期间重发会让上一条失效'});
      clickIt(el);
      return JSON.stringify({ok:true,msg:'已点击获取验证码'});
    """)


def js_fill_code(code):
    """写入短信验证码。"""
    lit = json.dumps(str(code or ""), ensure_ascii=False)
    return _wrap("""
      var el = codeInput();
      if(!el) return JSON.stringify({ok:false,msg:'没找到验证码输入框'});
      if(!setVal(el, %s)) return JSON.stringify({ok:false,msg:'写入验证码失败'});
      el.focus();
      return JSON.stringify({ok:true,msg:'已填入验证码'});
    """ % lit)


def js_ensure_agreement():
    """勾上「我已阅读并同意…」复选框（进登录页就勾，别等提交时才补）。

    真机 DOM：`#agreement`（16x16，可见），外层可点区域 `.div_agreement`。
    不勾时点「登录/注册」只会弹红字「勾选同意注册协议，以完成注册」。
    """
    return _wrap(r"""
      var cb = q('#agreement') || q('input[type=checkbox][name*=agree]');
      if(!cb) return JSON.stringify({ok:true, checked:false, before:false,
                                    msg:'页面上没有同意协议勾选框'});
      var before = false;
      try { before = !!cb.checked } catch(e) { before = false }
      if (!before) {
        clickIt(cb);
        try { if (!cb.checked) { clickIt(q('.div_agreement')); } } catch(e) {}
      }
      var now = false;
      try { now = !!cb.checked } catch(e) { now = false }
      return JSON.stringify({ok:true, checked:now, before:before,
        msg: now ? '已勾选同意协议' : '勾选协议失败，请手动勾选'});
    """)


def js_submit():
    """勾选「我已阅读并同意…」后点登录。

    真机坑（登录/注册页）：不勾协议直接点登录会弹红字「勾选同意注册协议，以完成
    注册」，登录根本不发出。真实 DOM 里复选框是 `#agreement`（点击即可勾上），
    外面还有 `.div_agreement` 可点区域做兜底。
    """
    return _wrap(r"""
      var cb = q('#agreement');
      var checked = false;
      try { checked = !!(cb && cb.checked) } catch(e) { checked = false }
      if (cb && !checked) {
        clickIt(cb);
        try {
          if (!cb.checked) { clickIt(q('.div_agreement')); }
        } catch(e) {}
      }
      var now = false;
      try { now = !!(cb && cb.checked) } catch(e) { now = false }
      var el = q('#submit_btn') ||
               firstByText('input[type=button],input[type=submit]', ['登录/注册','登录']) ||
               firstByText('button', ['登录/注册','登录']);
      if(!el) return JSON.stringify({ok:false,msg:'没找到登录按钮'});
      clickIt(el);
      return JSON.stringify({ok:true,
        msg: cb ? (now ? '已勾选协议并点击登录' : '已点击登录（协议未勾上，请手动勾选）')
                : '已点击登录'});
    """)


def js_login_error():
    """读登录页错误提示 + 验证码框当前值（判断「点登录后到底发生了什么」）。

    真机：提交后不论成败页面都会清空验证码框；失败时正文出现
    「短信验证码无效或过期」/「请输入短信验证码」等。不读这个的话，用户只能看到
    「已提交登录」然后干等，完全不知道服务端拒绝的原因。
    """
    return _wrap(r"""
      var body = ((document.body && document.body.innerText) || '').replace(/\s+/g,'');
      var m = body.match(/(请先输入短信验证码|请输入短信验证码|短信验证码无效或过期|验证码错误|验证码已失效|操作过于频繁)/);
      var el = q('#sms_code');
      return JSON.stringify({ok:true, error: m ? m[1] : '',
                             code_value: el ? (el.value || '') : ''});
    """)


def js_download_all():
    """点「全部下载」——真正的下载请求会走浏览器，由 Qt 侧拦截下载 URL。"""
    return _wrap(r"""
      var el = q('.ant-btn.task-footer-down') || firstByText('button,a', ['全部下载']);
      if(!el) return JSON.stringify({ok:false,msg:'没找到「全部下载」按钮（可能未登录或页面未加载完）'});
      clickIt(el);
      return JSON.stringify({ok:true,msg:'已点击「全部下载」'});
    """)


def js_page_text():
    """取页面可见文本（截断），用于日志与「是否需要登录」判断。"""
    return _wrap(r"""
      var t = ((document.body && document.body.innerText) || '').replace(/\s+/g,' ').trim();
      return JSON.stringify({ok:true, text: t.slice(0, 400)});
    """)


# ---------------------------------------------------------------------------
# 提取码（分享密码）页
#
# 真机 DOM（fast.uc.cn/s/<id> 有码分享）：
#   输入框  input.text-input[placeholder="请输入密码"]
#   按钮    div.view-btn（文本「查看文件」）
#   填满正确位数后站点**自动校验**（填错误码 2 秒内出现「文件密码有误，请重新输入」），
#   所以正常路径不需要点「查看文件」；点击只作兜底。
# ---------------------------------------------------------------------------
def js_share_code_state():
    """读提取码页状态：是否需要码、是否有错、是否已进文件页。"""
    return _wrap(r"""
      var inp = q('input[placeholder*="密码"]') || q('input.text-input');
      var body = ((document.body && document.body.innerText) || '').replace(/\s+/g,'');
      return JSON.stringify({
        ok: true,
        need_code: !!inp,
        error: body.indexOf('文件密码有误') >= 0 ? '文件密码有误' : '',
        has_files: !!q('.ant-btn.task-footer-down'),
        url: location.href
      });
    """)


def js_fill_share_code(code):
    """把提取码写进分享页输入框（触发 input/change，站点会自动校验）。"""
    lit = json.dumps(str(code or ""), ensure_ascii=False)
    return _wrap("""
      var el = q('input[placeholder*="密码"]') || q('input.text-input');
      if(!el) return JSON.stringify({ok:false,msg:'当前页面没有提取码输入框'});
      if(!setVal(el, %s)) return JSON.stringify({ok:false,msg:'写入提取码失败'});
      try{ el.focus() }catch(e){}
      return JSON.stringify({ok:true,msg:'已填入提取码'});
    """ % lit)


def js_click_view():
    """兜底：点「查看文件」（正常情况下填满后站点会自动提交，不需要点）。"""
    return _wrap(r"""
      var el = q('.view-btn');
      if(!el){ el = exact('div,span,button', '查看文件') }
      if(!el) return JSON.stringify({ok:false,msg:'没找到「查看文件」按钮'});
      clickIt(el);
      return JSON.stringify({ok:true,msg:'已点击查看文件'});
    """)
