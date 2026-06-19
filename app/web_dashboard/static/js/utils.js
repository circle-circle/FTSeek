/**
 * 共享工具函数 — 所有页面通用
 * 在 base.html 或各页面 <head> 中加载:
 *   <script src="/static/js/utils.js"></script>
 */

/**
 * HTML 转义 — 防止 XSS
 * 转义: & < > " ' / `
 * 适用于 innerHTML 和 onclick 属性上下文
 */
function escHtml(s) {
    return String(s)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;')
        .replace(/\//g, '&#x2F;')
        .replace(/`/g, '&#96;');
}
