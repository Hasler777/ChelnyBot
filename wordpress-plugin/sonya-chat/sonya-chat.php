<?php
/**
 * Plugin Name:       Соня — чат-консультант ЦветоМира
 * Plugin URI:        https://cvety-naberezhnye.ru
 * Description:        Плавающий чат-виджет «Соня»: AI-подбор букетов и передача заявок флористу в amoCRM. Тонкий загрузчик — сам виджет обслуживается сервером бота.
 * Version:           1.0.0
 * Author:            ЦветоМира
 * License:           GPL-2.0-or-later
 * Text Domain:       sonya-chat
 *
 * Как это работает: плагин лишь вставляет на страницы сайта один тег <script>,
 * который подгружает виджет с сервера бота (data-api). Вся логика чата, диалог с
 * Соней, подбор букетов и создание заявок в amoCRM — на стороне сервера. Значит,
 * обновления виджета не требуют переустановки плагина.
 */

if (!defined('ABSPATH')) {
    exit; // прямой вызов запрещён
}

define('SONYA_CHAT_OPT', 'sonya_chat_settings');

/** Значения по умолчанию. */
function sonya_chat_defaults() {
    return array(
        'enabled'   => '1',
        'api'       => 'https://144-31-108-55.sslip.io',
        'title'     => 'Соня · ЦветоМира',
        'subtitle'  => 'Онлайн-консультант по букетам',
        'color'     => '#d6336c',
        'position'  => 'right',
    );
}

function sonya_chat_get_settings() {
    $saved = get_option(SONYA_CHAT_OPT, array());
    if (!is_array($saved)) {
        $saved = array();
    }
    return array_merge(sonya_chat_defaults(), $saved);
}

/* ------------------------------------------------------------------ */
/*  Вставка виджета на сайт (футер каждой страницы фронтенда)          */
/* ------------------------------------------------------------------ */
function sonya_chat_render_footer() {
    if (is_admin()) {
        return;
    }
    $s = sonya_chat_get_settings();
    if (empty($s['enabled']) || $s['enabled'] !== '1') {
        return;
    }
    $api = rtrim(trim($s['api']), '/');
    if ($api === '') {
        return;
    }
    $src = esc_url($api . '/web/widget.js');
    printf(
        '<script src="%s" data-api="%s" data-title="%s" data-subtitle="%s" data-color="%s" data-position="%s"></script>' . "\n",
        $src,
        esc_attr($api),
        esc_attr($s['title']),
        esc_attr($s['subtitle']),
        esc_attr($s['color']),
        esc_attr($s['position'])
    );
}
add_action('wp_footer', 'sonya_chat_render_footer', 99);

/* ------------------------------------------------------------------ */
/*  Страница настроек в админке WordPress                              */
/* ------------------------------------------------------------------ */
function sonya_chat_admin_menu() {
    add_menu_page(
        'Соня — чат',
        'Соня',
        'manage_options',
        'sonya-chat',
        'sonya_chat_settings_page',
        'dashicons-format-chat',
        58
    );
}
add_action('admin_menu', 'sonya_chat_admin_menu');

function sonya_chat_register_settings() {
    register_setting('sonya_chat_group', SONYA_CHAT_OPT, 'sonya_chat_sanitize');
}
add_action('admin_init', 'sonya_chat_register_settings');

function sonya_chat_sanitize($input) {
    $d = sonya_chat_defaults();
    $out = array();
    $out['enabled']  = (isset($input['enabled']) && $input['enabled'] === '1') ? '1' : '0';
    $out['api']      = isset($input['api']) ? esc_url_raw(trim($input['api'])) : $d['api'];
    $out['title']    = isset($input['title']) ? sanitize_text_field($input['title']) : $d['title'];
    $out['subtitle'] = isset($input['subtitle']) ? sanitize_text_field($input['subtitle']) : $d['subtitle'];
    $color = isset($input['color']) ? sanitize_hex_color(trim($input['color'])) : '';
    $out['color']    = $color ? $color : $d['color'];
    $out['position'] = (isset($input['position']) && $input['position'] === 'left') ? 'left' : 'right';
    return $out;
}

