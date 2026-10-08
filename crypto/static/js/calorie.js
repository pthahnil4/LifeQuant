/**
 * 热量缺口管理 - 前端交互逻辑
 * ============================
 * 依赖：mdialog.js
 */

// =============================================================================
// 状态管理
// =============================================================================
let state = {
    records: [],
    dashboard: {},
    config: {},
    foods: [],
    foodCache: [],      // 食物搜索缓存
    editingId: null,    // 正在编辑的记录ID
    formCollapsed: false,
    mealFoods: { breakfast: [], lunch: [], dinner: [] },  // 每餐多食物列表
    // 从搜索建议选中的食物单位快照（点「添加」时随明细一并提交）
    mealPickUnit: { breakfast: '', lunch: '', dinner: '' },
    // 美食打卡点：列表数据 + 表单弹窗内的待传文件与已删照片暂存
    spots: [],
    spotEditingId: '',
    spotFormFiles: [],
    spotFormDeletedPhotos: []
};

// =============================================================================
// 工具函数
// =============================================================================
function formatNum(v, decimals) {
    if (v === null || v === undefined) return '0';
    decimals = decimals || 0;
    return Number(v).toLocaleString('zh-CN', { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
}

function formatDate(dateStr) {
    if (!dateStr) return '';
    const parts = dateStr.split('-');
    return parts[1] + '-' + parts[2];
}

/** HTML 文本转义（防注入，随笔/食物名均可能含特殊字符） */
function escHtml(s) {
    return String(s == null ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

/** HTML 属性转义（在 escHtml 基础上处理引号与换行，用于 title 提示） */
function escAttr(s) {
    return escHtml(s).replace(/"/g, '&quot;').replace(/\r?\n/g, ' ');
}

function todayStr() {
    const d = new Date();
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, '0');
    const day = String(d.getDate()).padStart(2, '0');
    return y + '-' + m + '-' + day;
}

// =============================================================================
// API 调用
// =============================================================================
const API = {
    async getRecords() {
        const res = await fetch('/calorie/api/records');
        return res.json();
    },
    async saveRecord(data) {
        const res = await fetch('/calorie/api/record', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data)
        });
        return res.json();
    },
    async deleteRecord(id) {
        const res = await fetch('/calorie/api/record/' + encodeURIComponent(id), {
            method: 'DELETE'
        });
        return res.json();
    },
    async getFoods(search, category) {
        let url = '/calorie/api/foods';
        const params = [];
        if (search) params.push('search=' + encodeURIComponent(search));
        if (category) params.push('category=' + encodeURIComponent(category));
        if (params.length) url += '?' + params.join('&');
        const res = await fetch(url);
        return res.json();
    },
    async addFood(data) {
        const res = await fetch('/calorie/api/food', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data)
        });
        return res.json();
    },
    async updateFood(id, data) {
        const res = await fetch('/calorie/api/food/' + encodeURIComponent(id), {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data)
        });
        return res.json();
    },
    async deleteFood(id) {
        const res = await fetch('/calorie/api/food/' + encodeURIComponent(id), {
            method: 'DELETE'
        });
        return res.json();
    },
    async getConfig() {
        const res = await fetch('/calorie/api/config');
        return res.json();
    },
    async updateConfig(data) {
        const res = await fetch('/calorie/api/config', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data)
        });
        return res.json();
    },
    async getCategories() {
        const res = await fetch('/calorie/api/categories');
        return res.json();
    },
    // ---- 美食打卡点 ----
    async getFoodSpots() {
        const res = await fetch('/calorie/api/food-spots');
        return res.json();
    },
    /** 创建/更新打卡点：multipart 提交。文件必须最先 append——Chrome 在
     *  multipart 末段为无文件名空片段时会解析错误（历史坑），故字段垫后。 */
    async saveFoodSpot(payload) {
        const fd = new FormData();
        (payload.files || []).forEach(f => fd.append('images[]', f));
        if (payload.id) fd.append('id', payload.id);
        fd.append('shop_name', payload.shop_name || '');
        fd.append('address', payload.address || '');
        fd.append('category', payload.category || '');
        fd.append('review', payload.review || '');
        const res = await fetch('/calorie/api/food-spot', { method: 'POST', body: fd });
        return res.json();
    },
    async deleteFoodSpot(id) {
        const res = await fetch('/calorie/api/food-spot/' + encodeURIComponent(id), {
            method: 'DELETE'
        });
        return res.json();
    },
    async deleteFoodSpotPhoto(photoId) {
        const res = await fetch('/calorie/api/food-spot/photo/' + encodeURIComponent(photoId), {
            method: 'DELETE'
        });
        return res.json();
    }
};

// =============================================================================
// 多食物选择管理
// =============================================================================
const foodSearchTimers = {};

/** 获取某餐的食物列表 */
function getMealFoods(meal) {
    return state.mealFoods[meal] || [];
}

/** 取明细数量（兼容旧数据：无 quantity 或非法值按 1 计） */
function foodQty(f) {
    const q = (typeof f.quantity === 'number') ? f.quantity : parseFloat(f.quantity);
    return (isFinite(q) && q > 0) ? q : 1;
}

/** 明细行总热量 = 单位热量 × 数量（旧数据无 quantity 时即原绝对热量） */
function foodTotalCal(f) {
    const t = (typeof f.total_calories === 'number') ? f.total_calories : parseFloat(f.total_calories);
    if (isFinite(t)) return t;
    return Math.round((f.calories || 0) * foodQty(f) * 100) / 100;
}

/** 计算某餐的总热量（按明细 total_calories 累加，保留2位避免浮点尾数） */
function calcMealCalories(meal) {
    const foods = getMealFoods(meal);
    return Math.round(foods.reduce((sum, f) => sum + foodTotalCal(f), 0) * 100) / 100;
}

/** 计算三餐总摄入 */
function calcTotalIntake() {
    return calcMealCalories('breakfast') + calcMealCalories('lunch') + calcMealCalories('dinner');
}

/** 渲染某餐的食物标签 */
function renderMealFoods(meal) {
    const container = document.getElementById(meal + '-foods');
    const totalEl = document.getElementById(meal + '-total');
    const foods = getMealFoods(meal);

    if (!container) return;

    if (!foods.length) {
        container.innerHTML = '<span style="color:var(--text-muted);font-size:0.8rem;">暂无食物</span>';
        if (totalEl) totalEl.textContent = '0 kcal';
        return;
    }

    let html = '';
    foods.forEach((f, idx) => {
        if (!f.name && !f.calories) return;
        const qty = foodQty(f);
        const total = foodTotalCal(f);
        // 标签展示「食物名 × 数量 (单位)」，数量可内联修改，总热量实时重算
        html += '<span class="meal-food-chip">' +
            '<span class="chip-name">' + escHtml(f.name || '食物') + '</span>' +
            '<span class="chip-qty-wrap">×<input class="chip-qty" type="number" min="0.1" step="0.1" value="' + qty + '"' +
            ' data-meal="' + meal + '" data-idx="' + idx + '" title="数量（支持小数，如 0.8）"' +
            ' onchange="setMealFoodQty(this.getAttribute(\'data-meal\'), +this.getAttribute(\'data-idx\'), this.value)"></span>' +
            (f.unit ? '<span class="chip-unit">(' + escHtml(f.unit) + ')</span>' : '') +
            '<span class="chip-cal">' + total + ' kcal</span>' +
            '<button class="chip-remove" onclick="removeMealFood(\'' + meal + '\', ' + idx + ')" title="移除">✕</button>' +
            '</span>';
    });
    container.innerHTML = html;

    const total = calcMealCalories(meal);
    if (totalEl) totalEl.textContent = total + ' kcal';
}

