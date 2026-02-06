/**
 * DOM 元素标记和提取 v3.0
 *
 * 基于 Skyvern 的 domUtils.js 完整实现
 * 核心改进：
 * 1. 多维度可交互性判断（视觉、指针、行为、状态校验）
 * 2. expectHitTarget 击中测试（解决重叠和遮挡问题）
 * 3. QuadTree 四叉树空间管理（优化标注密度）
 * 4. 更智能的元素过滤和优先级排序
 */

// 唯一 ID 计数器
let uniqueIdCounter = 0;

function generateUniqueId() {
    return `skyvern-${uniqueIdCounter++}`;
}

// ============================================================================
// Skyvern 高级特性：四叉树空间管理
// ============================================================================

/**
 * 四叉树节点类
 * 用于高效的空间查询和重叠检测
 */
class QuadTreeNode {
    constructor(bounds, maxElements = 10, maxDepth = 4) {
        this.bounds = bounds;        // {x, y, width, height}
        this.maxElements = maxElements;
        this.maxDepth = maxDepth;
        this.elements = [];
        this.children = null;
        this.depth = 0;
    }

    // 插入元素
    insert(element) {
        if (!this.contains(element.rect)) {
            return false;
        }

        // 如果未细分且未达到容量上限，直接插入
        if (this.children === null && this.elements.length < this.maxElements) {
            this.elements.push(element);
            return true;
        }

        // 达到容量上限，进行细分
        if (this.children === null && this.depth < this.maxDepth) {
            this.subdivide();
        }

        // 尝试插入到子节点
        if (this.children !== null) {
            for (const child of this.children) {
                if (child.insert(element)) {
                    return true;
                }
            }
        }

        // 无法插入子节点，保留在当前节点
        this.elements.push(element);
        return true;
    }

    // 细分为四个子节点
    subdivide() {
        const x = this.bounds.x;
        const y = this.bounds.y;
        const w = this.bounds.width / 2;
        const h = this.bounds.height / 2;

        this.children = [
            new QuadTreeNode({x, y, width: w, height: h}, this.maxElements, this.maxDepth),
            new QuadTreeNode({x: x + w, y, width: w, height: h}, this.maxElements, this.maxDepth),
            new QuadTreeNode({x, y: y + h, width: w, height: h}, this.maxElements, this.maxDepth),
            new QuadTreeNode({x: x + w, y: y + h, width: w, height: h}, this.maxElements, this.maxDepth),
        ];

        for (const child of this.children) {
            child.depth = this.depth + 1;
        }
    }

    // 检查矩形是否完全包含在边界内
    contains(rect) {
        return (
            rect.left >= this.bounds.x &&
            rect.right <= this.bounds.x + this.bounds.width &&
            rect.top >= this.bounds.y &&
            rect.bottom <= this.bounds.y + this.bounds.height
        );
    }

    // 查询与矩形相交的所有元素
    query(rect) {
        const result = [];
        this.queryRecursive(rect, result);
        return result;
    }

    queryRecursive(rect, result) {
        if (!this.intersects(rect)) {
            return;
        }

        result.push(...this.elements);

        if (this.children) {
            for (const child of this.children) {
                child.queryRecursive(rect, result);
            }
        }
    }

    // 检查矩形是否与边界相交
    intersects(rect) {
        return (
            rect.left < this.bounds.x + this.bounds.width &&
            rect.right > this.bounds.x &&
            rect.top < this.bounds.y + this.bounds.height &&
            rect.bottom > this.bounds.y
        );
    }
}

// ============================================================================
// Skyvern 高级特性：expectHitTarget 击中测试
// ============================================================================

/**
 * 获取父元素或 Shadow Host
 */
function parentElementOrShadowHost(element) {
    if (element.parentElement) {
        return element.parentElement;
    }
    if (element.parentNode && element.parentNode.nodeType === 11) {
        return element.parentNode.host;
    }
    return null;
}

/**
 * 获取包含的 Shadow Root 或 Document
 */
function enclosingShadowRootOrDocument(element) {
    let node = element;
    while (node.parentNode) {
        node = node.parentNode;
    }
    if (node.nodeType === 11 || node.nodeType === 9) {
        return node;
    }
    return null;
}

