/**
 * DOM 元素标记和提取
 *
 * 基于 Skyvern 的 domUtils.js，简化版
 * 核心功能：给所有可交互元素分配唯一 ID
 */

// 唯一 ID 计数器
let uniqueIdCounter = 0;

function generateUniqueId() {
    return `skyvern-${uniqueIdCounter++}`;
}

/**
 * 检查元素是否可见
 */
function isVisible(element) {
    if (!element) return false;

    // 检查 display 和 visibility
    const style = window.getComputedStyle(element);
    if (style.display === 'none' || style.visibility === 'hidden') {
        return false;
    }

    // 检查尺寸
    const rect = element.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) {
        return false;
    }

    // 检查是否在视口内
    if (rect.bottom < 0 || rect.top > window.innerHeight) {
        return false;
    }

    return true;
}

/**
 * 检查元素是否可交互
 * 参考 Skyvern 的实现，添加更多过滤条件
 */
function isInteractable(element) {
    if (!element) return false;

    const tagName = element.tagName.toLowerCase();
    const type = element.getAttribute('type');

    // 排除脚本和样式标签
    if (tagName === 'script' || tagName === 'style') return false;

    // 可交互的标签
    const interactableTags = ['a', 'button', 'input', 'select', 'textarea'];
    if (interactableTags.includes(tagName)) {
        // 对于 <a> 标签，进行额外的过滤
        if (tagName === 'a') {
            const href = element.getAttribute('href');
            const text = element.innerText?.toLowerCase() || '';
            const ariaLabel = element.getAttribute('aria-label')?.toLowerCase() || '';

            // 过滤掉明显的次要链接（GitHub 等网站的元数据链接）
            const secondaryPatterns = [
                'stargazers', 'stars', 'forks', 'watchers', 'watch',
                'fork', 'sponsor', 'issues', 'pull requests',
                'discussions', 'actions', 'projects', 'security',
                'insights', 'settings', 'commits', 'branches',
                'releases', 'packages', 'contributors'
            ];

            // 检查文本和 aria-label 是否包含次要模式
            const isSecondaryLink = secondaryPatterns.some(pattern =>
                text.includes(pattern) || ariaLabel.includes(pattern)
            );

            // 检查 href 是否指向次要页面
            const isSecondaryHref = href && secondaryPatterns.some(pattern =>
                href.includes(`/${pattern}`) || href.endsWith(`/${pattern}`)
            );

            // 如果是次要链接，降低优先级（但不完全排除，因为可能有误判）
            if (isSecondaryLink || isSecondaryHref) {
                // 检查元素大小，如果很小，可能确实是次要链接
                const rect = element.getBoundingClientRect();
                if (rect.width < 100 || rect.height < 30) {
                    return false;  // 小的次要链接，排除
                }
            }
        }

        return true;
    }

    // 有 onclick 或 role 的元素
    if (element.onclick || element.getAttribute('role') === 'button') {
        return true;
    }

    // 有 cursor: pointer 的元素
    const style = window.getComputedStyle(element);
    if (style.cursor === 'pointer') {
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
 * 添加优先级排序，确保主要内容链接排在前面
 */
function markAndExtractElements() {
    // 重置计数器
    uniqueIdCounter = 0;

    // 获取所有元素
    const allElements = document.querySelectorAll('*');
    const interactableElements = [];

    // 遍历所有元素
    for (const element of allElements) {
        // 检查是否可见和可交互
        if (isVisible(element) && isInteractable(element)) {
            // 分配唯一 ID
            const id = assignUniqueId(element);

            // 构建元素信息
            const elementInfo = buildElementInfo(element);

            // 计算优先级分数（用于排序）
            elementInfo.priority = calculateElementPriority(element);

            interactableElements.push(elementInfo);
        }
    }

    // 按优先级排序（高优先级在前）
    interactableElements.sort((a, b) => b.priority - a.priority);

    return {
        elements: interactableElements,
        count: interactableElements.length,
        viewport: {
            width: window.innerWidth,
            height: window.innerHeight,
        }
    };
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
