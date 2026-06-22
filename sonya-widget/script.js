console.log('SONYA: script.js загружен');

define([], function () {
  console.log('SONYA: define() выполнен');

  var BASE = 'https://144-31-108-55.sslip.io';
  var TOKEN = 'd87aa98041793ac32b71b97087cd06fa';

  function leadId() {
    try {
      if (window.APP && APP.data && APP.data.current_card && APP.data.current_card.id) {
        return APP.data.current_card.id;
      }
    } catch (e) {}
    var m = location.pathname.match(/\/detail\/(\d+)/);
    return m ? m[1] : null;
  }

  function panelUrl(id) {
    return BASE + '/widget/panel?lead_id=' + id + '&token=' + encodeURIComponent(TOKEN);
  }

  function showButton() {
    try {
      var id = leadId();
      console.log('SONYA: showButton, leadId =', id);
      var btn = document.getElementById('sonya-chat-btn');
      if (!id) { if (btn) btn.remove(); return; }
      if (btn) { btn.href = panelUrl(id); return; }
      var a = document.createElement('a');
      a.id = 'sonya-chat-btn';
      a.href = panelUrl(id);
      a.target = '_blank';
      a.textContent = '💬 Чат с клиентом';
      a.style.cssText =
        'position:fixed;right:20px;bottom:20px;z-index:99999;background:#2f6feb;' +
        'color:#fff;padding:12px 18px;border-radius:24px;font:600 14px sans-serif;' +
        'text-decoration:none;box-shadow:0 3px 10px rgba(0,0,0,.35);';
      document.body.appendChild(a);
      console.log('SONYA: кнопка добавлена');
    } catch (e) { console.log('SONYA: ошибка showButton', e); }
  }

  var CustomWidget = function () {
    var self = this;
    this.callbacks = {
      init: function () { console.log('SONYA: init'); return true; },
      render: function () {
        console.log('SONYA: render, area =', (self.system && self.system().area));
        setTimeout(showButton, 500);
        if (!window.__sonyaTimer) {
          window.__sonyaTimer = setInterval(showButton, 2000);
        }
        return true;
      },
      bind_actions: function () { console.log('SONYA: bind_actions'); return true; },
      settings: function () { return true; },
      onSave: function () { return true; },
      destroy: function () {
        var b = document.getElementById('sonya-chat-btn');
        if (b) b.remove();
      }
    };
  };

  return CustomWidget;
});