/** 修改某餐某条明细的数量，重算该行总热量并联动预览指标 */
function setMealFoodQty(meal, idx, value) {
    const list = getMealFoods(meal);
    const f = list[idx];
    if (!f) return;
    let qty = parseFloat(value);
    if (!isFinite(qty) || qty <= 0) qty = 1;
    f.quantity = Math.round(qty * 1000) / 1000;
    f.total_calories = Math.round((f.calories || 0) * f.quantity * 100) / 100;
    renderMealFoods(meal);
    updatePreview();
}

/** 向某餐添加食物（热量框填单位热量，数量×单位热量=该行总热量） */
function commitMealFood(meal) {
    const searchInput = document.querySelector('.meal-food-search[data-meal="' + meal + '"]');
    const calInput = document.querySelector('.meal-food-cal[data-meal="' + meal + '"]');
    const qtyInput = document.querySelector('.meal-food-qty[data-meal="' + meal + '"]');

    if (!searchInput) return;

    const name = searchInput.value.trim();
    const cal = parseFloat(calInput ? calInput.value : 0) || 0;
    let qty = parseFloat(qtyInput ? qtyInput.value : '');
    if (!isFinite(qty) || qty <= 0) qty = 1;
    qty = Math.round(qty * 1000) / 1000;

    if (!name && cal <= 0) return;

    state.mealFoods[meal].push({
        name: name || '食物',
        calories: cal,
        quantity: qty,
        unit: state.mealPickUnit[meal] || '',
        total_calories: Math.round(cal * qty * 100) / 100
    });

    searchInput.value = '';
    if (calInput) calInput.value = '';
    if (qtyInput) qtyInput.value = '1';
    state.mealPickUnit[meal] = '';

    // 隐藏建议列表
    const suggestEl = document.querySelector('.food-suggest[data-suggest="' + meal + '"]');
    if (suggestEl) suggestEl.style.display = 'none';

    renderMealFoods(meal);
    updatePreview();
}

/** 从某餐移除食物 */
function removeMealFood(meal, idx) {
    state.mealFoods[meal].splice(idx, 1);
    renderMealFoods(meal);
    updatePreview();
}

/** 设置某餐的食物列表（用于编辑回填，兼容无 quantity 的历史明细） */
function setMealFoods(meal, foods) {
    state.mealFoods[meal] = (foods || []).map(f => {
        const cal = typeof f.calories === 'number' ? f.calories : (parseFloat(f.calories) || 0);
        const qty = foodQty(f);
        return {
            name: f.name || '',
            calories: cal,
            quantity: qty,
            unit: f.unit || '',
            total_calories: Math.round(cal * qty * 100) / 100
        };
    });
    renderMealFoods(meal);
}

/** 重置某餐添加行的数量与单位快照 */
function resetMealAddRow(meal) {
    state.mealPickUnit[meal] = '';
    const qtyInput = document.querySelector('.meal-food-qty[data-meal="' + meal + '"]');
    if (qtyInput) qtyInput.value = '1';
}

/** 清空所有餐的食物列表 */
function clearAllMealFoods() {
    state.mealFoods = { breakfast: [], lunch: [], dinner: [] };
    ['breakfast', 'lunch', 'dinner'].forEach(resetMealAddRow);
    renderMealFoods('breakfast');
    renderMealFoods('lunch');
    renderMealFoods('dinner');
}

// =============================================================================
// 食物搜索自动补全（多餐通用）
// =============================================================================

function setupFoodSearch() {
    document.querySelectorAll('.meal-food-search').forEach(function(input) {
        const meal = input.getAttribute('data-meal');
        const suggest = document.querySelector('.food-suggest[data-suggest="' + meal + '"]');
        const calInput = document.querySelector('.meal-food-cal[data-meal="' + meal + '"]');

        if (!input || !suggest) return;

        let currentFoods = [];   // 当前建议列表对应的食物数组
        let activeIdx = -1;      // 键盘高亮的候选项索引

        function hideSuggest() {
            suggest.style.display = 'none';
            activeIdx = -1;
        }

        function setActive(idx) {
            const items = suggest.querySelectorAll('.s-item');
            items.forEach((el, i) => el.classList.toggle('active', i === idx));
            if (idx >= 0 && items[idx]) items[idx].scrollIntoView({ block: 'nearest' });
            activeIdx = idx;
        }

        /** 选中某个候选项：将食物名与单位热量回填输入框，数量重置为 1，
         *  由用户确认/修改数量后再点「添加」提交（不再自动提交清空输入框） */
        function pickFood(idx) {
            const f = currentFoods[idx];
            if (!f) return;
            const qtyInput = document.querySelector('.meal-food-qty[data-meal="' + meal + '"]');
            // 热量回填数值化：库中 calories 为字符串/缺失时转成有效数字，避免 NaN 写入输入框
            const fillCal = (typeof f.calories === 'number') ? f.calories : (parseFloat(f.calories) || 0);
            input.value = f.name;
            if (calInput) calInput.value = fillCal;
            if (qtyInput) qtyInput.value = '1';
            state.mealPickUnit[meal] = f.unit || '';
            hideSuggest();
            // 焦点移到数量框方便直接调整；不改数量时可直接点「添加」或再按回车提交
            if (qtyInput) { qtyInput.focus(); qtyInput.select(); }
        }

        /** 渲染建议列表（无结果时给出提示，回车可直接把手输食物入库） */
        function renderFoods(foods, keyword) {
            suggest.innerHTML = '';
            currentFoods = foods || [];
            activeIdx = -1;
            if (!currentFoods.length) {
                const tip = document.createElement('div');
                tip.className = 's-empty';
                tip.textContent = keyword
                    ? '未找到“' + keyword + '”，按回车直接添加该食物并自动存入食物库'
                    : '食物库为空，可手动输入添加';
                suggest.appendChild(tip);
                suggest.style.display = 'block';
                return;
            }
            currentFoods.forEach(function(f, idx) {
                const div = document.createElement('div');
                div.className = 's-item';
                div.innerHTML = '<span class="s-name"></span><span class="s-info"></span>';
                div.querySelector('.s-name').textContent = f.name;
                div.querySelector('.s-info').textContent = f.unit + ' \u00b7 ' + f.calories + ' kcal';
                div.addEventListener('click', function() { pickFood(idx); });
                suggest.appendChild(div);
            });
            suggest.style.display = 'block';
        }

        /** 查询食物库；空关键词返回前 10 条供快捷选择 */
        async function searchFoods(keyword) {
            try {
                const data = await API.getFoods(keyword, '');
                renderFoods(((data && data.foods) || []).slice(0, 10), keyword);
            } catch (e) {
                hideSuggest();
            }
        }

        // 鼠标在建议面板上按下时阻止默认行为，避免 input 失焦触发 blur 隐藏，
        // 保证点击候选项时回填逻辑不被吞掉（此前点击竞态是回填失效的主因之一）
        suggest.addEventListener('mousedown', function(e) { e.preventDefault(); });

        // 实时搜索（200ms 防抖）
        input.addEventListener('input', function() {
            const val = this.value.trim();
            if (foodSearchTimers[meal]) clearTimeout(foodSearchTimers[meal]);
            if (!val) {
                hideSuggest();
                return;
            }
            foodSearchTimers[meal] = setTimeout(function() { searchFoods(val); }, 200);
        });

        // 聚焦时：有内容则重新展示已有建议；空内容则拉取前 10 条食物供快捷选择
        input.addEventListener('focus', function() {
            if (this.value.trim()) {
                if (suggest.children.length) suggest.style.display = 'block';
            } else {
                searchFoods('');
            }
        });

        input.addEventListener('blur', function() {
            setTimeout(hideSuggest, 150);
        });

        // 键盘导航：↑↓ 选择、Enter 确认候选/提交手输、Esc 关闭
        input.addEventListener('keydown', function(e) {
            const visible = suggest.style.display === 'block';
            if (e.key === 'ArrowDown' && visible && currentFoods.length) {
                e.preventDefault();
                setActive((activeIdx + 1) % currentFoods.length);
            } else if (e.key === 'ArrowUp' && visible && currentFoods.length) {
                e.preventDefault();
                setActive(activeIdx <= 0 ? currentFoods.length - 1 : activeIdx - 1);
            } else if (e.key === 'Enter') {
                e.preventDefault();
                if (visible && currentFoods.length) {
                    pickFood(activeIdx >= 0 ? activeIdx : 0);
                } else {
                    hideSuggest();
                    commitMealFood(meal);
                }
            } else if (e.key === 'Escape') {
                hideSuggest();
            }
        });

        if (calInput) {
            calInput.addEventListener('keydown', function(e) {
                if (e.key === 'Enter') {
                    e.preventDefault();
                    commitMealFood(meal);
                }
            });
        }

        // 数量框回车同样直接提交该条明细
        const qtyEl = document.querySelector('.meal-food-qty[data-meal="' + meal + '"]');
        if (qtyEl) {
            qtyEl.addEventListener('keydown', function(e) {
                if (e.key === 'Enter') {
                    e.preventDefault();
                    commitMealFood(meal);
                }
            });
        }
    });
}