/**
 * expectHitTarget - 击中测试
 * 检查点击某个坐标时，是否会真正击中目标元素
 *
 * 来源：改编自 Playwright 的实现
 * 用途：解决元素被遮挡、重叠的问题
 *
 * @param {Object} hitPoint - {x, y} 坐标
 * @param {Element} targetElement - 目标元素
 * @returns {Element|null} - 如果击中目标返回 null，否则返回遮挡元素
 */
function expectHitTarget(hitPoint, targetElement) {
    const roots = [];

    // 1. 收集所有组件根节点（处理 Shadow DOM）
    let parentElement = targetElement;
    while (parentElement) {
        const root = enclosingShadowRootOrDocument(parentElement);
        if (!root) break;
        roots.push(root);
        if (root.nodeType === 9) break; // DOCUMENT_NODE
        parentElement = root.host;
    }

    // 2. 从顶层到底层检查每个根节点的击中目标
    let hitElement;
    for (let index = roots.length - 1; index >= 0; index--) {
        const root = roots[index];
        const elements = root.elementsFromPoint ? root.elementsFromPoint(hitPoint.x, hitPoint.y) : [];
        const singleElement = root.elementFromPoint ? root.elementFromPoint(hitPoint.x, hitPoint.y) : null;

        // 3. 处理 display:contents 的特殊情况
        if (singleElement && elements[0] &&
            parentElementOrShadowHost(singleElement) === elements[0]) {
            const style = window.getComputedStyle(singleElement);
            if (style && style.display === "contents") {
                elements.unshift(singleElement);
            }
        }

        // 4. 处理 WebKit 的 bug（元素顺序错误）
        if (elements[0] && elements[0].shadowRoot === root &&
            elements[1] === singleElement) {
            elements.shift();
        }

        const innerElement = elements[0];
        if (!innerElement) break;
        hitElement = innerElement;
        if (index && innerElement !== roots[index - 1].host) break;
    }

    // 5. 检查击中元素是否是目标或其后代
    const hitParents = [];
    while (hitElement && hitElement !== targetElement) {
        hitParents.push(hitElement);
        hitElement = parentElementOrShadowHost(hitElement);
    }

    if (hitElement === targetElement) return null;
    return hitParents[0] || document.documentElement;
}

// ============================================================================
// 增强的可见性和可交互性检查
// ============================================================================

/**
 * 检查元素是否可见（增强版）
 * 参考 Skyvern 的 isElementVisible 实现
 */
function isVisible(element) {
    if (!element) return false;

    // 检查 display 和 visibility
    const style = window.getComputedStyle(element);
    if (!style) return false;

    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }

    // 检查尺寸
    const rect = element.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) {
        return false;
    }

    // 检查是否在视口内（至少部分可见）
    if (rect.bottom < 0 || rect.top > window.innerHeight ||
        rect.right < 0 || rect.left > window.innerWidth) {
        return false;
    }

    return true;
}

/**
 * 检查元素是否被隐藏
 */
function isHidden(element) {
    const style = window.getComputedStyle(element);
    return style && (style.display === 'none' || style.visibility === 'hidden');
}

/**
 * 检查是否是脚本或样式标签
 */
function isScriptOrStyle(element) {
    const tagName = element.tagName.toLowerCase();
    return tagName === 'script' || tagName === 'style' || tagName === 'noscript';
}

/**
 * 检查元素是否有 ARIA Widget 角色
 */
function hasWidgetRole(element) {
    const role = element.getAttribute('role');
    const widgetRoles = [
        'button', 'checkbox', 'radio', 'textbox', 'combobox', 'listbox',
        'option', 'menuitem', 'tab', 'slider', 'spinbutton', 'switch'
    ];
    return role && widgetRoles.includes(role);
}

/**
 * 检查是否是 hover-only 元素
 */
function isHoverOnlyElement(element) {
    // 检查是否有 :hover 伪类样式
    // 简化实现：检查常见的 hover 指示器
    const className = element.className || '';
    // 确保 className 是字符串
    const classStr = typeof className === 'string' ? className : String(className);
    return classStr.includes('hover') || classStr.includes('dropdown');
}

/**
 * 检查是否有 Angular 点击绑定
 */