function sonya_chat_settings_page() {
    if (!current_user_can('manage_options')) {
        return;
    }
    $s = sonya_chat_get_settings();
    ?>
    <div class="wrap">
        <h1>🌷 Соня — чат-консультант</h1>
        <p>Плавающий чат-виджет на сайте. Соня подбирает букеты и передаёт заявки флористу в amoCRM — так же, как Telegram-бот.</p>
        <form method="post" action="options.php">
            <?php settings_fields('sonya_chat_group'); ?>
            <table class="form-table" role="presentation">
                <tr>
                    <th scope="row">Виджет включён</th>
                    <td>
                        <label>
                            <input type="checkbox" name="<?php echo SONYA_CHAT_OPT; ?>[enabled]" value="1" <?php checked($s['enabled'], '1'); ?> />
                            Показывать чат на сайте
                        </label>
                    </td>
                </tr>
                <tr>
                    <th scope="row"><label for="snya_api">Адрес сервера бота</label></th>
                    <td>
                        <input type="url" id="snya_api" class="regular-text" name="<?php echo SONYA_CHAT_OPT; ?>[api]" value="<?php echo esc_attr($s['api']); ?>" />
                        <p class="description">Публичный HTTPS-адрес сервера Сони (там же работают /web/* эндпоинты). Менять только если сервер переехал.</p>
                    </td>
                </tr>
                <tr>
                    <th scope="row"><label for="snya_title">Заголовок</label></th>
                    <td><input type="text" id="snya_title" class="regular-text" name="<?php echo SONYA_CHAT_OPT; ?>[title]" value="<?php echo esc_attr($s['title']); ?>" /></td>
                </tr>
                <tr>
                    <th scope="row"><label for="snya_sub">Подзаголовок</label></th>
                    <td><input type="text" id="snya_sub" class="regular-text" name="<?php echo SONYA_CHAT_OPT; ?>[subtitle]" value="<?php echo esc_attr($s['subtitle']); ?>" /></td>
                </tr>
                <tr>
                    <th scope="row"><label for="snya_color">Цвет</label></th>
                    <td>
                        <input type="text" id="snya_color" name="<?php echo SONYA_CHAT_OPT; ?>[color]" value="<?php echo esc_attr($s['color']); ?>" placeholder="#d6336c" />
                        <input type="color" value="<?php echo esc_attr($s['color']); ?>" oninput="document.getElementById('snya_color').value=this.value" />
                        <p class="description">Основной цвет пузыря и кнопки отправки.</p>
                    </td>
                </tr>
                <tr>
                    <th scope="row">Позиция</th>
                    <td>
                        <label><input type="radio" name="<?php echo SONYA_CHAT_OPT; ?>[position]" value="right" <?php checked($s['position'], 'right'); ?> /> Справа снизу</label>
                        &nbsp;&nbsp;
                        <label><input type="radio" name="<?php echo SONYA_CHAT_OPT; ?>[position]" value="left" <?php checked($s['position'], 'left'); ?> /> Слева снизу</label>
                    </td>
                </tr>
            </table>
            <?php submit_button('Сохранить'); ?>
        </form>
        <hr>
        <p><strong>Проверить:</strong> откройте главную страницу сайта — чат-пузырь появится в выбранном углу.
        Демо-страница виджета: <code><?php echo esc_html(rtrim($s['api'], '/')); ?>/web/demo</code></p>
    </div>
    <?php
}

/* Ссылка «Настройки» на странице списка плагинов. */
function sonya_chat_action_links($links) {
    $url = admin_url('admin.php?page=sonya-chat');
    array_unshift($links, '<a href="' . esc_url($url) . '">Настройки</a>');
    return $links;
}
add_filter('plugin_action_links_' . plugin_basename(__FILE__), 'sonya_chat_action_links');