// =============================================================================
// 预览计算
// =============================================================================
function updatePreview() {
    const morningWeight = parseFloat(document.getElementById('morning-weight').value) || 0;
    const breakfastCal = calcMealCalories('breakfast');
    const lunchCal = calcMealCalories('lunch');
    const dinnerCal = calcMealCalories('dinner');
    const steps = parseInt(document.getElementById('daily-steps').value) || 0;

    const cfg = state.config;
    if (!cfg || !cfg.height) return;

    // BMR
    const bmr = (10 * morningWeight / 2) + (6.25 * cfg.height) - (5 * cfg.age) + 5;
    const totalIntake = breakfastCal + lunchCal + dinnerCal;
    const intakeDeficit = bmr - totalIntake;
    const exercise = steps * (cfg.step_frequency || 0.7) * (cfg.weight_factor || 55) / 1000;
    const deficit = intakeDeficit + exercise;

    const fmt = (v) => v.toFixed(1) + ' kcal';

    document.getElementById('pv-bmr').textContent = morningWeight > 0 ? fmt(bmr) : '-- kcal';
    document.getElementById('pv-intake').textContent = totalIntake > 0 ? fmt(totalIntake) : '-- kcal';
    document.getElementById('pv-intake-deficit').textContent = (morningWeight > 0 || totalIntake > 0)
        ? (intakeDeficit >= 0 ? '+' + fmt(intakeDeficit) : fmt(intakeDeficit))
        : '-- kcal';
    document.getElementById('pv-exercise').textContent = steps > 0 ? '+' + fmt(exercise) : '-- kcal';
    document.getElementById('pv-deficit').textContent = (morningWeight > 0 || steps > 0 || totalIntake > 0)
        ? (deficit >= 0 ? '+' + fmt(deficit) : fmt(deficit))
        : '-- kcal';

    // 颜色标记
    const pvDeficit = document.getElementById('pv-deficit');
    pvDeficit.className = 'pv-value' + (deficit > 0 ? ' positive' : deficit < 0 ? ' negative' : '');
}

// =============================================================================
// 表单折叠
// =============================================================================
function toggleForm() {
    state.formCollapsed = !state.formCollapsed;
    document.getElementById('form-body').style.display = state.formCollapsed ? 'none' : 'block';
    document.getElementById('form-arrow').className = 'arrow' + (state.formCollapsed ? '' : ' open');
}

// =============================================================================
// 表单操作
// =============================================================================

/** 随笔入口深链与表单当前日期联动：跳转随笔页时自动关联该日热量记录 */
function updateJournalEntryLink() {
    const link = document.getElementById('journal-entry-link');
    const date = document.getElementById('record-date').value || todayStr();
    if (link) link.href = '/journal?link=' + encodeURIComponent('calorie:' + date);
    // 日记本入口：同日期锚点 + 关联来源（日记页 ?date= 定位、?link= 回指热量记录）
    const diaryLink = document.getElementById('diary-entry-link');
    if (diaryLink) diaryLink.href = '/diary?date=' + encodeURIComponent(date)
        + '&link=' + encodeURIComponent('calorie:' + date);
}

function resetForm() {
    document.getElementById('record-date').value = todayStr();
    document.getElementById('morning-weight').value = '';
    document.getElementById('evening-weight').value = '';
    document.getElementById('daily-steps').value = '';
    clearAllMealFoods();
    state.editingId = null;
    document.getElementById('save-status').textContent = '';
    updatePreview();
    updateJournalEntryLink();
}

function fillForm(record) {
    document.getElementById('record-date').value = record.date || '';
    document.getElementById('morning-weight').value = record.morning_weight || '';
    document.getElementById('evening-weight').value = record.evening_weight || '';
    document.getElementById('daily-steps').value = record.daily_steps || '';
    updateJournalEntryLink();

    // 回填多食物数据（优先使用 foods 数组，兼容旧格式）
    if (record.breakfast_foods && record.breakfast_foods.length) {
        setMealFoods('breakfast', record.breakfast_foods);
    } else if (record.breakfast_food) {
        setMealFoods('breakfast', [{ name: record.breakfast_food, calories: record.breakfast_calories || 0 }]);
    } else {
        setMealFoods('breakfast', []);
    }

    if (record.lunch_foods && record.lunch_foods.length) {
        setMealFoods('lunch', record.lunch_foods);
    } else if (record.lunch_food) {
        setMealFoods('lunch', [{ name: record.lunch_food, calories: record.lunch_calories || 0 }]);
    } else {
        setMealFoods('lunch', []);
    }

    if (record.dinner_foods && record.dinner_foods.length) {
        setMealFoods('dinner', record.dinner_foods);
    } else if (record.dinner_food) {
        setMealFoods('dinner', [{ name: record.dinner_food, calories: record.dinner_calories || 0 }]);
    } else {
        setMealFoods('dinner', []);
    }

    state.editingId = record.id;
    document.getElementById('save-status').textContent = '\u270f\ufe0f \u7f16\u8f91\u4e2d: ' + record.date;
    // 展开表单
    if (state.formCollapsed) toggleForm();
    updatePreview();
}

