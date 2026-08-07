define(['jquery'], function ($) {
  return function CustomWidget() {
    var DEFAULT_URL = 'https://144-31-108-55.sslip.io/salesbot/handler';

    this.callbacks = {
      init: function () { return true; },
      bind_actions: function () { return true; },
      render: function () { return true; },
      settings: function () { return true; },

      // Вызывается при сохранении шага виджета в конструкторе Salesbot.
      // Возвращаем один шаг с встроенным обработчиком widget_request: он POST-ит
      // на наш URL сообщение клиента ({{message_text}}) + id сделки, ждёт колбэк
      // на return_url (наш сервер сам отправит ответ ИИ обратно в бота).
      onSalesbotDesignerSave: function (handler_code, params) {
        var url = (params && params.url) ? params.url : DEFAULT_URL;
        var step = {
          question: [
            {
              handler: 'widget_request',
              params: {
                url: url,
                data: {
                  message: '{{message_text}}',
                  lead: '{{lead.id}}',
                  name: '{{contact.name}}'
                }
              }
            }
          ],
          require: []
        };
        return JSON.stringify([step]);
      },

      destroy: function () {}
    };

    return this;
  };
});
