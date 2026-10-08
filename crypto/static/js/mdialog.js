/* ================================================================
   现代化弹窗组件 (MDialog)
   替代原生 alert/confirm/prompt
   独立模块，可在任意页面引入
   ================================================================ */
var MDialog = (function() {
    var activeDialog = null; // 当前活动的弹窗

    // 图标映射
    var ICON_MAP = {
        info:    { emoji: 'ℹ️', cls: 'm-icon-info' },
        success: { emoji: '✅', cls: 'm-icon-success' },
        warning: { emoji: '⚠️', cls: 'm-icon-warning' },
        danger:  { emoji: '🗑️', cls: 'm-icon-danger' },
        primary: { emoji: '💡', cls: 'm-icon-primary' },
        confirm: { emoji: '❓', cls: 'm-icon-warning' },
        alert:   { emoji: 'ℹ️', cls: 'm-icon-info' }
    };

    /**
     * 显示弹窗
     * @param {Object} opts - 配置项
     * @param {string} opts.title - 标题
     * @param {string} opts.message - 内容（支持 HTML）
     * @param {string} opts.type - 类型: info|success|warning|danger|primary|confirm|alert
     * @param {boolean} opts.modal - 是否模态（默认 true，false 为非模态）
     * @param {boolean} opts.draggable - 是否可拖拽（默认 true）
     * @param {boolean} opts.showClose - 是否显示关闭按钮（默认 true）
     * @param {boolean} opts.showCancel - 是否显示取消按钮（默认 false）
     * @param {string} opts.okText - 确定按钮文本（默认 '确定'）
     * @param {string} opts.cancelText - 取消按钮文本（默认 '取消'）
     * @param {string} opts.okType - 确定按钮类型（默认 'primary'）
     * @param {string} opts.cancelType - 取消按钮类型（默认 'secondary'）
     * @param {Function} opts.onOk - 点击确定回调
     * @param {Function} opts.onCancel - 点击取消回调
     * @param {Function} opts.onClose - 弹窗关闭后回调
     * @param {boolean} opts.prompt - 是否显示输入框（prompt 模式）
     * @param {string} opts.defaultValue - prompt 模式默认值
     * @param {string} opts.placeholder - prompt 模式占位符
     */
    function show(opts) {
        opts = opts || {};
        // 关闭已有的弹窗
        if (activeDialog) close(activeDialog);

        var type = opts.type || 'info';
        var isModal = opts.modal !== false;
        var draggable = opts.draggable !== false;
        var showClose = opts.showClose !== false;
        var showCancel = opts.showCancel || false;
        var okText = opts.okText || '确定';
        var cancelText = opts.cancelText || '取消';
        var okType = opts.okType || 'primary';
        var cancelType = opts.cancelType || 'secondary';
        var isPrompt = opts.prompt || false;

        // 图标
        var iconData = ICON_MAP[type] || ICON_MAP.info;
        var iconHtml = '<span class="m-dialog-icon ' + iconData.cls + '">' + iconData.emoji + '</span>';

        // 标题
        var title = opts.title || '';
        var titleHtml = title ? '<span class="m-dialog-title">' + title + '</span>' : '';
        var closeBtnHtml = showClose ? '<button class="m-dialog-close" aria-label="关闭">✕</button>' : '';

        // 内容（兼容 content 参数名，部分页面使用 content 而非 message）
        var message = opts.message || opts.content || '';
        var bodyHtml = '<div class="m-dialog-body">' + message;
        if (isPrompt) {
            bodyHtml += '<input type="text" class="m-dialog-input" placeholder="' + (opts.placeholder || '') + '" value="' + (opts.defaultValue || '') + '">';
        }
        bodyHtml += '</div>';

        // 按钮（兼容 buttons 数组：[{text, type, onClick}]，优先于默认按钮）
        var customButtons = (opts.buttons && opts.buttons.length) ? opts.buttons : null;
        var footerHtml = '<div class="m-dialog-footer">';
        if (customButtons) {
            customButtons.forEach(function(b, i) {
                var bType = (b.type === 'cancel') ? cancelType : (b.type || okType);
                footerHtml += '<button class="m-btn m-btn-' + bType + ' m-btn-sm m-dialog-custom-btn" data-btn-idx="' + i + '">' + (b.text || '确定') + '</button>';
            });
        } else {
            if (showCancel) {
                footerHtml += '<button class="m-btn m-btn-' + cancelType + ' m-btn-sm m-dialog-cancel-btn">' + cancelText + '</button>';
            }
            footerHtml += '<button class="m-btn m-btn-' + okType + ' m-btn-sm m-dialog-ok-btn">' + okText + '</button>';
        }
        footerHtml += '</div>';

        // 构建完整 HTML
        var dialogHtml = '<div class="m-dialog m-dialog-' + type + '"' + (draggable ? ' data-draggable="true"' : '') + '>' +
            '<div class="m-dialog-header">' + iconHtml + titleHtml + closeBtnHtml + '</div>' +
            bodyHtml + footerHtml + '</div>';

        // 创建遮罩层
        var overlay = document.createElement('div');
        overlay.className = 'm-dialog-overlay' + (isModal ? '' : ' m-dialog-nonmodal');
        overlay.innerHTML = dialogHtml;
        document.body.appendChild(overlay);

        var dialog = overlay.querySelector('.m-dialog');
        activeDialog = overlay;

        // 自定义宽度（如食物库管理等宽内容弹窗），同时保证不超出视口
        if (opts.width) {
            dialog.style.width = opts.width;
            dialog.style.maxWidth = 'calc(100vw - 32px)';
        }

        // 动画显示
        requestAnimationFrame(function() {
            requestAnimationFrame(function() {
                overlay.classList.add('m-dialog-visible');
            });
        });

        // 绑定事件
        var okBtn = overlay.querySelector('.m-dialog-ok-btn');
        var cancelBtn = overlay.querySelector('.m-dialog-cancel-btn');
        var closeBtn = overlay.querySelector('.m-dialog-close');
        var inputEl = overlay.querySelector('.m-dialog-input');

        function doClose() { close(overlay); }

        // buttons 数组模式：逐个绑定回调，onClick 返回 false 可阻止关闭
        if (customButtons) {
            var customBtns = overlay.querySelectorAll('.m-dialog-custom-btn');
            Array.prototype.forEach.call(customBtns, function(btn) {
                var idx = parseInt(btn.getAttribute('data-btn-idx'), 10);
                btn.addEventListener('click', function() {
                    var b = customButtons[idx];
                    var ret = (b && b.onClick) ? b.onClick() : undefined;
                    if (ret !== false) doClose();
                });
            });
        }

        // 确定
        if (okBtn) {
            okBtn.addEventListener('click', function() {
                if (opts.onOk) {
                    var result = isPrompt ? (inputEl ? inputEl.value : '') : true;
                    var ret = opts.onOk(result);
                    // 如果回调返回 false，则不关闭
                    if (ret === false) return;
                }
                doClose();
            });
        }

        // 取消
        if (cancelBtn) {
            cancelBtn.addEventListener('click', function() {
                if (opts.onCancel) opts.onCancel();
                doClose();
            });
        }

        // 关闭按钮
        if (closeBtn) {
            closeBtn.addEventListener('click', function() {
                if (opts.onCancel) opts.onCancel();
                doClose();
            });
        }

        // 点击遮罩关闭（仅模态）
        if (isModal) {
            overlay.addEventListener('click', function(e) {
                if (e.target === overlay) {
                    if (opts.onCancel) opts.onCancel();
                    doClose();
                }
            });
        }

        // ESC 关闭
        function escHandler(e) {
            if (e.key === 'Escape') {
                if (opts.onCancel) opts.onCancel();
                doClose();
                document.removeEventListener('keydown', escHandler);
            }
        }
        document.addEventListener('keydown', escHandler);
        overlay._escHandler = escHandler;

        // 拖拽功能
        if (draggable) {
            initDrag(dialog, overlay.querySelector('.m-dialog-header'));
        }

        // 自动聚焦输入框
        if (isPrompt && inputEl) {
            setTimeout(function() { inputEl.focus(); }, 300);
            // 回车确认
            inputEl.addEventListener('keydown', function(e) {
                if (e.key === 'Enter') {
                    okBtn.click();
                }
            });
        }

        return overlay;
    }

    /**
     * 关闭弹窗
     */
    function close(overlay) {
        if (!overlay || !overlay.parentNode) return;
        overlay.classList.remove('m-dialog-visible');
        if (overlay._escHandler) {
            document.removeEventListener('keydown', overlay._escHandler);
        }
        setTimeout(function() {
            if (overlay.parentNode) {
                overlay.parentNode.removeChild(overlay);
            }
            if (activeDialog === overlay) activeDialog = null;
        }, 280);
    }

    /**
     * 拖拽功能
     */
    function initDrag(dialog, handle) {
        if (!handle) return;
        var startX, startY, offsetX = 0, offsetY = 0;
        var isDragging = false;

        function onPointerDown(e) {
            if (e.target.closest('.m-dialog-close')) return;
            isDragging = true;
            dialog.classList.add('m-dialog-dragging');
            startX = e.clientX - offsetX;
            startY = e.clientY - offsetY;
            e.preventDefault();
        }

        function onPointerMove(e) {
            if (!isDragging) return;
            offsetX = e.clientX - startX;
            offsetY = e.clientY - startY;
            dialog.style.transform = 'translate(' + offsetX + 'px, ' + offsetY + 'px) scale(1)';
        }

        function onPointerUp() {
            isDragging = false;
            dialog.classList.remove('m-dialog-dragging');
        }

        handle.addEventListener('mousedown', onPointerDown);
        document.addEventListener('mousemove', onPointerMove);
        document.addEventListener('mouseup', onPointerUp);

        // 触摸支持
        handle.addEventListener('touchstart', function(e) {
            if (e.target.closest('.m-dialog-close')) return;
            isDragging = true;
            dialog.classList.add('m-dialog-dragging');
            startX = e.touches[0].clientX - offsetX;
            startY = e.touches[0].clientY - offsetY;
        }, { passive: true });
        document.addEventListener('touchmove', function(e) {
            if (!isDragging) return;
            offsetX = e.touches[0].clientX - startX;
            offsetY = e.touches[0].clientY - startY;
            dialog.style.transform = 'translate(' + offsetX + 'px, ' + offsetY + 'px) scale(1)';
        }, { passive: true });
        document.addEventListener('touchend', onPointerUp);
    }

    /* ---- 快捷方法 ---- */

    /**
     * alert 替代（仅确定按钮）
     * MDialog.alert('操作成功！');
     * MDialog.alert({ message: '操作成功！', type: 'success' });
     */
    function alert(msgOrOpts) {
        var opts = typeof msgOrOpts === 'string' ? { message: msgOrOpts } : (msgOrOpts || {});
        opts.type = opts.type || 'alert';
        opts.title = opts.title || '提示';
        return show(opts);
    }

    /**
     * confirm 替代（确定/取消）
     * MDialog.confirm('确定要删除吗？', function() { ... });
     * MDialog.confirm({ message: '确定要删除吗？', onOk: function() {...} });
     */
    function confirm(msgOrOpts, onOk) {
        var opts;
        if (typeof msgOrOpts === 'string') {
            opts = { message: msgOrOpts, onOk: onOk };
        } else {
            opts = msgOrOpts || {};
        }
        opts.type = opts.type || 'confirm';
        opts.title = opts.title || '操作确认';
        opts.showCancel = true;
        opts.okType = opts.okType || 'warning';
        return show(opts);
    }

    /**
     * 危险操作确认（红色确定按钮）
     * MDialog.danger('确定要删除吗？此操作不可撤销！', function() { ... });
     */
    function danger(msgOrOpts, onOk) {
        var opts;
        if (typeof msgOrOpts === 'string') {
            opts = { message: msgOrOpts, onOk: onOk };
        } else {
            opts = msgOrOpts || {};
        }
        opts.type = opts.type || 'danger';
        opts.title = opts.title || '危险操作确认';
        opts.showCancel = true;
        opts.okText = opts.okText || '确认执行';
        opts.okType = 'danger';
        return show(opts);
    }

    /**
     * prompt 替代（带输入框）
     * MDialog.prompt('请输入名称：', function(val) { ... });
     */
    function prompt(msgOrOpts, onOk) {
        var opts;
        if (typeof msgOrOpts === 'string') {
            opts = { message: '<p>' + msgOrOpts + '</p>', onOk: onOk };
        } else {
            opts = msgOrOpts || {};
        }
        opts.type = opts.type || 'info';
        opts.title = opts.title || '请输入';
        opts.showCancel = true;
        opts.prompt = true;
        return show(opts);
    }

    return {
        show: show,
        close: close,
        alert: alert,
        confirm: confirm,
        danger: danger,
        prompt: prompt
    };
})();

// 兼容小写引用（部分页面使用 mdialog 而非 MDialog）
window.mdialog = MDialog;