async function saveRecord() {
    const date = document.getElementById('record-date').value;
    if (!date) {
        alert('请选择日期');
        return;
    }

    const data = {
        date: date,
        morning_weight: document.getElementById('morning-weight').value || null,
        evening_weight: document.getElementById('evening-weight').value || null,
        breakfast_foods: state.mealFoods.breakfast,
        breakfast_calories: calcMealCalories('breakfast'),
        lunch_foods: state.mealFoods.lunch,
        lunch_calories: calcMealCalories('lunch'),
        dinner_foods: state.mealFoods.dinner,
        dinner_calories: calcMealCalories('dinner'),
        daily_steps: document.getElementById('daily-steps').value || 0
    };

    const result = await API.saveRecord(data);
    if (result.success) {
        state.records = result.records || state.records;
        // 更新记录列表中的记录
        if (result.record) {
            const idx = state.records.findIndex(r => r.id === result.record.id);
            if (idx >= 0) {
                state.records[idx] = result.record;
            } else {
                state.records.unshift(result.record);
            }
        }
        state.dashboard = result.dashboard || state.dashboard;
        recalcCumulativeLocal();
        renderDashboard();
        renderHistory();
        renderTrendChart();
        resetForm();
        document.getElementById('save-status').textContent = '✅ 保存成功';
        setTimeout(() => { document.getElementById('save-status').textContent = ''; }, 2000);
    } else {
        alert('保存失败: ' + (result.error || '未知错误'));
    }
}

async function deleteRecord(id) {
    if (!confirm('确定要删除这条记录吗？')) return;
    const result = await API.deleteRecord(id);
    if (result.success) {
        state.records = state.records.filter(r => r.id !== id);
        state.dashboard = result.dashboard || state.dashboard;
        recalcCumulativeLocal();
        renderDashboard();
        renderHistory();
        renderTrendChart();
        if (state.editingId === id) resetForm();
    } else {
        alert('删除失败: ' + (result.error || '未知错误'));
    }
}

// =============================================================================
// 局部刷新：本地重算累计缺口链（镜像后端 calorie_routes._recalc_cumulative）
// =============================================================================
// save 只回单条 record、delete 只回 dashboard，其余记录的 cumulative_deficit 会残留
// 旧值（累计是按日期前缀和，增删一条会改变其后所有记录），故本地统一重算：
// 按日期升序做 calorie_deficit 前缀和，写回每条 cumulative_deficit（保留2位，与后端一致：
// 累加过程不舍入、仅存储时舍入）。sorted 内为同一对象引用，就地修改即同步 state.records。
function recalcCumulativeLocal() {
    const sorted = (state.records || []).slice().sort((a, b) => (a.date || '').localeCompare(b.date || ''));
    let cumulative = 0;
    sorted.forEach(r => {
        cumulative += (r.calorie_deficit || 0);
        r.cumulative_deficit = Math.round(cumulative * 100) / 100;
    });
}

// =============================================================================
// 渲染统计看板
// =============================================================================
function renderDashboard() {
    const d = state.dashboard || {};
    document.getElementById('ov-total-deficit').textContent = formatNum(d.total_calorie_deficit, 1);
    document.getElementById('ov-cumulative').textContent = formatNum(d.cumulative_deficit, 1);
    document.getElementById('ov-progress').textContent = formatNum(d.current_progress, 1) + '%';
    document.getElementById('ov-progress-bar').style.width = Math.min(d.current_progress || 0, 100) + '%';
    // 目标文案跟随配置动态刷新，避免改配置后页面仍显示旧目标
    const targetDeficit = (state.config && state.config.target_deficit) || 100000;
    document.getElementById('ov-progress-label').textContent = '目标完成进度（目标 ' + formatNum(targetDeficit, 0) + ' kcal）';
    document.getElementById('ov-count').textContent = d.record_count || 0;
    document.getElementById('ov-avg-deficit').textContent = formatNum(d.avg_calorie_deficit, 1);
    document.getElementById('ov-avg-intake').textContent = formatNum(d.avg_total_intake, 1);
    document.getElementById('ov-avg-steps').textContent = formatNum(d.avg_daily_steps);
    document.getElementById('ov-avg-exercise').textContent = formatNum(d.avg_exercise_calories, 1);
    document.getElementById('ov-fat-loss').textContent = formatNum(d.total_fat_loss, 3);
    renderEta(d);
}

// =============================================================================
// 预计目标达成日期（后端 CalorieCalculator.calc_completion_estimate 推算）
// =============================================================================
// 口径：剩余缺口 = 总目标 - 已累积缺口；还需天数 = 剩余缺口 ÷ 历史日均缺口（向上取整），
// 达成日期 = 今天 + 还需天数。日均缺口 ≤0 时后端返回 null（无记录或整体净盈余），
// 前端显示「暂不可推算」；剩余缺口为 0 视为已达成。
function renderEta(d) {
    const numEl = document.getElementById('ov-eta-date');
    const labelEl = document.getElementById('ov-eta-label');
    if (!numEl || !labelEl) return;
    const remaining = Number(d.remaining_deficit || 0);
    const days = d.est_days_remaining;
    const avg = Number(d.avg_calorie_deficit || 0);
    // 小字固定以「预计达成」开头保留卡片语义，推算细节放 title 悬停提示，避免窄屏折行拉高卡片
    labelEl.title = '剩余缺口 ' + formatNum(remaining, 0) + ' kcal ÷ 历史日均缺口 ' + formatNum(avg, 0) + ' kcal';
    if (remaining <= 0) {
        numEl.textContent = '已达成';
        labelEl.textContent = '预计达成 · 累积缺口已达标 🎉';
        return;
    }
    if (days === null || days === undefined) {
        numEl.textContent = '待记录';
        labelEl.textContent = '预计达成 · 暂不可推算';
        return;
    }
    numEl.textContent = d.est_completion_date || '--';
    labelEl.textContent = '预计达成 · 还需 ' + days + ' 天';
}

// =============================================================================
// 数据趋势折线图（早/晚体重 + 总热量缺口累计值）
// =============================================================================
let trendChart = null;

