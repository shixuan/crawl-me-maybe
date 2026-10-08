/* Keep selects as the value source while rendering their choices as buttons. */
for (const select of document.querySelectorAll("select.select")) {
  const wrap = document.createElement("div");
  wrap.className = "dropdown";
  wrap.id = `${select.id}-dropdown`;
  const trigger = document.createElement("button");
  trigger.type = "button";
  trigger.className = "select";
  const value = document.createElement("span");
  value.className = "select-label";
  trigger.append(value);
  trigger.setAttribute("aria-haspopup", "listbox");
  const menu = document.createElement("div");
  menu.className = "select-menu";
  menu.id = `${select.id}-choices`;
  menu.setAttribute("role", "listbox");
  trigger.setAttribute("aria-controls", menu.id);
  select.before(wrap);
  wrap.append(trigger, menu);
  select.style.display = "none";

  function close() {
    menu.hidden = true;
    trigger.setAttribute("aria-expanded", "false");
  }

  function sync() {
    wrap.hidden = select.hidden;
    value.textContent = select.selectedOptions[0]?.textContent || "choose";
    const label = select.closest("label")?.querySelector("span")?.textContent || select.id;
    trigger.setAttribute("aria-label", `${label}: ${trigger.textContent}`);
    menu.setAttribute("aria-label", label);
    menu.replaceChildren();
    for (const option of select.options) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "select-option";
      button.textContent = option.textContent;
      button.disabled = option.disabled;
      button.setAttribute("role", "option");
      button.setAttribute("aria-selected", String(option.selected));
      button.onclick = () => {
        select.value = option.value;
        select.dispatchEvent(new Event("change", { bubbles: true }));
        sync();
        trigger.focus();
      };
      menu.append(button);
    }
    close();
  }

  function open() {
    menu.hidden = false;
    trigger.setAttribute("aria-expanded", "true");
    (menu.querySelector('[aria-selected="true"]:not(:disabled)') || menu.querySelector("button:not(:disabled)"))?.focus();
  }

  trigger.onclick = () => menu.hidden ? open() : close();
  trigger.onkeydown = event => {
    if (["ArrowDown", "ArrowUp"].includes(event.key)) {
      event.preventDefault();
      open();
    }
  };
  wrap.addEventListener("keydown", event => {
    if (event.key === "Escape") {
      event.preventDefault();
      close();
      trigger.focus();
    } else if (event.target !== trigger && ["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
      event.preventDefault();
      const choices = [...menu.querySelectorAll("button:not(:disabled)")];
      const index = choices.indexOf(document.activeElement);
      const next = event.key === "Home" ? 0 : event.key === "End" ? choices.length - 1
        : (index + (event.key === "ArrowDown" ? 1 : -1) + choices.length) % choices.length;
      choices[next]?.focus();
    }
  });
  wrap.addEventListener("focusout", event => {
    if (!wrap.contains(event.relatedTarget)) close();
  });
  document.addEventListener("click", event => {
    if (!wrap.contains(event.target)) close();
  });
  select.addEventListener("change", sync);
  new MutationObserver(sync).observe(select, { childList: true, subtree: true, attributes: true });
  sync();
}
