document.querySelector("[data-sidebar-toggle]")?.addEventListener("click", () => {
  document.querySelector("#sidebar")?.classList.toggle("open");
});

document.querySelectorAll("[data-formset]").forEach((formset) => {
  const prefix = formset.dataset.prefix;
  const total = formset.querySelector(`#id_${prefix}-TOTAL_FORMS`);
  const list = formset.querySelector("[data-line-list]");
  const template = formset.querySelector("[data-empty-line]");
  formset.querySelector("[data-add-line]")?.addEventListener("click", () => {
    const index = Number(total.value);
    const html = template.innerHTML.replaceAll("__prefix__", index);
    list.insertAdjacentHTML("beforeend", html);
    total.value = index + 1;
  });
});