function renderTrendChart() {
    const container = document.getElementById('trend-chart-container');
    if (!container) return;

    const records = (state.records || []).slice().sort((a, b) => (a.date || '').localeCompare(b.date || ''));
    if (!records.length) {
        if (trendChart) { trendChart.dispose(); trendChart = null; }
        container.innerHTML = '<div class="trend-empty">📉 暂无数据，保存第一条记录后即可查看趋势图</div>';
        return;
    }
    // 容器可能被替换为空状态提示，需还原图表 DOM
    if (!document.getElementById('trend-chart')) {
        container.innerHTML = '<div class="trend-chart-wrap" id="trend-chart"></div>';
    }
    const chartEl = document.getElementById('trend-chart');

    const dates = records.map(r => r.date || '');
    const morning = records.map(r => (r.morning_weight !== null && r.morning_weight !== undefined && r.morning_weight !== '') ? +Number(r.morning_weight).toFixed(1) : null);
    const evening = records.map(r => (r.evening_weight !== null && r.evening_weight !== undefined && r.evening_weight !== '') ? +Number(r.evening_weight).toFixed(1) : null);
    const cumulative = records.map(r => +Number(r.cumulative_deficit || 0).toFixed(1));

    if (!trendChart) {
        trendChart = echarts.init(chartEl);
        window.addEventListener('resize', () => { if (trendChart) trendChart.resize(); });
    }
    trendChart.setOption({
        backgroundColor: 'transparent',
        tooltip: {
            trigger: 'axis',
            axisPointer: { type: 'cross', label: { backgroundColor: '#41617f' } },
            formatter: function(params) {
                let html = '<div style="font-weight:600;margin-bottom:4px;">' + params[0].axisValue + '</div>';
                params.forEach(p => {
                    if (p.value === null || p.value === undefined) return;
                    const unit = p.seriesName === '总热量缺口累计' ? ' kcal' : ' kg';
                    html += p.marker + ' ' + p.seriesName + '：<b>' + p.value + unit + '</b><br>';
                });
                return html;
            }
        },
        legend: { data: ['早上体重', '晚上体重', '总热量缺口累计'], top: 4, icon: 'roundRect' },
        grid: { left: 52, right: 64, top: 40, bottom: 56 },
        xAxis: {
            type: 'category', data: dates, boundaryGap: false,
            axisLabel: { fontSize: 10, color: '#8aa3ba', formatter: v => v.slice(5) },
            axisLine: { lineStyle: { color: '#dfe9f3' } }
        },
        yAxis: [
            {
                type: 'value', name: '体重 (kg)', position: 'left', scale: true,
                axisLabel: { fontSize: 10, color: '#8aa3ba' },
                splitLine: { lineStyle: { type: 'dashed', color: '#eef2f7' } }
            },
            {
                type: 'value', name: '缺口 (kcal)', position: 'right',
                axisLabel: { fontSize: 10, color: '#8aa3ba' },
                splitLine: { show: false }
            }
        ],
        dataZoom: [
            { type: 'inside', xAxisIndex: 0 },
            { type: 'slider', xAxisIndex: 0, height: 18, bottom: 8, borderColor: '#dfe9f3', fillerColor: 'rgba(11,95,168,0.12)' }
        ],
        series: [
            {
                name: '早上体重', type: 'line', data: morning, smooth: true,
                connectNulls: true, symbolSize: 5,
                lineStyle: { width: 2.2, color: '#ff9f43' }, itemStyle: { color: '#ff9f43' }
            },
            {
                name: '晚上体重', type: 'line', data: evening, smooth: true,
                connectNulls: true, symbolSize: 5,
                lineStyle: { width: 2.2, color: '#5f27cd' }, itemStyle: { color: '#5f27cd' }
            },
            {
                name: '总热量缺口累计', type: 'line', yAxisIndex: 1, data: cumulative, smooth: true,
                showSymbol: false, lineStyle: { width: 2, color: '#10ac84' }, itemStyle: { color: '#10ac84' },
                areaStyle: {
                    color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
                        { offset: 0, color: 'rgba(16,172,132,0.22)' },
                        { offset: 1, color: 'rgba(16,172,132,0.02)' }
                    ])
                }
            }
        ]
    }, true);
    trendChart.resize();
}

// =============================================================================
// 渲染历史记录
// =============================================================================
function renderHistory() {
    const container = document.getElementById('history-container');
    const records = state.records || [];
    const countEl = document.getElementById('history-count');
    if (countEl) countEl.textContent = records.length + ' 条';

    if (!records.length) {
        container.innerHTML = '<div class="empty-state"><div class="empty-icon">📊</div><div class="empty-text">暂无记录，开始记录你的第一条数据吧</div></div>';
        return;
    }

    let html = '<div class="history-table-wrap"><table class="history-table">';
    html += '<thead><tr>' +
        '<th>日期</th>' +
        '<th>早重</th>' +
        '<th>晚重</th>' +
        '<th>BMR</th>' +
        '<th>早餐</th>' +
        '<th>午餐</th>' +
        '<th>晚餐</th>' +
        '<th>摄入</th>' +
        '<th>摄入缺口</th>' +
        '<th>步数</th>' +
        '<th>运动</th>' +
        '<th>🔥缺口</th>' +
        '<th>累积缺口</th>' +
        '<th>操作</th>' +
        '</tr></thead><tbody>';

    for (const r of records) {
        // 显示食物名称与数量（优先使用 foods 数组，兼容旧格式；数量非 1 时后缀 ×N）
        const foodLabel = f => (f.name || '') + (foodQty(f) !== 1 ? ' ×' + foodQty(f) : '');
        const bf = r.breakfast_foods && r.breakfast_foods.length
            ? r.breakfast_foods.map(foodLabel).filter(Boolean).join(', ')
            : (r.breakfast_food || '');
        const lf = r.lunch_foods && r.lunch_foods.length
            ? r.lunch_foods.map(foodLabel).filter(Boolean).join(', ')
            : (r.lunch_food || '');
        const df = r.dinner_foods && r.dinner_foods.length
            ? r.dinner_foods.map(foodLabel).filter(Boolean).join(', ')
            : (r.dinner_food || '');

        const totalIntake = ((r.breakfast_calories || 0) + (r.lunch_calories || 0) + (r.dinner_calories || 0));
        const deficit = r.calorie_deficit || 0;
        const deficitClass = deficit >= 0 ? 'positive' : 'negative';
        const intakeDeficitClass = (r.intake_deficit || 0) >= 0 ? 'positive' : 'negative';

        html += '<tr>' +
            '<td>' + (r.date || '') + '</td>' +
            '<td class="num">' + (r.morning_weight ? formatNum(r.morning_weight, 1) : '-') + '</td>' +
            '<td class="num">' + (r.evening_weight ? formatNum(r.evening_weight, 1) : '-') + '</td>' +
            '<td class="num">' + (r.bmr ? formatNum(r.bmr, 1) : '-') + '</td>' +
            '<td class="food-cell" title="' + bf + '">' + (bf || '-') + '</td>' +
            '<td class="food-cell" title="' + lf + '">' + (lf || '-') + '</td>' +
            '<td class="food-cell" title="' + df + '">' + (df || '-') + '</td>' +
            '<td class="num">' + formatNum(totalIntake, 1) + '</td>' +
            '<td class="num ' + intakeDeficitClass + '">' + ((r.intake_deficit || 0) >= 0 ? '+' : '') + formatNum(r.intake_deficit || 0, 1) + '</td>' +
            '<td class="num">' + (r.daily_steps ? formatNum(r.daily_steps) : '-') + '</td>' +
            '<td class="num">' + (r.exercise_calories ? '+' + formatNum(r.exercise_calories, 1) : '-') + '</td>' +
            '<td class="num ' + deficitClass + '">' + (deficit >= 0 ? '+' : '') + formatNum(deficit, 1) + '</td>' +
            '<td class="num">' + formatNum(r.cumulative_deficit || 0, 1) + '</td>' +
            '<td>' +
            '<a class="action-btn" href="/journal?link=calorie:' + encodeURIComponent(r.date || '') + '" target="_blank" title="为这天补记减肥随笔（关联当日热量记录）">✍️</a> ' +
            '<button class="action-btn" onclick="fillForm(state.records.find(rec => rec.id===\'' + r.id + '\'))" title="编辑">\u270f\ufe0f</button> ' +
            '<button class="action-btn danger" onclick="deleteRecord(\'' + r.id + '\')" title="删除">\ud83d\uddd1\ufe0f</button>' +
            '</td>' +
            '</tr>';
    }

    html += '</tbody></table></div>';
    container.innerHTML = html;
}