function hasAngularClickBinding(element) {
    return element.hasAttribute('ng-click') ||
           element.hasAttribute('(click)') ||
           element.hasAttribute('data-ng-click');
}

/**
 * 检查是否是 hover pointer 元素
 */
function isHoverPointerElement(element) {
    const style = window.getComputedStyle(element);
    return style && style.cursor === 'pointer';
}

/**
 * 检查元素是否可交互（完整版 - 基于 Skyvern）
 *
 * 多维度判断：
 * 1. 视觉校验 - 检查可见性
 * 2. 指针校验 - 检查 cursor: pointer
 * 3. 行为校验 - 检查事件绑定
 * 4. 状态校验 - 检查 disabled/readonly
 * 5. ARIA 角色校验
 */
function isInteractable(element) {
    if (!element) return false;

    // 1. 基础可见性检查
    if (!isVisible(element)) return false;
    if (isHidden(element)) return false;
    if (isScriptOrStyle(element)) return false;

    const tagName = element.tagName.toLowerCase();
    const type = element.getAttribute('type');
    const className = element.className || '';

    // 2. ARIA 角色检查（优先级最高）
    if (hasWidgetRole(element)) return true;

    // 3. pointer-events 检查
    const style = window.getComputedStyle(element);
    if (style) {
        const pointerEvents = style.pointerEvents;
        if (pointerEvents === "none" && !element.disabled) {
            if (!isHoverOnlyElement(element)) return false;
        }
    }

    // 4. 排除特定标签
    if (tagName === "html" || tagName === "iframe" ||
        tagName === "frameset" || tagName === "frame") {
        return false;
    }

    // 5. 标准交互元素
    if (tagName === "a" && element.href) {
        // 对于 <a> 标签，进行额外的过滤
        const href = element.getAttribute('href');
        const text = element.innerText?.toLowerCase() || '';
        const ariaLabel = element.getAttribute('aria-label')?.toLowerCase() || '';

        // 过滤掉明显的次要链接
        const secondaryPatterns = [
            'stargazers', 'stars', 'forks', 'watchers', 'watch',
            'fork', 'sponsor', 'issues', 'pull requests',
            'discussions', 'actions', 'projects', 'security',
            'insights', 'settings', 'commits', 'branches',
            'releases', 'packages', 'contributors'
        ];

        const isSecondaryLink = secondaryPatterns.some(pattern =>
            text.includes(pattern) || ariaLabel.includes(pattern)
        );

        const isSecondaryHref = href && secondaryPatterns.some(pattern =>
            href.includes(`/${pattern}`) || href.endsWith(`/${pattern}`)
        );

        if (isSecondaryLink || isSecondaryHref) {
            const rect = element.getBoundingClientRect();
            if (rect.width < 100 || rect.height < 30) {
                return false;
            }
        }

        return true;
    }

    if (tagName === "button" || tagName === "select" ||
        tagName === "textarea") {
        return !element.disabled;
    }

    // 6. Input 元素检查
    if (tagName === "input") {
        if (element.disabled || element.readOnly) return false;
        // 排除 hidden input
        if (type === "hidden") return false;
        return true;
    }

    // 7. Label 元素
    if (tagName === "label" && element.control && !element.control.disabled) {
        return true;
    }

    // 8. 事件属性检查
    if (element.hasAttribute("onclick") ||
        element.isContentEditable ||
        element.hasAttribute("jsaction")) {
        return true;
    }

    // 9. 特殊 div/span 检查
    if (tagName === "div" || tagName === "span") {
        // Angular 绑定
        if (hasAngularClickBinding(element)) return true;

        // 特殊类名
        if (className.includes("blinking-cursor") ||
            className.includes("svg-container")) {
            return true;
        }
    }

    // 10. Listbox/Option 角色
    if ((tagName === "ul" || tagName === "div") &&
        element.getAttribute("role") === "listbox") {
        return true;
    }
    if ((tagName === "li" || tagName === "div") &&
        element.getAttribute("role") === "option") {
        return true;
    }

    // 11. CSS 指针样式检查
    if (isHoverPointerElement(element)) return true;

    // 12. jQuery 事件检查
    if (typeof window.jQuery !== 'undefined' && window.jQuery._data) {
        try {
            const events = window.jQuery._data(element, "events");
            if (events && "click" in events) return true;
        } catch (e) {
            // jQuery 可能不可用
        }
    }

    // 13. tabindex 检查
    if (tagName === "div" && element.getAttribute("tabindex") === "0") {
        return true;
    }

    return false;
}

