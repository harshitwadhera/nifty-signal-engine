// Small DOM double for the dependency-free browser scripts; visual layout is checked in a browser.
function node(tagName = 'div') {
  return {tagName, textContent: '', value: '', className: '', clientWidth: 420, children: [], listeners: {}, attributes: {},
    append(...items) { for (const item of items) { item.remove?.(); item.parent = this; this.children.push(item); } },
    replaceChildren(...items) { for (const child of this.children) child.parent = null; this.children = []; this.append(...items); },
    remove() { if (this.parent) { this.parent.children = this.parent.children.filter(item => item !== this); this.parent = null; } },
    addEventListener(event, action) { this.listeners[event] = action; },
    setAttribute(key, value) { this.attributes[key] = String(value); if (key === 'class') this.className = String(value); },
    getAttribute(key) { return this.attributes[key] ?? null; },
    closest(selector) { return selector === '[data-start]' && this.attributes['data-start'] ? this : this.parent?.closest(selector); }
  };
}
module.exports = {node};