// =============================================================================
// 食物库管理弹窗
// =============================================================================

/** 食物分类 → CSS 标签类映射 */
const FOOD_CAT_CLASS = {
    '主食': 'cat-staple',
    '肉蛋': 'cat-protein',
    '蔬菜': 'cat-veggie',
    '水果': 'cat-fruit',
    '乳制品': 'cat-dairy',
    '零食': 'cat-snack',
    '饮品': 'cat-drink'
};

function foodCatTag(category) {
    const cat = category || '其他';
    const cls = FOOD_CAT_CLASS[cat] || 'cat-other';
    return '<span class="cat-tag ' + cls + '">' + cat + '</span>';
}

async function openFoodManager() {
    const data = await API.getFoods('', '');
    const foods = data.foods || [];
    state.foodCache = foods;

    let html = '<div>';
    html += '<div class="food-mgr-search">';
    html += '<input type="text" id="food-search-input" placeholder="搜索食物名称..." oninput="foodManagerSearch()">';
    html += '<select id="food-category-filter" onchange="foodManagerSearch()">';
    html += '<option value="">全部分类</option>';
    // 收集分类
    const cats = new Set();
    foods.forEach(f => { if (f.category) cats.add(f.category); });
    const sortedCats = Array.from(cats).sort();
    sortedCats.forEach(c => { html += '<option value="' + c + '">' + c + '</option>'; });
    html += '</select>';
    html += '<button class="m-btn m-btn-primary m-btn-sm" onclick="showAddFoodDialog()" style="white-space:nowrap;">+ 新增食物</button>';
    html += '</div>';

    html += '<div id="food-list-container" style="max-height:400px;overflow-y:auto;">';
    html += renderFoodTable(foods);
    html += '</div>';
    html += '<div class="food-mgr-count" id="food-mgr-count">共 ' + foods.length + ' 种食物</div>';
    html += '</div>';

    mdialog.show({
        title: '📖 食物热量库管理',
        content: html,
        width: '750px',
        buttons: [{ text: '关闭', type: 'cancel', onClick: () => mdialog.close() }]
    });
}

function renderFoodTable(foods) {
    if (!foods || !foods.length) {
        return '<div style="text-align:center;padding:30px;color:#999;">暂无食物数据</div>';
    }
    let html = '<table class="food-mgr-table"><thead><tr>' +
        '<th>食物名称</th><th>单位</th><th>热量 (kcal)</th><th>分类</th><th>操作</th>' +
        '</tr></thead><tbody>';
    for (const f of foods) {
        html += '<tr>' +
            '<td style="font-weight:500;color:#1c3d5a;">' + f.name + '</td>' +
            '<td>' + f.unit + '</td>' +
            '<td style="font-variant-numeric:tabular-nums;font-weight:600;">' + f.calories + '</td>' +
            '<td>' + foodCatTag(f.category) + '</td>' +
            '<td><div class="food-mgr-actions">' +
            '<button onclick="showEditFoodDialog(\'' + f.id + '\')">✏️ 编辑</button>' +
            '<button class="del" onclick="deleteFoodItem(\'' + f.id + '\')">🗑️ 删除</button>' +
            '</div></td>' +
            '</tr>';
    }
    html += '</tbody></table>';
    return html;
}

async function foodManagerSearch() {
    const search = document.getElementById('food-search-input').value.trim();
    const category = document.getElementById('food-category-filter').value;
    const data = await API.getFoods(search, category);
    document.getElementById('food-list-container').innerHTML = renderFoodTable(data.foods || []);
}

function showAddFoodDialog() {
    const units = ['100克','10克','30克','28克','20克','6克','5克','1个','1份','1片','1根','1杯','1袋','250毫升','500毫升','5串','10颗','15个','22个','10下'];
    const html = '<div class="config-grid">' +
        '<div class="config-group"><label>食物名称</label><input type="text" id="add-food-name" placeholder="如 苹果"></div>' +
        '<div class="config-group"><label>单位</label><select id="add-food-unit">' +
        units.map(u => '<option value="' + u + '">' + u + '</option>').join('') +
        '</select></div>' +
        '<div class="config-group"><label>热量 (kcal)</label><input type="number" id="add-food-cal" step="0.1" placeholder="如 116"></div>' +
        '<div class="config-group"><label>分类</label><select id="add-food-cat"><option value="">其他</option><option value="主食">主食</option><option value="肉蛋">肉蛋</option><option value="蔬菜">蔬菜</option><option value="水果">水果</option><option value="乳制品">乳制品</option><option value="零食">零食</option><option value="饮品">饮品</option></select></div>' +
        '</div>';

    mdialog.show({
        title: '新增食物',
        content: html,
        buttons: [
            { text: '取消', type: 'cancel', onClick: () => mdialog.close() },
            { text: '✅ 保存', type: 'primary', onClick: async () => {
                const name = document.getElementById('add-food-name').value.trim();
                if (!name) { alert('食物名称不能为空'); return; }
                const data = {
                    name: name,
                    unit: document.getElementById('add-food-unit').value,
                    calories: parseFloat(document.getElementById('add-food-cal').value) || 0,
                    category: document.getElementById('add-food-cat').value || '其他'
                };
                const result = await API.addFood(data);
                if (result.success) {
                    mdialog.close();
                    openFoodManager();
                } else {
                    alert(result.error || '添加失败');
                }
            }}
        ]
    });
}

function showEditFoodDialog(foodId) {
    const food = state.foodCache.find(f => f.id === foodId);
    if (!food) return;

    const html = '<div class="config-grid">' +
        '<div class="config-group"><label>食物名称</label><input type="text" id="edit-food-name" value="' + food.name + '"></div>' +
        '<div class="config-group"><label>单位</label><select id="edit-food-unit">' +
        ['100克','10克','1个','1份','250毫升','1片','1根','1杯','500毫升'].map(u =>
            '<option value="' + u + '"' + (u === food.unit ? ' selected' : '') + '>' + u + '</option>'
        ).join('') +
        '</select></div>' +
        '<div class="config-group"><label>热量 (kcal)</label><input type="number" id="edit-food-cal" step="0.1" value="' + food.calories + '"></div>' +
        '<div class="config-group"><label>分类</label><select id="edit-food-cat">' +
        ['','主食','肉蛋','蔬菜','水果','乳制品','零食','饮品'].map(c =>
            '<option value="' + c + '"' + (c === (food.category || '') ? ' selected' : '') + '>' + (c || '其他') + '</option>'
        ).join('') +
        '</select></div>' +
        '</div>';

    mdialog.show({
        title: '编辑食物 - ' + food.name,
        content: html,
        buttons: [
            { text: '取消', type: 'cancel', onClick: () => mdialog.close() },
            { text: '✅ 保存', type: 'primary', onClick: async () => {
                const name = document.getElementById('edit-food-name').value.trim();
                if (!name) { alert('食物名称不能为空'); return; }
                const data = {
                    name: name,
                    unit: document.getElementById('edit-food-unit').value,
                    calories: parseFloat(document.getElementById('edit-food-cal').value) || 0,
                    category: document.getElementById('edit-food-cat').value || '其他'
                };
                const result = await API.updateFood(foodId, data);
                if (result.success) {
                    mdialog.close();
                    openFoodManager();
                } else {
                    alert(result.error || '更新失败');
                }
            }}
        ]
    });
}