/**
 * 给元素分配唯一 ID
 */
function assignUniqueId(element) {
    if (!element.hasAttribute('unique_id')) {
        const id = generateUniqueId();
        element.setAttribute('unique_id', id);
        return id;
    }
    return element.getAttribute('unique_id');
}

/**
 * 构建元素信息
 */
function buildElementInfo(element) {
    const rect = element.getBoundingClientRect();
    const style = window.getComputedStyle(element);

    return {
        id: element.getAttribute('unique_id'),
        tagName: element.tagName.toLowerCase(),
        text: element.innerText?.substring(0, 200) || '',
        attributes: {
            id: element.id || null,
            class: element.className || null,
            href: element.href || null,
            type: element.type || null,
            name: element.name || null,
            placeholder: element.placeholder || null,
            value: element.value || null,
        },
        rect: {
            x: Math.round(rect.x),
            y: Math.round(rect.y),
            width: Math.round(rect.width),
            height: Math.round(rect.height),
            top: Math.round(rect.top),
            left: Math.round(rect.left),
            bottom: Math.round(rect.bottom),
            right: Math.round(rect.right),
        },
        isVisible: isVisible(element),
        isInteractable: isInteractable(element),
    };
}

/**
 * 主函数：标记所有可交互元素并返回元素列表
 * v3.0 完整优化（完全基于 Skyvern）：
 * - 多维度可交互性判断
 * - expectHitTarget 击中测试（解决遮挡问题）
 * - QuadTree 四叉树空间管理（优化重叠检测）
 * - 更智能的元素过滤和优先级排序
 */
function markAndExtractElements() {
    // 重置计数器
    uniqueIdCounter = 0;

    // 获取所有元素
    const allElements = document.querySelectorAll('*');
    const candidateElements = [];

    // 遍历所有元素
    for (const element of allElements) {
        // 检查是否可见和可交互（使用增强版函数）
        if (!isVisible(element) || !isInteractable(element)) {
            continue;
        }

        const rect = element.getBoundingClientRect();
        const area = rect.width * rect.height;

        // 过滤太小的元素（面积 < 400px²）
        if (area < 400) {
            continue;
        }

        // 过滤太大的元素（可能是容器，面积 > 视口的 80%）
        const viewportArea = window.innerWidth * window.innerHeight;
        if (area > viewportArea * 0.8) {
            continue;
        }

        // expectHitTarget 击中测试：确保元素真的可以被点击
        const centerX = rect.left + rect.width / 2;
        const centerY = rect.top + rect.height / 2;

        // 检查中心点是否在视口内
        if (centerX < 0 || centerX > window.innerWidth ||
            centerY < 0 || centerY > window.innerHeight) {
            continue;
        }

        // 使用 expectHitTarget 进行精确的击中测试
        const hitPoint = { x: centerX, y: centerY };
        const blocker = expectHitTarget(hitPoint, element);

        // 如果有遮挡元素，跳过
        if (blocker !== null) {
            continue;
        }

        // 分配唯一 ID
        const id = assignUniqueId(element);

        // 构建元素信息
        const elementInfo = buildElementInfo(element);

        // ��算优先级分数（用于排序）
        elementInfo.priority = calculateElementPriority(element);
        elementInfo.area = area;  // 保存面积用于排序

        candidateElements.push(elementInfo);
    }

    // 按面积排序（大元素优先，用于四叉树插入）
    candidateElements.sort((a, b) => b.area - a.area);

    // 使用四叉树进行空间管理和重叠过滤
    const filteredElements = filterOverlappingElementsWithQuadTree(candidateElements, 0.5);

    // 限制最多 30 个元素（更少更精准）
    const finalElements = filteredElements.slice(0, 30);

    // 按优先级重新排序（用于 VLM 列表显示）
    finalElements.sort((a, b) => b.priority - a.priority);

    return {
        elements: finalElements,
        count: finalElements.length,
        viewport: {
            width: window.innerWidth,
            height: window.innerHeight,
        }
    };
}