async function deleteFoodItem(foodId) {
    if (!confirm('确定要删除这个食物吗？')) return;
    const result = await API.deleteFood(foodId);
    if (result.success) {
        openFoodManager();
    } else {
        alert(result.error || '删除失败');
    }
}

// =============================================================================
// 配置管理弹窗
// =============================================================================
async function openConfig() {
    const cfg = state.config || {};

    const html = '<div class="config-grid">' +
        '<div class="config-group"><label>身高 (cm)</label><input type="number" id="cfg-height" step="0.1" value="' + (cfg.height || 169) + '"></div>' +
        '<div class="config-group"><label>年龄</label><input type="number" id="cfg-age" value="' + (cfg.age || 29) + '"></div>' +
        '<div class="config-group"><label>步频系数</label><input type="number" id="cfg-step-freq" step="0.01" value="' + (cfg.step_frequency || 0.7) + '"></div>' +
        '<div class="config-group"><label>体重系数 (kg)</label><input type="number" id="cfg-weight-factor" step="0.1" value="' + (cfg.weight_factor || 55) + '"></div>' +
        '<div class="config-group"><label>总目标缺口 (kcal)</label><input type="number" id="cfg-target" value="' + (cfg.target_deficit || 100000) + '"></div>' +
        '<div class="config-group config-tip">1kg 体脂 ≈ 7700 kcal，10kg 目标 ≈ 77000 kcal，当前目标 100000 kcal（约13kg）</div>' +
        '</div>';

    mdialog.show({
        title: '⚙️ 参数配置',
        content: html,
        buttons: [
            { text: '取消', type: 'cancel', onClick: () => mdialog.close() },
            { text: '✅ 保存', type: 'primary', onClick: async () => {
                const data = {
                    height: parseFloat(document.getElementById('cfg-height').value) || 169,
                    age: parseInt(document.getElementById('cfg-age').value) || 29,
                    step_frequency: parseFloat(document.getElementById('cfg-step-freq').value) || 0.7,
                    weight_factor: parseFloat(document.getElementById('cfg-weight-factor').value) || 55,
                    target_deficit: parseFloat(document.getElementById('cfg-target').value) || 100000
                };
                const result = await API.updateConfig(data);
                if (result.success) {
                    state.config = result.config || data;
                    state.dashboard = result.dashboard || state.dashboard;
                    renderDashboard();
                    renderHistory();
                    updatePreview();
                    mdialog.close();
                } else {
                    alert('保存失败: ' + (result.error || '未知错误'));
                }
            }}
        ]
    });
}

// =============================================================================
// 美食打卡点（探店记录 + 照片）
// =============================================================================

// 常用美食种类候选（datalist 可自由输入，不在列表内的种类直接采用输入值）
const SPOT_CATEGORY_PRESETS = ['火锅', '烧烤', '日料', '川菜', '粤菜', '小吃快餐',
    '面馆', '西餐', '自助餐', '甜品饮品', '农家菜', '其他'];

/** 滚动到指定元素：手写查找首个可滚动祖先再滚动，避免 scrollIntoView({block:'nearest'})
 *  在某些布局下劫持整页滚动（本项目已知的响应式坑） */
function scrollElIntoViewSafely(el) {
    if (!el) return;
    let parent = el.parentElement;
    while (parent && parent !== document.body) {
        const style = getComputedStyle(parent);
        if (/(auto|scroll)/.test(style.overflowY)
            && parent.scrollHeight > parent.clientHeight) {
            const top = parent.getBoundingClientRect().top
                + window.scrollY - el.getBoundingClientRect().top
                - parent.clientHeight / 2 + el.clientHeight / 2;
            parent.scrollTo({ top: Math.max(0, top), behavior: 'smooth' });
            return;
        }
        parent = parent.parentElement;
    }
    el.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function scrollToFoodSpotPanel(event) {
    if (event) event.preventDefault();
    scrollElIntoViewSafely(document.getElementById('food-spot-panel'));
}

function renderFoodSpots() {
    const container = document.getElementById('spot-container');
    const countEl = document.getElementById('spot-count');
    if (!container) return;
    const spots = state.spots || [];
    if (countEl) countEl.textContent = spots.length + ' 家';

    if (!spots.length) {
        container.innerHTML = '<div class="empty-state"><div class="empty-icon">🍜</div>'
            + '<div class="empty-text">还没有打卡记录，去吃到好吃的就记一家吧</div></div>';
        return;
    }

    let html = '<div class="spot-grid">';
    for (const s of spots) {
        const photos = (s.photos || []).map(p =>
            '<img src="' + escAttr(p.url) + '" alt="' + escAttr(p.original_name || '照片') + '"'
            + ' title="' + escAttr(p.original_name || '') + '"'
            + ' onclick="openSpotLightbox(\'' + escAttr(p.url) + '\')" loading="lazy">').join('');
        html += '<div class="spot-card">' +
            '<div class="spot-head">' +
            '<span class="spot-shop" title="' + escAttr(s.shop_name) + '">' + escHtml(s.shop_name) + '</span>' +
            (s.category ? '<span class="spot-cat">' + escHtml(s.category) + '</span>' : '') +
            '</div>' +
            (s.address ? '<div class="spot-addr">📮 ' + escHtml(s.address) + '</div>' : '') +
            (photos ? '<div class="spot-thumbs">' + photos + '</div>' : '') +
            (s.review ? '<div class="spot-review">' + escHtml(s.review) + '</div>' : '') +
            '<div class="spot-date">🕐 ' + escHtml((s.created_at || '').slice(0, 10)) + '</div>' +
            '<div class="spot-card-actions">' +
            '<button onclick="openFoodSpotForm(\'' + escAttr(s.id) + '\')">✏️ 编辑</button>' +
            '<button class="del" onclick="deleteFoodSpot(\'' + escAttr(s.id) + '\')">🗑️ 删除</button>' +
            '</div>' +
            '</div>';
    }
    html += '</div>';
    container.innerHTML = html;
}

/** 轻量大图查看：点遮罩关闭 */
function openSpotLightbox(url) {
    if (!url) return;
    const box = document.createElement('div');
    box.className = 'spot-lightbox';
    box.innerHTML = '<img src="' + escAttr(url) + '" alt="打卡照片">';
    box.addEventListener('click', function() { box.remove(); });
    document.body.appendChild(box);
}

async function loadFoodSpots() {
    try {
        const data = await API.getFoodSpots();
        if (data.success) {
            state.spots = data.spots || [];
            renderFoodSpots();
        }
    } catch (e) { /* 列表加载失败不打断页面其他功能 */ }
}

/** 重建弹窗内的照片预览区：已入库照片（可移除） + 本次新选文件（可撤下） */
function renderSpotFormPreviews() {
    const strip = document.getElementById('spot-preview-strip');
    if (!strip) return;
    let html = '';
    if (state.spotEditingId) {
        const cur = (state.spots || []).find(s => s.id === state.spotEditingId);
        const existing = ((cur && cur.photos) || [])
            .filter(p => state.spotFormDeletedPhotos.indexOf(p.id) < 0);
        existing.forEach(p => {
            html += '<div class="spot-preview-item">' +
                '<img src="' + escAttr(p.url) + '" alt="已上传照片">' +
                '<span class="pv-tag">已上传</span>' +
                '<button class="pv-remove" title="移除这张照片" '
                + 'onclick="removeSpotExistingPhoto(\'' + escAttr(p.id) + '\')">✕</button>' +
                '</div>';
        });
    }
    state.spotFormFiles.forEach((f, idx) => {
        const url = URL.createObjectURL(f);
        html += '<div class="spot-preview-item">' +
            '<img src="' + url + '" alt="' + escAttr(f.name) + '">' +
            '<span class="pv-tag">新选择</span>' +
            '<button class="pv-remove" title="撤下" onclick="removeSpotFormFile(' + idx + ')">✕</button>' +
            '</div>';
    });
    strip.innerHTML = html || '<span class="spot-file-tip">尚未选择照片</span>';
}

function onSpotFilesPicked(input) {
    const files = Array.from(input.files || []);
    const remain = 9 - state.spotFormFiles.length;
    if (files.length > remain) alert('单次最多添加 9 张照片，超出部分已忽略');
    state.spotFormFiles = state.spotFormFiles.concat(files.slice(0, Math.max(0, remain)));
    input.value = '';
    renderSpotFormPreviews();
}

function removeSpotFormFile(idx) {
    state.spotFormFiles.splice(idx, 1);
    renderSpotFormPreviews();
}

/** 编辑时点掉已上传照片：立即调 DELETE 接口删库删文件，预览同步移除 */
async function removeSpotExistingPhoto(photoId) {
    const result = await API.deleteFoodSpotPhoto(photoId);
    if (result.success) {
        state.spotFormDeletedPhotos.push(photoId);
        // 列表缓存同步摘掉，重开弹窗不显示死链
        const cur = (state.spots || []).find(s => (s.photos || []).some(p => p.id === photoId));
        if (cur) cur.photos = cur.photos.filter(p => p.id !== photoId);
        renderSpotFormPreviews();
        renderFoodSpots();
    } else {
        alert(result.error || '照片移除失败');
    }
}

/**
 * 打开新增/编辑打卡弹窗（multipart 提交：文本字段 + images[] 一次带过去）。
 * spotId 为空 = 新增；否则回填该打卡点的文本字段与已上传照片。
 */
function openFoodSpotForm(spotId) {
    const cur = spotId ? (state.spots || []).find(s => s.id === spotId) : null;
    if (spotId && !cur) return;
    state.spotEditingId = spotId || '';
    state.spotFormFiles = [];
    state.spotFormDeletedPhotos = [];

    const catOptions = SPOT_CATEGORY_PRESETS.map(c => '<option value="' + escAttr(c) + '"></option>').join('');
    const html = '<div class="config-grid">' +
        '<div class="config-group"><label>店铺名称 *</label>'
        + '<input type="text" id="spot-f-shop" maxlength="128" placeholder="如 老街牛肉面馆" value="' + escAttr(cur ? cur.shop_name : '') + '"></div>' +
        '<div class="config-group"><label>美食种类/分类</label>'
        + '<input type="text" id="spot-f-cat" maxlength="64" list="spot-cat-list" placeholder="下拉可选，也可自由输入" value="' + escAttr(cur ? cur.category : '') + '">'
        + '<datalist id="spot-cat-list">' + catOptions + '</datalist></div>' +
        '<div class="config-group spot-form-full"><label>详细地址</label>'
        + '<input type="text" id="spot-f-addr" maxlength="255" placeholder="如 XX路XX号XX商场3楼" value="' + escAttr(cur ? cur.address : '') + '"></div>' +
        '<div class="config-group spot-form-full"><label>个人点评/心得</label>'
        + '<textarea id="spot-f-review" rows="4" placeholder="味道、性价比、服务、推荐菜……" style="padding:8px 12px;border:1.5px solid #e2e9f1;border-radius:9px;font-size:.9rem;outline:none;font-family:inherit;resize:vertical;">' + escHtml(cur ? cur.review : '') + '</textarea></div>' +
        '<div class="config-group spot-form-full"><label>照片（美食特写 / 店铺环境 / 地图截图等，最多9张，单张≤8MB）</label>'
        + '<div class="spot-file-row"><input type="file" id="spot-f-files" accept="image/*" multiple onchange="onSpotFilesPicked(this)">'
        + '<span class="spot-file-tip">支持 jpg / png / gif / webp / bmp</span></div>'
        + '<div class="spot-preview-strip" id="spot-preview-strip"></div></div>' +
        '</div>';

    mdialog.show({
        title: (spotId ? '✏️ 编辑打卡点' : '📍 新增美食打卡点'),
        content: html,
        width: '640px',
        buttons: [
            { text: '取消', type: 'cancel', onClick: () => mdialog.close() },
            { text: '✅ 保存', type: 'primary', onClick: async () => {
                const shop = document.getElementById('spot-f-shop').value.trim();
                if (!shop) { alert('店铺名称不能为空'); return; }
                const result = await API.saveFoodSpot({
                    id: state.spotEditingId,
                    shop_name: shop,
                    address: document.getElementById('spot-f-addr').value.trim(),
                    category: document.getElementById('spot-f-cat').value.trim(),
                    review: document.getElementById('spot-f-review').value.trim(),
                    files: state.spotFormFiles
                });
                if (result.success) {
                    mdialog.close();
                    await loadFoodSpots();
                } else if (result.spot) {
                    // 文本已存但部分照片失败：提示后仍刷新列表并关闭，避免重复提交
                    alert('已保存文字，但部分照片失败：' + (result.error || '未知原因'));
                    mdialog.close();
                    await loadFoodSpots();
                } else {
                    alert(result.error || '保存失败');
                }
            }}
        ]
    });
    renderSpotFormPreviews();
}

async function deleteFoodSpot(spotId) {
    const cur = (state.spots || []).find(s => s.id === spotId);
    const photoNote = (cur && cur.photos && cur.photos.length)
        ? '（连同 ' + cur.photos.length + ' 张照片）' : '';
    if (!confirm('确定删除打卡点「' + (cur ? cur.shop_name : '') + '」' + photoNote + '吗？')) return;
    const result = await API.deleteFoodSpot(spotId);
    if (result.success) {
        await loadFoodSpots();
    } else {
        alert(result.error || '删除失败');
    }
}

// =============================================================================
// 页面初始化
// =============================================================================
async function init() {
    // 设置日期默认值
    const dateInput = document.getElementById('record-date');
    dateInput.value = todayStr();
    dateInput.addEventListener('change', updateJournalEntryLink);
    updateJournalEntryLink();
    const heroDate = document.getElementById('hero-date');
    if (heroDate) heroDate.textContent = todayStr();

    // 设置多餐食物搜索自动补全
    setupFoodSearch();

    // 初始化每餐食物列表为空
    clearAllMealFoods();

    // 实时预览绑定
    ['morning-weight', 'daily-steps'].forEach(function(id) {
        const el = document.getElementById(id);
        if (el) el.addEventListener('input', updatePreview);
    });

    // 加载数据
    const result = await API.getRecords();
    if (result.success) {
        state.records = result.records || [];
        state.dashboard = result.dashboard || {};
        state.config = result.config || {};
        renderDashboard();
        renderHistory();
        renderTrendChart();
    }

    // 美食打卡点列表（独立加载，失败不影响主功能）
    loadFoodSpots();
}

// 页面加载完成后初始化
document.addEventListener('DOMContentLoaded', init);