/**
 * 使用四叉树过滤重叠元素（高性能版本）
 * 如果两个元素重叠度 > threshold，只保留面积更大的那个
 *
 * @param {Array} elements - 元素列表（已按面积排序）
 * @param {number} threshold - 重叠阈值 (0-1)
 * @returns {Array} - 过滤后的元素列表
 */
function filterOverlappingElementsWithQuadTree(elements, threshold = 0.5) {
    if (elements.length === 0) return [];

    // 计算边界
    const bounds = calculateBounds(elements);

    // 创建四叉树
    const quadTree = new QuadTreeNode(bounds, 10, 4);

    // 插入所有元素到四叉树
    for (const element of elements) {
        quadTree.insert(element);
    }

    // 过滤重叠元素
    const filtered = [];
    const processed = new Set();

    for (const element of elements) {
        if (processed.has(element.id)) continue;

        // 使用四叉树快速查找附近的元素
        const nearby = quadTree.query(element.rect);

        // 检查是否与已添加的元素重叠
        let hasOverlap = false;
        for (const other of filtered) {
            // 检查是否在附近元素列表中
            if (nearby.some(n => n.id === other.id)) {
                const overlap = calculateOverlap(element.rect, other.rect);
                if (overlap > threshold) {
                    hasOverlap = true;
                    break;
                }
            }
        }

        if (!hasOverlap) {
            filtered.push(element);
        }

        processed.add(element.id);
    }

    return filtered;
}

/**
 * 计算元素列表的边界
 */
function calculateBounds(elements) {
    if (elements.length === 0) {
        return { x: 0, y: 0, width: window.innerWidth, height: window.innerHeight };
    }

    let minX = Infinity, minY = Infinity;
    let maxX = -Infinity, maxY = -Infinity;

    for (const element of elements) {
        const rect = element.rect;
        minX = Math.min(minX, rect.left);
        minY = Math.min(minY, rect.top);
        maxX = Math.max(maxX, rect.right);
        maxY = Math.max(maxY, rect.bottom);
    }

    return {
        x: minX,
        y: minY,
        width: maxX - minX,
        height: maxY - minY
    };
}

/**
 * 过滤重叠元素（原始版本，保留作为备用）
 * 如果两个元素重叠度 > threshold，只保留面积更大的那个
 */
function filterOverlappingElements(elements, threshold = 0.5) {
    const filtered = [];
    const used = new Set();

    for (let i = 0; i < elements.length; i++) {
        if (used.has(i)) continue;

        const elem1 = elements[i];

        // 检查是否与已添加的元素重叠
        let hasOverlap = false;
        for (let j = 0; j < filtered.length; j++) {
            const elem2 = filtered[j];
            const overlap = calculateOverlap(elem1.rect, elem2.rect);

            // 如果重叠度 > threshold，跳过当前元素
            if (overlap > threshold) {
                hasOverlap = true;
                break;
            }
        }

        if (!hasOverlap) {
            filtered.push(elem1);
        }
    }

    return filtered;
}

/**
 * 计算两个矩形的重叠度
 * 返回值：0-1，表示较小矩形被覆盖的比例
 */
function calculateOverlap(rect1, rect2) {
    const x1 = Math.max(rect1.x, rect2.x);
    const y1 = Math.max(rect1.y, rect2.y);
    const x2 = Math.min(rect1.x + rect1.width, rect2.x + rect2.width);
    const y2 = Math.min(rect1.y + rect1.height, rect2.y + rect2.height);

    if (x2 <= x1 || y2 <= y1) {
        return 0;  // 没有重叠
    }

    const overlapArea = (x2 - x1) * (y2 - y1);
    const area1 = rect1.width * rect1.height;
    const area2 = rect2.width * rect2.height;
    const smallerArea = Math.min(area1, area2);

    return overlapArea / smallerArea;
}

/**
 * 计算元素的优先级分数
 * 分数越高，优先级越高
 */
function calculateElementPriority(element) {
    let score = 0;
    const tagName = element.tagName.toLowerCase();
    const text = element.innerText || '';
    const href = element.getAttribute('href') || '';
    const rect = element.getBoundingClientRect();

    // 1. 标签类型优先级
    if (tagName === 'a') score += 100;  // 链接优先
    else if (tagName === 'button') score += 80;
    else if (tagName === 'input') score += 60;

    // 2. 元素大小优先级（大元素优先）
    const area = rect.width * rect.height;
    if (area > 10000) score += 50;  // 大于 100x100
    else if (area > 5000) score += 30;  // 大于 70x70
    else if (area > 2000) score += 10;  // 大于 45x45

    // 3. 文本长度优先级（有实质内容的优先）
    if (text.length > 50) score += 30;
    else if (text.length > 20) score += 20;
    else if (text.length > 5) score += 10;

    // 4. 位置优先级（靠上的优先）
    if (rect.top < 300) score += 20;  // 页面顶部
    else if (rect.top < 600) score += 10;  // 页面中上部

    // 5. 降低次要链接的优先级
    const secondaryPatterns = [
        'stargazers', 'stars', 'forks', 'watchers', 'watch',
        'fork', 'sponsor', 'issues', 'pull requests',
        'discussions', 'actions', 'projects', 'security',
        'insights', 'settings', 'commits', 'branches',
        'releases', 'packages', 'contributors'
    ];

    const textLower = text.toLowerCase();
    const hrefLower = href.toLowerCase();

    for (const pattern of secondaryPatterns) {
        if (textLower.includes(pattern) || hrefLower.includes(`/${pattern}`)) {
            score -= 100;  // 大幅降低次要链接的优先级
            break;
        }
    }

    // 6. 提升主要内容链接的优先级
    // 如果 href 指向主要内容页面（不包含次要路径）
    if (tagName === 'a' && href && !href.includes('#')) {
        const pathSegments = href.split('/').filter(s => s.length > 0);
        // 简单路径（如 /owner/repo）优先于复杂路径（如 /owner/repo/stargazers）
        if (pathSegments.length <= 3) {
            score += 40;
        }
    }

    return score;
}

/**
 * 通过 unique_id 查找元素
 */
function findElementById(uniqueId) {
    return document.querySelector(`[unique_id="${uniqueId}"]`);
}

/**
 * 通过 unique_id 点击元素
 * 使用多种方法确保点击成功，参考 Skyvern 的实现
 */
function clickElementById(uniqueId) {
    const element = findElementById(uniqueId);
    if (!element) {
        return false;
    }

    try {
        // 方法1: 尝试使用原生 click() 方法
        if (typeof element.click === 'function') {
            element.click();
            return true;
        }

        // 方法2: 如果没有 click 方法，使用事件分发
        // 创建并分发鼠标事件
        const clickEvent = new MouseEvent('click', {
            view: window,
            bubbles: true,
            cancelable: true,
            buttons: 1
        });
        element.dispatchEvent(clickEvent);
        return true;
    } catch (error) {
        console.error('Click failed:', error);

        // 方法3: 最后尝试触发 mousedown 和 mouseup 事件
        try {
            const mousedownEvent = new MouseEvent('mousedown', {
                view: window,
                bubbles: true,
                cancelable: true,
                buttons: 1
            });
            const mouseupEvent = new MouseEvent('mouseup', {
                view: window,
                bubbles: true,
                cancelable: true,
                buttons: 1
            });
            element.dispatchEvent(mousedownEvent);
            element.dispatchEvent(mouseupEvent);
            element.dispatchEvent(new MouseEvent('click', {
                view: window,
                bubbles: true,
                cancelable: true,
                buttons: 1
            }));
            return true;
        } catch (innerError) {
            console.error('All click methods failed:', innerError);
            return false;
        }
    }
}

/**
 * 获取元素的中心坐标
 */
function getElementCenter(uniqueId) {
    const element = findElementById(uniqueId);
    if (element) {
        const rect = element.getBoundingClientRect();
        return {
            x: Math.round(rect.left + rect.width / 2),
            y: Math.round(rect.top + rect.height / 2),
        };
    }
    return null;
}

// 导出函数供 Python 调用
if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
        markAndExtractElements,
        findElementById,
        clickElementById,
        getElementCenter,
    };
}
